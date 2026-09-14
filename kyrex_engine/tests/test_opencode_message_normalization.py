"""
OpenCode message normalization regression tests.

The OpenCode gateway rejects top-level message fields such as `name`
("messages[N]: \"name\" is not supported by this endpoint"). The shared
message construction in kyrex.core (used by both Kyrex Chat and the TUI)
adds `name` to tool-result messages, so the OpenAI provider must strip it
ONLY when the request targets the OpenCode gateway:

  1. OpenCode requests contain no top-level `name` on any message.
  2. role, content, tool_call_id, order are preserved exactly.
  3. Nested tool_calls (id/type/function.name/arguments) serialize unchanged.
  4. Messages without `name` are passed through byte-for-byte (same list).
  5. OpenRouter / other providers still receive `name` — behavior unchanged.
  6. x-opencode-session is still sent alongside the normalized payload.

The OpenAI SDK is faked at the module boundary (same approach as
test_opencode_session.py) to capture the exact kwargs reaching
chat.completions.create with no network call.
"""

import asyncio
import json

import pytest

import kyrex.providers.openai_ as openai_module
from kyrex.providers import get_provider


class _FakeCompletions:
    def __init__(self, calls):
        self.calls = calls

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return iter([])


class _FakeChat:
    def __init__(self, calls):
        self.completions = _FakeCompletions(calls)


class _FakeClient:
    def __init__(self, calls, **client_kwargs):
        self._build_kwargs = client_kwargs
        self.chat = _FakeChat(calls)


@pytest.fixture
def captured_openai(monkeypatch):
    state = {"clients": [], "create_calls": []}

    def fake_async_openai(**kwargs):
        client = _FakeClient(state["create_calls"], **kwargs)
        state["clients"].append(client)
        return client

    monkeypatch.setattr(openai_module, "AsyncOpenAI", fake_async_openai)
    yield state


def _run_chat(provider, messages):
    return asyncio.run(
        provider.chat("test-model", messages, stream_callback=None)
    )


OPENCODE_URL = "https://opencode.ai/zen/go/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


def _tool_calling_messages():
    """Conversation with name-bearing tool result — the repro payload."""
    return [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_001",
                    "type": "function",
                    "function": {"name": "list_dir", "arguments": '{"path": "."}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_001",
            "name": "list_dir",
            "content": "src/\ntests/",
        },
        {"role": "user", "content": "thanks"},
    ]


# ── 1. OpenCode requests contain no top-level `name` ────────────────────

class TestOpenCodeStripsName:
    def test_normalized_request_is_json_serializable(self, captured_openai):
        provider = get_provider(
            "openai", api_key="k", base_url=OPENCODE_URL, session_id="s-json"
        )
        _run_chat(provider, _tool_calling_messages())

        request = captured_openai["create_calls"][0]
        assert isinstance(request["messages"], list)
        assert all(isinstance(message, dict) for message in request["messages"])
        json.dumps(request)

    def test_no_name_field_reaches_opencode(self, captured_openai):
        provider = get_provider(
            "openai", api_key="k", base_url=OPENCODE_URL, session_id="s-norm"
        )
        _run_chat(provider, _tool_calling_messages())

        assert len(captured_openai["create_calls"]) == 1
        sent = captured_openai["create_calls"][0]["messages"]
        for i, msg in enumerate(sent):
            assert "name" not in msg, f"message[{i}] still carries 'name'"

    def test_tool_result_vs_assistant_object(self, captured_openai):
        """The assistant tool_calls object survives `name` stripping intact
        even though the assistant message itself has no top-level name."""
        provider = get_provider(
            "openai", api_key="k", base_url=OPENCODE_URL, session_id="s"
        )
        _run_chat(provider, _tool_calling_messages())

        sent = captured_openai["create_calls"][0]["messages"]
        assistant = next(m for m in sent if m.get("role") == "assistant")
        tool = next(m for m in sent if m.get("role") == "tool")
        assert assistant["tool_calls"][0]["function"]["name"] == "list_dir"
        assert tool["tool_call_id"] == "call_001"


# ── 2. role / content / order / tool payloads preserved ──────────────────

