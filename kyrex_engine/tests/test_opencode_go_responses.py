"""OpenCode Go Responses models use /responses with tool history intact."""

import asyncio
from types import SimpleNamespace as NS

import pytest

import kyrex.providers.openai_ as openai_module


class _Events:
    def __init__(self, events):
        self.events = events

    def __aiter__(self):
        self._iterator = iter(self.events)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration


@pytest.fixture
def client(monkeypatch):
    calls = []

    async def create_response(**kwargs):
        calls.append(("responses", kwargs))
        result = NS(output=[NS(type="function_call", call_id="call_2",
                               name="list_dir", arguments='{"path":"."}')],
                    output_text="", usage=NS(input_tokens=12, output_tokens=4))
        return _Events([NS(type="response.completed", response=result)])

    async def create_chat(**kwargs):
        calls.append(("chat", kwargs))
        return _Events([NS(choices=[NS(delta=NS(content="ok", tool_calls=None), finish_reason="stop")], usage=None)])

    def fake_client(**kwargs):
        return NS(responses=NS(create=create_response),
                  chat=NS(completions=NS(create=create_chat)))

    monkeypatch.setattr(openai_module, "AsyncOpenAI", fake_client)
    return calls


def test_luna_uses_responses_and_preserves_tool_round_trip(client):
    provider = openai_module.OpenAIProvider(
        "key", base_url="https://opencode.ai/zen/go/v1", session_id="sess")
    messages = [
        {"role": "system", "content": "You are a Bot"},
        {"role": "assistant", "content": "", "reasoning_content": "private",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "list_dir", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "list_dir",
         "content": "src/"},
        {"role": "user", "content": "again"},
    ]
    tools = [{"type": "function", "function": {"name": "list_dir",
              "description": "List files", "parameters": {"type": "object"}}}]
    result = asyncio.run(provider.chat("gpt-6-luna", messages, tools=tools))
    assert result["tool_calls"][0]["id"] == "call_2"
    assert result["usage"] == {"prompt_tokens": 12, "completion_tokens": 4}
    kind, request = client[0]
    assert kind == "responses"
    assert request["model"] == "gpt-6-luna"
    assert request["input"][1] == {"type": "function_call", "call_id": "call_1",
                                   "name": "list_dir", "arguments": "{}"}
    assert request["input"][2] == {"type": "function_call_output",
                                   "call_id": "call_1", "output": "src/"}
    assert request["tools"][0]["name"] == "list_dir"
    assert "reasoning_content" not in str(request)


def test_deepseek_stays_on_chat_completions(client):
    provider = openai_module.OpenAIProvider(
        "key", base_url="https://opencode.ai/zen/go/v1")
    asyncio.run(provider.chat("deepseek-v4.1-flash", [{"role": "user", "content": "hi"}]))
    assert client[0][0] == "chat"


def test_legacy_go_base_uses_documented_responses_endpoint(client, monkeypatch):
    bases = []
    original = openai_module.AsyncOpenAI

    def record_client(**kwargs):
        bases.append(kwargs.get("base_url"))
        return original(**kwargs)

    monkeypatch.setattr(openai_module, "AsyncOpenAI", record_client)
    provider = openai_module.OpenAIProvider(
        "key", base_url="https://opencode.ai/inference/go/openai/v1", session_id="sess")
    result = asyncio.run(provider.chat("gpt-6-luna", [{"role": "user", "content": "hi"}]))
    assert result["tool_calls"][0]["id"] == "call_2"
    assert bases == ["https://opencode.ai/inference/go/openai/v1",
                     "https://opencode.ai/zen/go/v1"]


def test_luna_streams_text_and_rejects_incomplete_response(client, monkeypatch):
    provider = openai_module.OpenAIProvider(
        "key", base_url="https://opencode.ai/zen/go/v1")
    emitted = []

    async def stream_text(**kwargs):
        result = NS(output=[], output_text="hello", usage=None)
        return _Events([NS(type="response.output_text.delta", delta="hello"),
                        NS(type="response.completed", response=result)])

    provider._client.responses.create = stream_text
    result = asyncio.run(provider.chat("gpt-6-luna", [{"role": "user", "content": "hi"}],
                                       stream_callback=emitted.append))
    assert result["content"] == "hello"
    assert emitted == ["hello"]

    async def incomplete(**kwargs):
        return _Events([NS(type="response.output_text.delta", delta="partial")])

    provider._client.responses.create = incomplete
    result = asyncio.run(provider.chat("gpt-6-luna", [{"role": "user", "content": "hi"}]))
    assert "error" in result
