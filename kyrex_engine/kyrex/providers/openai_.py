import os
import time
from urllib.parse import urlsplit
from openai import AsyncOpenAI, APIError, RateLimitError, APITimeoutError, APIConnectionError, AuthenticationError
from .base import BaseProvider, retry_with_backoff
# Shared OpenCode gateway detection (same source the setup wizard uses), plus
# the canonical header name. No duplicated host-matching logic anywhere.
from ..opencode import OPENCODE_SESSION_HEADER, is_opencode_gateway as _is_opencode_gateway


# OpenCode Go advertises these model IDs on /responses rather than
# /chat/completions. Keep the switch scoped to that gateway so the same ID on
# another OpenAI-compatible provider retains its configured protocol.
_OPENCODE_RESPONSES_MODELS = frozenset({
    "gpt-6-luna", "gpt-5.6-luna", "grok-4.7", "grok-4.6",
    "muse-spark-1.3-contributor", "muse-spark-1.2-contributor",
})


class OpenAIProvider(BaseProvider):
    def __init__(self, api_key: str, base_url: str | None = None, extra_headers: dict | None = None, session_id: str | None = None):
        # Always strip the key — whitespace breaks the Authorization header
        if api_key:
            api_key = api_key.strip()
        # Fall back to env vars only if not provided via config (config takes priority)
        if not api_key and "OPENAI_API_KEY" in os.environ:
            api_key = os.environ["OPENAI_API_KEY"].strip()
        if not base_url and "OPENAI_BASE_URL" in os.environ:
            base_url = os.environ["OPENAI_BASE_URL"].strip()

        headers = dict(extra_headers or {})
        # OpenCode requires a stable x-opencode-session to route requests to
        # a conversation. The provider/request layer adds it ONLY for the
        # OpenCode gateway, using the stable per-conversation id owned by the
        # session layer (one id per conversation, reused across every request).
        # Other custom headers are preserved, but runtime conversation identity
        # replaces the setup-only connection-test session header.
        if _is_opencode_gateway(base_url) and session_id:
            headers[OPENCODE_SESSION_HEADER] = session_id

        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        if headers:
            kwargs["default_headers"] = headers
        self._client = AsyncOpenAI(**kwargs)
        self._is_opencode = _is_opencode_gateway(base_url)
        self._client_kwargs = kwargs

    def set_session_id(self, session_id: str) -> None:
        """Switch the OpenCode request identity to the active conversation."""
        if not self._is_opencode or not session_id:
            return
        headers = dict(self._client_kwargs.get("default_headers") or {})
        if headers.get(OPENCODE_SESSION_HEADER) == session_id:
            return
        headers[OPENCODE_SESSION_HEADER] = session_id
        self._client_kwargs["default_headers"] = headers
        self._client = AsyncOpenAI(**self._client_kwargs)

    @staticmethod
    def _normalize_messages_for_opencode(messages: list) -> list:
        """Strip fields the OpenCode gateway rejects (e.g. top-level `name`).

        Only applied to OpenCode requests. Preserves role, content,
        tool_call_id, order, and every nested tool_calls/function name —
        only unsupported top-level message fields are removed. Returns the
        original list untouched for other providers (OpenRouter etc.)."""
        normalized = []
        changed = False
        for m in messages:
            if isinstance(m, dict) and "name" in m:
                m = {k: v for k, v in m.items() if k != "name"}
                changed = True
            normalized.append(m)
        return normalized if changed else messages

    @staticmethod
    def _responses_input(messages: list) -> list:
        """Convert the engine's Chat history into Responses input items."""
        items = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                items.append({"type": "function_call_output",
                              "call_id": message["tool_call_id"],
                              "output": str(message.get("content") or "")})
                continue
            if role not in {"system", "developer", "user", "assistant"}:
                continue
            content = message.get("content") or ""
            if content:
                items.append({"role": role, "content": content})
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                items.append({"type": "function_call",
                              "call_id": tool_call["id"],
                              "name": function["name"],
                              "arguments": function.get("arguments") or "{}"})
        return items

    @staticmethod
    def _responses_tools(tools: list | None) -> list:
        return [
            {"type": "function", **tool["function"]}
            for tool in (tools or []) if tool.get("type") == "function"
        ]

    async def _chat_responses(self, model, messages, tools, stream_callback,
                              reasoning_callback, interrupt_event,
                              final_round_callback) -> dict:
        kwargs = {
            "model": model,
            "input": self._responses_input(messages),
            "max_output_tokens": 32768,
            "timeout": 120,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = self._responses_tools(tools)

        client = self._client
        base = str(self._client_kwargs.get("base_url") or "")
        parsed = urlsplit(base)
        if (parsed.hostname == "opencode.ai"
                and parsed.path.rstrip("/") == "/inference/go/openai/v1"):
            # The legacy Go chat URL does not expose /responses. Keep its
            # Chat Completions requests intact and use the documented Go URL
            # only for Responses models.
            client = AsyncOpenAI(**{**self._client_kwargs,
                                    "base_url": "https://opencode.ai/zen/go/v1"})
        stream = await client.responses.create(**kwargs)
        text_parts = []
        reasoning_parts = []
        response = None
        async for event in stream:
            if interrupt_event is not None and interrupt_event.is_set():
                break
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                delta = getattr(event, "delta", "") or ""
                text_parts.append(delta)
                if stream_callback and delta:
                    stream_callback(delta)
            elif kind == "response.reasoning_summary_text.delta":
                delta = getattr(event, "delta", "") or ""
                reasoning_parts.append(delta)
                if reasoning_callback and delta:
                    reasoning_callback(delta)
            elif kind == "response.completed":
                response = event.response
            elif kind in {"response.failed", "response.incomplete"}:
                detail = getattr(getattr(event, "response", None), "error", None)
                raise RuntimeError(f"OpenCode Responses request {kind}: {detail}")

        # A missing completion is an interrupted or truncated stream, never
        # a successful empty assistant round.
        if response is None:
            raise RuntimeError("OpenCode Responses stream ended without completion")
        tool_calls = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", "") != "function_call":
                continue
            tool_calls.append({
                "id": item.call_id, "type": "function",
                "function": {"name": item.name, "arguments": item.arguments},
            })
        content = "".join(text_parts) or getattr(response, "output_text", "") or ""
        if content and not text_parts and stream_callback:
            stream_callback(content)
        if final_round_callback and content and not tool_calls:
            final_round_callback("final_round_starting")
        result = {"role": "assistant", "content": content or None,
                  "tool_calls": tool_calls or None}
        if reasoning_parts:
            result["reasoning_content"] = "".join(reasoning_parts)
        usage = getattr(response, "usage", None)
        if usage:
            result["usage"] = {
                "prompt_tokens": getattr(usage, "input_tokens", 0),
                "completion_tokens": getattr(usage, "output_tokens", 0),
            }
        return result

    @retry_with_backoff(
        max_retries=3,
        base_delay=1.0,
        max_delay=60.0,
        retryable_exceptions=(APIError, RateLimitError, APITimeoutError, APIConnectionError, Exception),
    )
    async def chat(self, model: str, messages: list, tools: list | None = None, stream_callback=None, reasoning_callback=None, interrupt_event=None, final_round_callback=None) -> dict:
        try:
            if self._is_opencode:
                messages = self._normalize_messages_for_opencode(messages)
                if model in _OPENCODE_RESPONSES_MODELS:
                    return await self._chat_responses(
                        model, messages, tools, stream_callback,
                        reasoning_callback, interrupt_event,
                        final_round_callback)
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": 32768,
                "timeout": 120,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if tools:
                kwargs["tools"] = tools

            full_content = ""
            full_reasoning = ""
            tool_calls_raw = {}
            stream_usage = None  # Captured from the final chunk (include_usage)
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

                # Capture real token usage from the final chunk (include_usage).
                if getattr(chunk, "usage", None):
                    stream_usage = {
                        "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0),
                        "completion_tokens": getattr(chunk.usage, "completion_tokens", 0),
                    }

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

            result = {
                "role": "assistant",
                "content": full_content or None,
                **({"reasoning_content": full_reasoning} if full_reasoning else {}),
                "tool_calls": tool_calls,
            }
            if stream_usage:
                result["usage"] = stream_usage
            return result
        except Exception as e:
            # Catch all exceptions and return as an explicit error dict. The
            # "error" key lets the engine distinguish a provider failure (e.g.
            # HTTP 429 usage/rate limit, 5xx) from a normal tool-less assistant
            # round — the former must terminate the turn immediately instead of
            # being counted as an empty round.
            detail = str(e)
            if getattr(e, "status_code", None) == 404 and "<!doctype html" in detail.lower():
                detail = ("OpenCode endpoint returned 404. Check the provider profile API URL "
                          "(https://opencode.ai/zen/go/v1) and deploy the latest backend.")
            return {
                "role": "assistant",
                "content": f"[OpenAI Provider Error: {detail}]",
                "tool_calls": None,
                "error": detail,
            }

    def supports_reasoning(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "openai"