class TestPreservation:
    def test_roles_content_order_preserved(self, captured_openai):
        base = [
            {"role": "system", "content": "You are Kyrex."},
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "c1", "name": "f", "content": "ok"},
            {"role": "user", "content": "next question"},
        ]
        provider = get_provider(
            "openai", api_key="k", base_url=OPENCODE_URL, session_id="s"
        )
        _run_chat(provider, base)

        sent = captured_openai["create_calls"][0]["messages"]
        assert [m["role"] for m in sent] == [
            "system", "user", "tool", "user",
        ]
        assert sent[0]["content"] == "You are Kyrex."
        assert sent[1]["content"] == "hello"
        assert sent[2]["tool_call_id"] == "c1"
        assert sent[2]["content"] == "ok"
        assert sent[3]["content"] == "next question"

    def test_multi_tool_call_message_serializes_correctly(self, captured_openai):
        msgs = [
            {
                "role": "assistant",
                "content": "doing two things",
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "a", "arguments": "{}"}},
                    {"id": "c2", "type": "function",
                     "function": {"name": "b", "arguments": '{"x": 1}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "a", "content": "r1"},
            {"role": "tool", "tool_call_id": "c2", "name": "b", "content": "r2"},
        ]
        provider = get_provider(
            "openai", api_key="k", base_url=OPENCODE_URL, session_id="s"
        )
        _run_chat(provider, msgs)

        sent = captured_openai["create_calls"][0]["messages"]
        assert sent[0]["tool_calls"] == msgs[0]["tool_calls"]
        assert all("name" not in m or m["role"] != "tool" for m in sent)
        assert sent[1]["tool_call_id"] == "c1" and sent[1]["content"] == "r1"
        assert sent[2]["tool_call_id"] == "c2" and sent[2]["content"] == "r2"

    def test_messages_without_name_pass_through_unchanged(self, captured_openai):
        msgs = _tool_calling_messages()
        plain = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ]
        provider = get_provider(
            "openai", api_key="k", base_url=OPENCODE_URL, session_id="s"
        )
        _run_chat(provider, plain)
        assert (
            captured_openai["create_calls"][-1]["messages"] is plain
        ), "name-free messages must not be copied or rebuilt"

    def test_static_normalizer_direct(self):
        from kyrex.providers.openai_ import OpenAIProvider

        msgs = [
            {"role": "tool", "tool_call_id": "c1", "name": "f", "content": "r"},
            {"role": "user", "content": "hi"},
        ]
        out = OpenAIProvider._normalize_messages_for_opencode(msgs)
        assert "name" not in out[0]
        assert out[0]["tool_call_id"] == "c1" and out[0]["content"] == "r"
        assert out[1] is msgs[1]

        same = [{"role": "user", "content": "hi"}]
        assert OpenAIProvider._normalize_messages_for_opencode(same) is same


# ── 3. OpenRouter behavior unchanged ─────────────────────────────────────

class TestOpenRouterUnchanged:
    def test_openrouter_still_receives_name(self, captured_openai):
        provider = get_provider("openai", api_key="k", base_url=OPENROUTER_URL)
        _run_chat(provider, _tool_calling_messages())

        sent = captured_openai["create_calls"][0]["messages"]
        tool = next(m for m in sent if m.get("role") == "tool")
        assert tool["name"] == "list_dir"
        assert "x-opencode-session" not in (
            captured_openai["clients"][0]._build_kwargs.get("default_headers") or {}
        )

    def test_non_opencode_custom_host_also_unchanged(self, captured_openai):
        provider = get_provider(
            "openai",
            api_key="k",
            base_url="https://my-llm-proxy.internal/v1",
        )
        _run_chat(provider, _tool_calling_messages())
        sent = captured_openai["create_calls"][0]["messages"]
        assert any("name" in m for m in sent)


# ── 4. x-opencode-session still sent ─────────────────────────────────────

class TestSessionHeaderStillSent:
    def test_header_present_on_normalized_request(self, captured_openai):
        provider = get_provider(
            "openai", api_key="k", base_url=OPENCODE_URL, session_id="conv-99"
        )
        _run_chat(provider, _tool_calling_messages())

        headers = captured_openai["clients"][0]._build_kwargs.get(
            "default_headers", {}
        )
        assert headers.get("x-opencode-session") == "conv-99"
        assert captured_openai["create_calls"][0]["model"] == "test-model"
