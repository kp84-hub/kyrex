"""Regression test for the OpenCode message-normalization path.

The reported failure was::

    Provider error: Object of type coroutine is not JSON serializable

Root cause: a message normaliser was declared ``async`` and called without
``await``, so a coroutine object landed in the provider request body and the
OpenAI SDK blew up while JSON-encoding it.

These checks build and JSON-serialise a real OpenCode request and assert the
four required invariants:

  * ``name`` is removed only for OpenCode,
  * tool calls / tool results remain valid (``tool_calls``, ``tool_call_id``),
  * the ``x-opencode-session`` header is present on OpenCode requests,
  * OpenRouter (and stock OpenAI) requests are unchanged.

Runs under pytest (bare ``test_*`` functions) and standalone::

    python3 tests/test_opencode_normalization.py
"""
import asyncio
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kyrex.providers.openai_ import OpenAIProvider  # noqa: E402

OPENCODE_BASE = "https://opencode.ai/zen/go/v1"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _messages():
    """A realistic turn: system, named user, assistant tool call, tool result."""
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "name": "alice", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "read_local_file",
                        "arguments": '{"path": "a"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "read_local_file",
            "tool_call_id": "call_1",
            "content": "file body",
        },
    ]


def _provider(base_url):
    return OpenAIProvider(api_key="sk-test-fake", base_url=base_url)


def _headers(provider):
    return {k.lower(): v for k, v in provider._client.default_headers.items()}


# ── The core regression: concrete list, JSON-serialisable, no coroutine ──────

def test_normalize_is_synchronous_and_returns_list():
    provider = _provider(OPENCODE_BASE)
    result = provider.normalize_messages(_messages())
    assert not inspect.iscoroutine(result), "normaliser must not return a coroutine"
    assert isinstance(result, list) and all(isinstance(m, dict) for m in result)


def test_opencode_payload_is_json_serializable():
    provider = _provider(OPENCODE_BASE)
    request_messages = provider.normalize_messages(_messages())
    payload = {"model": "gpt-x", "messages": request_messages, "max_tokens": 10}
    # This is precisely where the old coroutine bug surfaced.
    text = json.dumps(payload)
    assert isinstance(text, str) and '"messages"' in text


# ── name removed only for OpenCode ──────────────────────────────────────────

def test_name_removed_only_for_opencode():
    opencode = _provider(OPENCODE_BASE)
    openrouter = _provider(OPENROUTER_BASE)
    assert all("name" not in m for m in opencode.normalize_messages(_messages()))
    assert any("name" in m for m in openrouter.normalize_messages(_messages()))


# ── tool calls / tool results remain valid ──────────────────────────────────

def test_tool_calls_and_results_stay_valid():
    provider = _provider(OPENCODE_BASE)
    msgs = provider.normalize_messages(_messages())
    assistant = next(m for m in msgs if m["role"] == "assistant")
    tool = next(m for m in msgs if m["role"] == "tool")
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "read_local_file"
    assert tool["tool_call_id"] == "call_1"
    assert tool["content"] == "file body"
    json.dumps({"messages": msgs})  # whole turn still serialises


# ── x-opencode-session present on OpenCode only ─────────────────────────────

def test_opencode_session_header_present():
    provider = _provider(OPENCODE_BASE)
    headers = _headers(provider)
    assert "x-opencode-session" in headers
    assert headers["x-opencode-session"]


def test_env_supplied_session_id_is_honoured():
    os.environ["KYREX_SESSION_ID"] = "sess-abc-123"
    try:
        provider = _provider(OPENCODE_BASE)
        assert _headers(provider)["x-opencode-session"] == "sess-abc-123"
    finally:
        os.environ.pop("KYREX_SESSION_ID", None)


# ── OpenRouter unchanged ────────────────────────────────────────────────────

def test_openrouter_unchanged_no_session_header():
    provider = _provider(OPENROUTER_BASE)
    assert "x-opencode-session" not in _headers(provider)
    original = _messages()
    # Returned by identity — no mutation, no normalisation.
    assert provider.normalize_messages(original) is original
    json.dumps({"messages": provider.normalize_messages(original)})


# ── Defensive: an async normaliser must be awaited before serialisation ─────

def test_resolve_messages_awaits_a_coroutine_normaliser():
    provider = _provider(OPENCODE_BASE)

    async def _async_normalize(messages):  # the regressed shape
        return provider.normalize_messages.__wrapped__(messages) if hasattr(
            provider.normalize_messages, "__wrapped__"
        ) else [
            {k: v for k, v in m.items() if k != "name"} if isinstance(m, dict) else m
            for m in messages
        ]

    original = provider.normalize_messages
    provider.normalize_messages = _async_normalize
    try:
        resolved = asyncio.run(provider._resolve_messages(_messages()))
    finally:
        provider.normalize_messages = original

    assert not inspect.iscoroutine(resolved)
    assert isinstance(resolved, list)
    json.dumps({"messages": resolved})


if __name__ == "__main__":
    checks = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in checks:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {fn.__name__}: {type(exc).__name__}: {exc}")
    print("\n" + ("ALL TESTS PASSED" if not failures
                  else f"{failures} FAILURE(S)"))
    sys.exit(1 if failures else 0)
