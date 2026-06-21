import os
import time
from openai import AsyncOpenAI, APIError, RateLimitError, APITimeoutError, APIConnectionError, AuthenticationError
from .base import BaseProvider, retry_with_backoff


class OpenAIProvider(BaseProvider):
    def __init__(self, api_key: str, base_url: str | None = None, extra_headers: dict | None = None):
        # Always strip the key — whitespace breaks the Authorization header
        if api_key:
            api_key = api_key.strip()
        # Fall back to env vars only if not provided via config (config takes priority)
        if not api_key and "OPENAI_API_KEY" in os.environ:
            api_key = os.environ["OPENAI_API_KEY"].strip()
        if not base_url and "OPENAI_BASE_URL" in os.environ:
            base_url = os.environ["OPENAI_BASE_URL"].strip()

        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        if extra_headers:
            kwargs["default_headers"] = extra_headers
        self._client = AsyncOpenAI(**kwargs)

    @retry_with_backoff(
        max_retries=3,
        base_delay=1.0,
        max_delay=60.0,
        retryable_exceptions=(APIError, RateLimitError, APITimeoutError, APIConnectionError, Exception),
    )
    async def chat(self, model: str, messages: list, tools: list | None = None, stream_callback=None, reasoning_callback=None, interrupt_event=None) -> dict:
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": 8192,
                "timeout": 120,
                "stream": True,
            }
            if tools:
                kwargs["tools"] = tools

            full_content = ""
            full_reasoning = ""
            tool_calls_raw = {}
            content_buffer = ""  # Accumulates raw content for <thinking> tag parsing

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