import inspect
import os
import time
import uuid
from openai import AsyncOpenAI, APIError, RateLimitError, APITimeoutError, APIConnectionError, AuthenticationError
from .base import BaseProvider, retry_with_backoff


class OpenAIProvider(BaseProvider):
    # OpenCode's (zen/go) router rejects a per-message ``name`` field and
    # refuses to route a request that omits a session id (MissingSessionID).
    _OPENCODE_SESSION_HEADER = "x-opencode-session"

    def __init__(self, api_key: str, base_url: str | None = None, extra_headers: dict | None = None):
        # Always strip the key — whitespace breaks the Authorization header
        if api_key:
            api_key = api_key.strip()
        # Fall back to env vars only if not provided via config (config takes priority)
        if not api_key and "OPENAI_API_KEY" in os.environ:
            api_key = os.environ["OPENAI_API_KEY"].strip()
        if not base_url and "OPENAI_BASE_URL" in os.environ:
            base_url = os.environ["OPENAI_BASE_URL"].strip()

        # Remember the endpoint: OpenCode needs request normalisation (drop the
        # message ``name`` field, inject a session header) that must NOT be
        # applied to OpenRouter or stock OpenAI.
        self._base_url = base_url or ""

        headers = dict(extra_headers or {})
        if self.is_opencode():
            # Added once here so the header rides every request. Honour an
            # explicit env session id, else mint a stable per-provider one.
            headers.setdefault(
                self._OPENCODE_SESSION_HEADER,
                (os.environ.get("KYREX_SESSION_ID") or "").strip()
                or f"kyrex-{uuid.uuid4().hex}",
            )

        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        if headers:
            kwargs["default_headers"] = headers
        self._client = AsyncOpenAI(**kwargs)

    def is_opencode(self) -> bool:
        """True when this provider targets the OpenCode (zen/go) router."""
        return "opencode" in self._base_url.lower()

    def normalize_messages(self, messages: list) -> list:
        """Return the message list to serialise into the request body.

        SYNCHRONOUS by design: this value is JSON-encoded into the HTTP body,
        so it must be a concrete list of dicts — never a coroutine.  An
        ``async`` normaliser called without ``await`` is exactly what produced
        "Object of type coroutine is not JSON serializable".

        For OpenCode only, drop the per-message ``name`` field, which its
        router rejects.  ``role``/``content``, ``tool_calls`` and
        ``tool_call_id`` are left untouched, so tool calls and tool results
        stay valid.  OpenRouter and every other endpoint get ``messages`` back
        unchanged (and by identity, so nothing else can drift).
        """
        if not self.is_opencode():
            return messages
        normalized = []
        for msg in messages:
            if isinstance(msg, dict) and "name" in msg:
                msg = {k: v for k, v in msg.items() if k != "name"}
            normalized.append(msg)
        return normalized

    async def _resolve_messages(self, messages: list) -> list:
        """Produce the concrete messages for the request body.

        Belt-and-suspenders against the regression this fixes: if the
        normaliser is ever (re)declared ``async`` and invoked without
        ``await``, the bare coroutine is awaited here, before serialisation,
        instead of leaking into the payload.
        """
        normalized = self.normalize_messages(messages)
        if inspect.iscoroutine(normalized):
            normalized = await normalized
        return normalized

    @retry_with_backoff(
        max_retries=3,
        base_delay=1.0,
        max_delay=60.0,
        retryable_exceptions=(APIError, RateLimitError, APITimeoutError, APIConnectionError, Exception),
    )
    async def chat(self, model: str, messages: list, tools: list | None = None, stream_callback=None, reasoning_callback=None, interrupt_event=None, final_round_callback=None) -> dict:
        try:
            # Normalise BEFORE serialisation. This must resolve to a concrete
            # list of dicts: a coroutine here would raise
            # "Object of type coroutine is not JSON serializable" inside the SDK.
            request_messages = await self._resolve_messages(messages)
            kwargs = {
                "model": model,
                "messages": request_messages,
                "max_tokens": 32768,
                "timeout": 120,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools

            full_content = ""
            full_reasoning = ""
            tool_calls_raw = {}
            content_buffer = ""  # Accumulates raw content for <thinking> tag parsing
            
            # ── Progressive final-round detection ──
            # Track content length and tool call presence to optimistically
            # signal when the final round starts (no tool calls after ~40 tokens).
            _streaming_content_len = 0
            _seen_tool_call = False
            _final_round_optimistic = False
            _FINAL_ROUND_TOKEN_THRESHOLD = 40  # tokens
            _FINAL_ROUND_CHAR_THRESHOLD = _FINAL_ROUND_TOKEN_THRESHOLD * 4  # ~4 chars/token

            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                # Check interrupt on every chunk — breaks streaming immediately
                if interrupt_event is not None and interrupt_event.is_set():
                    break

                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue

                # Handle native reasoning_content (DeepSeek/Kimi native field)
                native_reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if native_reasoning:
                    full_reasoning += native_reasoning
                    if reasoning_callback:
                        reasoning_callback(native_reasoning)

                # Handle tool calls from the stream (OpenAI sends these in chunks)
                if delta.tool_calls:
                    # Mark that we've seen at least one tool call delta
                    if not _seen_tool_call:
                        _seen_tool_call = True
                        # If we already optimistically signaled final round, correct it
                        if _final_round_optimistic:
                            _final_round_optimistic = False
                            if final_round_callback:
                                final_round_callback("round_has_tools_after_all")

                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_raw:
                            tool_calls_raw[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                        if tc.id:
                            tool_calls_raw[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_raw[idx]["function"]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls_raw[idx]["function"]["arguments"] += tc.function.arguments

                if not delta.content:
                    continue

                content_buffer += delta.content

                # Parse <thinking>...</thinking> tags from the content stream.
                while "</thinking>" in content_buffer:
                    close_idx = content_buffer.find("</thinking>")
                    reasoning_part = content_buffer[:close_idx]
                    content_buffer = content_buffer[close_idx + len("</thinking>"):]
                    # Strip the opening <thinking> tag if present
                    reasoning_part = reasoning_part.replace("<thinking>", "")
                    if reasoning_part:
                        full_reasoning += reasoning_part
                        if reasoning_callback:
                            reasoning_callback(reasoning_part)

                if "<thinking>" not in content_buffer and content_buffer:
                    has_partial = any(
                        content_buffer.endswith(tag[:i])
                        for tag in ["<thinking>", "</thinking>"]
                        for i in range(1, len(tag))
                    )
                    if not has_partial:
                        # Track content length for progressive final-round detection
                        if not _seen_tool_call and not _final_round_optimistic:
                            _streaming_content_len += len(content_buffer)
                            # Optimistically signal final round if we've streamed enough content
                            if _streaming_content_len >= _FINAL_ROUND_CHAR_THRESHOLD:
                                _final_round_optimistic = True
                                if final_round_callback:
                                    final_round_callback("final_round_starting")

                        full_content += content_buffer
                        if stream_callback:
                            stream_callback(content_buffer)
                        content_buffer = ""

            # After stream ends, flush any remaining buffered content.
            if content_buffer:
                if "<thinking>" in content_buffer:
                    reasoning_part = content_buffer.replace("<thinking>", "")
                    if reasoning_part:
                        full_reasoning += reasoning_part
                        if reasoning_callback:
                            reasoning_callback(reasoning_part)
                else:
                    full_content += content_buffer
                    if stream_callback:
                        stream_callback(content_buffer)

            tool_calls = list(tool_calls_raw.values()) if tool_calls_raw else None

            return {
                "role": "assistant",
                "content": full_content or None,
                **({"reasoning_content": full_reasoning} if full_reasoning else {}),
                "tool_calls": tool_calls
            }
        except Exception as e:
            # Catch all exceptions and return as error dict
            return {
                "role": "assistant",
                "content": f"[OpenAI Provider Error: {str(e)}",
                "tool_calls": None,
            }

    def supports_reasoning(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "openai"