import os
import json
from typing import Optional
from anthropic import AsyncAnthropic, APIError, RateLimitError, APITimeoutError, APIConnectionError
from .base import BaseProvider, retry_with_backoff


def _to_anthropic_messages(messages: list) -> tuple[Optional[str], list]:
    system = None
    result = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system = msg["content"]
            continue
        elif role == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            result.append({"role": "user", "content": content})
        elif role == "assistant":
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])
            blocks = []
            reasoning_content = msg.get("reasoning_content") or msg.get("reasoning") or ""
            if reasoning_content:
                blocks.append({"type": "thinking", "thinking": reasoning_content, "signature": ""})
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"]),
                    }
                )
            result.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            result.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg["tool_call_id"],
                            "content": msg.get("content", ""),
                        }
                    ],
                }
            )
    return system, result


def _to_openai_tools(tools: list) -> list:
    anthropic_tools = []
    for t in tools:
        if t.get("type") == "function":
            func = t["function"]
            anthropic_tools.append(
                {
                    "name": func["name"],
                    "description": func.get("description", ""),
                    "input_schema": func["parameters"],
                }
            )
    return anthropic_tools


class AnthropicProvider(BaseProvider):
    def __init__(self, api_key: str, base_url: str | None = None, extra_headers: dict | None = None):
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        if extra_headers:
            kwargs["default_headers"] = extra_headers
        self._client = AsyncAnthropic(**kwargs)
        self._max_tokens = int(os.getenv("KYREX_MAX_TOKENS", "8192"))

    @retry_with_backoff(
        max_retries=3,
        base_delay=1.0,
        max_delay=60.0,
        retryable_exceptions=(APIError, RateLimitError, APITimeoutError, APIConnectionError, Exception),
    )
    async def chat(self, model: str, messages: list, tools: list | None = None, stream_callback=None, reasoning_callback=None, interrupt_event=None) -> dict:
        try:
            system, anthropic_msgs = _to_anthropic_messages(messages)
            kwargs = {
                "model": model,
                "messages": anthropic_msgs,
                "max_tokens": self._max_tokens,
                "thinking": {"type": "enabled", "budget_tokens": 2000},
            }
            if system:
                kwargs["system"] = system
            if tools:
                kwargs["tools"] = _to_openai_tools(tools)

            if stream_callback:
                return await self._chat_stream(kwargs, stream_callback, reasoning_callback, interrupt_event)

            response = await self._client.messages.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            # Catch all exceptions and return as error dict
            return {
                "role": "assistant",
                "content": f"[Anthropic Provider Error: {str(e)}",
                "tool_calls": None,
                "reasoning_content": None,
            }

    async def _chat_stream(self, kwargs: dict, stream_callback, reasoning_callback=None, interrupt_event=None) -> dict:
        try:
            full_content = ""
            full_reasoning = ""

            async with self._client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    # Check interrupt on every event — breaks streaming immediately
                    if interrupt_event is not None and interrupt_event.is_set():
                        break

                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            text = event.delta.text
                            full_content += text
                            stream_callback(text)
                        elif event.delta.type == "thinking_delta":
                            full_reasoning += event.delta.thinking
                            if reasoning_callback:
                                reasoning_callback(event.delta.thinking)

                final_message = await stream.get_final_message()

            result = self._parse_response(final_message)
            result["content"] = full_content or result.get("content")
            if full_reasoning:
                result["reasoning_content"] = full_reasoning
            return result
        except Exception as e:
            # Catch all exceptions and return as error dict
            return {
                "role": "assistant",
                "content": f"[Anthropic Provider Error: {str(e)}",
                "tool_calls": None,
                "reasoning_content": None,
            }

    def _parse_response(self, response) -> dict:
        result = {"role": "assistant", "tool_calls": None, "reasoning_content": None}
        text_parts = []
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input),
                        },
                    }
                )
            elif block.type == "thinking":
                result["reasoning_content"] = block.thinking

        result["content"] = "\n".join(text_parts) if text_parts else None
        if tool_calls:
            result["tool_calls"] = tool_calls

        return result

    @property
    def name(self) -> str:
        return "anthropic"

    def supports_reasoning(self) -> bool:
        return True
