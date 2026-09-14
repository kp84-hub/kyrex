"""Regression tests — Kyrex Chat conversations read like a person, not telemetry.

Pins the presentation-boundary contract added to chat_service:

  * internal engine control markers ("[Task Complete: …]", "[continue] …",
    loop-detector / circuit-breaker "[!] Task not verified complete …",
    "[!] Max recursion depth reached.") NEVER appear in rendered/persisted
    assistant text;
  * one user turn yields exactly ONE coherent assistant response, even when the
    engine internally ran multiple rounds;
  * a greeting produces one short natural bubble and no capability inventory;
  * REAL errors stay visible as errors (never sanitized away);
  * Bot-bound task execution + approval events are unchanged.

The provider is a fake driven through ``kyrex.providers.get_provider``; the
engine path uses a scripted NDJSON bridge (same technique as
test_chat_repo_aware.py) so the real service persistence + SSE bridge runs end
to end with no network and no live LLM.

Run: pytest test_chat_natural.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import chat_service  # noqa: E402

MARKERS = [
    "[Task Complete: Added the changelog entry.]",
    "[continue] Two consecutive tool-less rounds and task_complete was not called.",
    "[!] Task not verified complete — loop detected: repeating identical tool calls 3+ times. Aborting reasoning loop.",
    "[!] Task not verified complete — circuit breaker: 3 consecutive tool failures. Aborting.",
    "[!] Max recursion depth reached.",
]


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    chat_service.close_all_engine_sessions()


def setup_function():
    _reset()


def teardown_function():
    _reset()


async def _frames(agen):
    return [f async for f in agen]


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


def _deltas(frames):
    return [f["content"] for f in frames if f.get("type") == "delta"]


class FakeProvider:
    """Records the messages it was asked to answer; emits fixed tokens."""

    def __init__(self, tokens=None, content=None):
        self.tokens = tokens if tokens is not None else ["Hello", " there"]
        self.content = content
        self.seen_messages = None

    async def chat(self, model, messages, tools=None, stream_callback=None,
                   interrupt_event=None, **kw):
        self.seen_messages = messages
        for t in self.tokens:
            if stream_callback:
                stream_callback(t)
        return {"role": "assistant", "content": self.content or "".join(self.tokens)}


# ── 1. sanitizer unit contract ─────────────────────────────────────

def test_sanitize_strips_every_internal_marker():
    raw = (
        "I updated the file and the tests pass.\n"
        + "\n".join(MARKERS)
        + "\nEverything is green."
    )
    out = chat_service.sanitize_assistant_text(raw)
    for marker in MARKERS:
        assert marker not in out, f"marker leaked: {marker!r}"
    assert "I updated the file" in out
    assert "Everything is green." in out


def test_sanitize_collapses_engine_rounds_and_dedupes():
    # Two rounds with the engine's inter-round divider, then a repeated round.
    raw = "Hey there\n\n---\n\nHey there\n\nWhat can I help with?"
    out = chat_service.sanitize_assistant_text(raw)
    assert "---" not in out
    assert out.count("Hey there") == 1, "duplicate round text must collapse"
    assert "What can I help with?" in out


def test_sanitize_preserves_real_error_text():
    err = "[OpenAI Provider Error: upstream 500 — request failed]"
    assert chat_service.sanitize_assistant_text(err).strip() == err.strip()


def test_sanitize_conversation_does_not_mutate_input():
    conv = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Done.\n" + MARKERS[0]},
    ]}
    out = chat_service.sanitize_conversation(conv)
    assert MARKERS[0] in conv["messages"][1]["content"], "stored record must not mutate"
    assert MARKERS[0] not in out["messages"][1]["content"]
    assert out["messages"][0]["content"] == "hi"


# ── 2. greeting: one natural bubble, no inventory ──────────────────

def test_greeting_one_natural_bubble_and_no_inventory():
    greeting = "Hey — what would you like to work on?"
    prov = FakeProvider(content=greeting, tokens=[greeting])
    with patch("chat_service.get_provider", return_value=prov):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "hi")))

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete"
    assert terminal["content"] == greeting

    # Exactly one assistant message; exactly one terminal frame.
    convs = chat_service.list_conversations("alice")
    assert len(convs) == 1
    conv = chat_service.get_conversation("alice", convs[0]["conversation_id"])
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user", "assistant"], roles
    assert conv["messages"][1]["content"] == greeting
    terminals = [f for f in frames if f.get("type") == "status"]
    assert len(terminals) == 1


def test_greeting_system_context_forbids_inventory_and_false_claims():
    prov = FakeProvider(tokens=["Hi!"], content="Hi!")
    with patch("chat_service.get_provider", return_value=prov):
        asyncio.run(_frames(chat_service.stream_chat("alice", "", "hi")))

    system = "\n".join(
        m["content"] for m in prov.seen_messages if m["role"] == "system")
    low = system.lower()
    # Brief greeting guidance is present...
    assert "what would you like to work on" in low
    # ...and the inventory / false-claim prohibitions are explicit.
    assert "do not" in low and "capabilities" in low
    assert "tool call actually succeeded" in low


# ── 3. markers never reach rendered/persisted text (pure chat) ─────

def test_markers_never_render_in_pure_chat_output():
    # Even if a provider echoes a marker (defense in depth), the terminal
    # frame and the persisted message must be clean.
    raw = "Working on it.\n" + "\n".join(MARKERS)
    prov = FakeProvider(content=raw, tokens=[raw])
    with patch("chat_service.get_provider", return_value=prov):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "do work")))

    terminal = _terminal(frames)
    assert terminal["status"] == "complete"
    for marker in MARKERS:
        assert marker not in terminal["content"]
    convs = chat_service.list_conversations("alice")
    conv = chat_service.get_conversation("alice", convs[0]["conversation_id"])
    assert all(marker not in conv["messages"][1]["content"] for marker in MARKERS)


# ── 4. engine path: multiple rounds -> one coherent response ───────

# A scripted bridge speaking the REAL protocol. Its chat turn streams two
# rounds (with the engine's inter-round divider) and emits a chat_done whose
# content carries both rounds AND the internal markers — exactly the shape
# kyrex_engine/kyrex/core.py produces.
FAKE_BRIDGE = r'''
import json, os, sys, queue, threading

def out(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()

q = queue.Queue()
def reader():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            p = json.loads(line)
        except Exception:
            continue
        if p.get("type") == "chat":
            q.put(p)
threading.Thread(target=reader, daemon=True).start()

out({"type": "session_state", "model": "fake", "provider": "fake",
     "context": os.getcwd(), "files": {}})
out({"type": "phase", "value": "IDLE"})

while True:
    q.get()
    final = (
        "Hey there\n\n---\n\nHey there\n\n"
        "Here is the answer you asked for.\n"
        "[continue] Two consecutive tool-less rounds and task_complete was "
        "not called. If the work is finished, call task_complete now; "
        "otherwise continue with tools.\n"
        "[Task Complete: Answered the question.]"
    )
    out({"type": "token", "content": "Hey there\n\n---\n\nHey there\n\n"})
    out({"type": "token", "content": "Here is the answer you asked for.\n"})
    out({"type": "chat_done", "content": final, "reasoning": ""})
    out({"type": "phase", "value": "IDLE"})
'''


@pytest.fixture()
def engine_workspace(tmp_path, monkeypatch):
    bridge = tmp_path / "fake_bridge.py"
    bridge.write_text(FAKE_BRIDGE)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.delenv("KYREX_CHAT_WORKSPACE", raising=False)
    monkeypatch.setenv("KYREX_CHAT_WORKSPACES", json.dumps({"testws": str(ws)}))
    monkeypatch.setattr(chat_service, "ENGINE_BRIDGE_PATH", str(bridge))
    yield ws


def test_engine_rounds_combined_into_one_clean_response(engine_workspace):
    frames = asyncio.run(_frames(chat_service.stream_chat(
        "alice", "", "explain something", workspace_id="testws")))

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete"
    content = terminal["content"]

    for marker in MARKERS:
        assert marker not in content, f"marker leaked into rendered text: {marker!r}"
    assert "[continue]" not in content
    assert "---" not in content
    # Multiple engine rounds -> ONE coherent body (duplicate round collapsed).
    assert content.count("Hey there") == 1
    assert "Here is the answer you asked for." in content

    # Persisted history is clean too.
    convs = chat_service.list_conversations("alice")
    conv = chat_service.get_conversation("alice", convs[0]["conversation_id"])
    assistants = [m for m in conv["messages"] if m["role"] == "assistant"]
    assert len(assistants) == 1, "one user turn -> one assistant bubble"
    for marker in MARKERS:
        assert marker not in assistants[0]["content"]


def test_engine_markers_never_reach_sse_done_over_http(engine_workspace):
    from fastapi.testclient import TestClient
    import main

    main.sessions["sess-nat"] = "alice"
    client = TestClient(main.app, cookies={"session": "sess-nat"})

    with client.stream("POST", "/api/chat",
                       json={"message": "explain", "workspace_id": "testws",
                             "request_id": "r-nat"}) as resp:
        assert resp.status_code == 200
        frames = []
        for line in resp.iter_lines():
            if line.startswith("data:"):
                frames.append(json.loads(line[5:].strip()))

    assert frames[-1]["type"] == "done"
    done = frames[-1]["content"]
    for marker in MARKERS:
        assert marker not in done
    assert "---" not in done
    # Exactly one terminal event.
    assert len([f for f in frames if f["type"] in ("done", "error", "cancelled")]) == 1


# ── 5. GET conversation strips legacy markers ──────────────────────

def test_get_conversation_strips_markers_from_stored_history():
    from fastapi.testclient import TestClient
    import main

    conv = chat_service.create_conversation("alice", "Legacy")
    conv["messages"] = [
        {"id": "u1", "role": "user", "content": "do it", "created_at": "t"},
        {"id": "a1", "role": "assistant",
         "content": "Done.\n" + "\n".join(MARKERS), "created_at": "t"},
    ]
    chat_service._write("alice", conv)

    main.sessions["sess-get"] = "alice"
    client = TestClient(main.app, cookies={"session": "sess-get"})
    body = client.get(f"/api/conversations/{conv['conversation_id']}").json()

    assistant = [m for m in body["messages"] if m["role"] == "assistant"][0]
    for marker in MARKERS:
        assert marker not in assistant["content"]
    assert "Done." in assistant["content"]
    # The stored record itself is untouched.
    stored = chat_service.get_conversation("alice", conv["conversation_id"])
    assert MARKERS[0] in stored["messages"][1]["content"]


# ── 6. real errors stay visible ────────────────────────────────────

def test_provider_error_remains_visible():
    class ErrProvider(FakeProvider):
        async def chat(self, model, messages, tools=None, stream_callback=None,
                       interrupt_event=None, **kw):
            return {"role": "assistant",
                    "content": "[OpenAI Provider Error: upstream 500]"}

    with patch("chat_service.get_provider", return_value=ErrProvider()):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "hi")))

    terminal = _terminal(frames)
    assert terminal["status"] == "error"
    assert "Provider Error" in terminal["message"]
    # A failed turn never persists a false assistant message.
    convs = chat_service.list_conversations("alice")
    conv = chat_service.get_conversation("alice", convs[0]["conversation_id"])
    assert [m for m in conv["messages"] if m["role"] == "assistant"] == []


# ── 7. Bot-bound task + approval events unchanged ──────────────────

def test_bot_task_event_frames_unchanged():
    class Store:
        def get_pending_approval(self, task_id):
            return {"tier": 2, "summary": "Push branch", "detail": "d",
                    "token": "tok-123"}

    store = Store()
    # task lifecycle statuses
    assert chat_service._bot_task_event_frame(
        {"type": "submitted"}, store, "t1") == {
        "type": "task", "task_id": "t1", "status": "queued"}
    assert chat_service._bot_task_event_frame(
        {"type": "claimed"}, store, "t1") == {
        "type": "task", "task_id": "t1", "status": "running"}
    # approval requested carries tier/summary/token (target-owned; unchanged)
    frame = chat_service._bot_task_event_frame(
        {"type": "approval_requested",
         "payload": {"approval_id": "a1"}}, store, "t1")
    assert frame["type"] == "approval_request"
    assert frame["tier"] == 2 and frame["token"] == "tok-123"
    assert frame["summary"] == "Push branch"
    # approval resolved
    frame = chat_service._bot_task_event_frame(
        {"type": "approval_resolved", "payload": {"decision": "APPROVED"}},
        store, "t1")
    assert frame == {"type": "approval_result", "task_id": "t1",
                     "decision": "APPROVED"}
    # progress passthrough
    assert chat_service._bot_task_event_frame(
        {"type": "progress", "payload": {"x": 1}}, store, "t1") == {
        "type": "progress", "payload": {"x": 1}}


def test_writable_bot_result_text_is_sanitized():
    # serve.format_result echoes final_response, which carries the marker.
    result = {
        "status": "no_changes",
        "final_response": "Explained the fix.\n[Task Complete: Explained.]",
    }
    formatted = chat_service.serve.format_result(result)
    cleaned = chat_service.sanitize_assistant_text(formatted)
    assert "[Task Complete:" not in cleaned
    assert "Explained the fix." in cleaned


def test_writable_bot_routing_decision_unchanged():
    """Sanitization must never alter the writable-Bot routing gate."""
    dev = chat_service.dev_bot
    assert dev.is_writable_bot_policy(dev.developer_preset_policy()) is True
    assert dev.is_writable_bot_policy({"fs:read": 0}) is False
