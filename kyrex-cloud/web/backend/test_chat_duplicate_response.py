"""Regression — Kyrex Chat: one user message, one assistant answer.

Observed: a single user message produced two identical assistant messages. The
response text was correct — it was simply committed (and rendered) twice.

Trace of the Chat SSE flow, and where the same answer was committed twice:

  * backend stream events — one engine turn streams EVERY round's content,
    separated by the engine's inter-round divider ``"\\n\\n---\\n"``
    (kyrex_engine/kyrex/core.py: ``streamer("\\n\\n---\\n")``).
  * final assistant event — the authoritative ``chat_done`` payload is those
    rounds joined with a BARE newline (``full_text = "\\n".join(
    collected_content)``), so a two-round turn whose rounds produced the same
    answer carries that answer twice. The streamed divider shape
    (``answer + "\\n\\n---\\n" + answer``) was not collapsed either: the engine
    emits no blank line after the dashes, which the old divider pattern
    required.
  * conversation persistence — ``chat_service.stream_chat`` adopts that payload
    as ``final_text`` and appends it as ONE assistant message, so the answer was
    committed twice *inside a single message* and replayed that way forever.
  * frontend streaming state / transcript finalization — the client replaces
    its accumulated deltas with the terminal ``done`` content
    (``kyrex-chat/src/lib/streaming.js``), so the doubled payload rendered as
    two identical assistant messages, before and after a reconnect, because the
    duplication was persisted.

Fix: finalization is idempotent at the shared boundary
(``chat_service.sanitize_assistant_text``), the single transform applied to the
SSE ``done`` payload, the persisted message, and replayed history. An answer
block that immediately repeats at the head of the text collapses to one copy,
so it does not matter how many engine rounds produced it, nor how many times
the message is re-finalized. No new message/event/request identity is invented:
the existing assistant message keeps its id and carries one copy of its own
answer, and delegation/status/result frames are untouched.

Run: pytest test_chat_duplicate_response.py
"""

import asyncio
import json
import os
import sys
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
# Never touch the live chat store: the regression drives real persistence.
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-dup-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import chat_api  # noqa: E402
import chat_service  # noqa: E402

# The answer used by every case below. Deliberately a plain single-line reply:
# the real duplicate was one complete answer followed by the same answer again.
ANSWER = "I am Kyrex, the coordinator Bot for this workspace."

# The two shapes the engine hands the Chat layer for the SAME two-round turn.
DONE_SHAPE = ANSWER + "\n" + ANSWER            # authoritative chat_done payload
DELTA_SHAPE = ANSWER + "\n\n---\n" + ANSWER    # streamed rounds + divider


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


def _deltas(frames):
    return [f["content"] for f in frames if f.get("type") == "delta"]


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


def _assistants(user, conversation_id):
    conv = chat_service.get_conversation(user, conversation_id)
    return [m for m in conv["messages"] if m.get("role") == "assistant"]


def _only_conversation_id(user="alice"):
    convs = chat_service.list_conversations(user)
    assert convs, "expected the turn to persist a conversation"
    return convs[0]["conversation_id"]


# A scripted NDJSON bridge speaking the REAL engine protocol. Its turn is the
# observed production shape: two rounds, each producing the SAME answer, with
# the engine's inter-round divider streamed between them and the authoritative
# chat_done payload joining the rounds with a bare newline.
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

ANSWER = "I am Kyrex, the coordinator Bot for this workspace."

while True:
    q.get()
    out({"type": "token", "content": ANSWER})
    out({"type": "token", "content": "\n\n---\n"})
    out({"type": "token", "content": ANSWER})
    out({"type": "chat_done", "content": ANSWER + "\n" + ANSWER, "reasoning": ""})
    out({"type": "phase", "value": "IDLE"})
'''


@pytest.fixture()
def engine_workspace(tmp_path, monkeypatch):
    bridge = tmp_path / "fake_dup_bridge.py"
    bridge.write_text(FAKE_BRIDGE)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.delenv("KYREX_CHAT_WORKSPACE", raising=False)
    monkeypatch.setenv("KYREX_CHAT_WORKSPACES", json.dumps({"testws": str(ws)}))
    monkeypatch.setattr(chat_service, "ENGINE_BRIDGE_PATH", str(bridge))
    yield ws


class FakeProvider:
    """Pure-chat provider double (error path)."""

    def __init__(self, error=None):
        self.error = error

    async def chat(self, model, messages, tools=None, stream_callback=None,
                   interrupt_event=None, **kw):
        if self.error:
            raise RuntimeError(self.error)
        return {"role": "assistant", "content": ""}


# ── 1. the exact duplicate-response shape collapses to one copy ─────

def test_finalization_collapses_the_exact_duplicate_response():
    """answer + "\\n" + answer must finalize to ONE copy (and stay stable)."""
    out = chat_service.sanitize_assistant_text(DONE_SHAPE)
    assert out == ANSWER
    assert out.count(ANSWER) == 1
    # Idempotent: re-finalizing the finalized text changes nothing.
    assert chat_service.sanitize_assistant_text(out) == out


def test_finalization_collapses_the_streamed_delta_shape():
    """The streamed rounds+divider shape collapses to the same one copy."""
    out = chat_service.sanitize_assistant_text(DELTA_SHAPE)
    assert out == ANSWER
    assert chat_service.sanitize_assistant_text(out) == out


def test_finalization_collapses_three_rounds_and_multiline_answers():
    three = "\n".join([ANSWER] * 3)
    assert chat_service.sanitize_assistant_text(three) == ANSWER
    multiline = "line one\nline two\nline one\nline two"
    assert chat_service.sanitize_assistant_text(multiline) == "line one\nline two"


def test_finalization_never_merges_distinct_content_or_error_text():
    distinct = "First part.\nSecond part."
    assert chat_service.sanitize_assistant_text(distinct) == distinct
    err = "[OpenAI Provider Error: upstream 500 — request failed]"
    assert chat_service.sanitize_assistant_text(err).strip() == err.strip()


# ── 2. one user message -> exactly one assistant answer (engine path) ─

def test_one_user_message_yields_exactly_one_assistant_answer(engine_workspace):
    frames = asyncio.run(_frames(chat_service.stream_chat(
        "alice", "", "what is your role?", workspace_id="testws")))

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete"
    # The final assistant event carries the answer ONCE.
    assert terminal["content"] == ANSWER
    assert terminal["content"].count(ANSWER) == 1
    assert "---" not in terminal["content"]

    conv_id = _only_conversation_id()
    conv = chat_service.get_conversation("alice", conv_id)
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assistants = _assistants("alice", conv_id)
    assert len(assistants) == 1, "one user message -> one assistant message"
    assert assistants[0]["content"] == ANSWER
    assert assistants[0]["content"].count(ANSWER) == 1, "answer committed twice"


# ── 3. streamed tokens are not appended again by the final event ─────

def test_streamed_tokens_are_not_appended_again_by_the_final_event(
        engine_workspace):
    frames = asyncio.run(_frames(chat_service.stream_chat(
        "alice", "", "what is your role?", workspace_id="testws")))
    terminal = _terminal(frames)

    # What the client accumulated from `delta` frames, put through the same
    # presentation boundary the terminal content uses.
    streamed = "".join(_deltas(frames))
    assert streamed.count(ANSWER) == 2, "the raw stream really did repeat"
    assert chat_service.sanitize_assistant_text(streamed) == terminal["content"]

    # The client REPLACES its buffer with done.content (streaming.js), so the
    # transcript ends with exactly one copy — not deltas + done.
    streamed_then_final = terminal["content"] or chat_service.sanitize_assistant_text(streamed)
    assert streamed_then_final.count(ANSWER) == 1


# ── 4. reconnect / replay does not duplicate messages ───────────────

def test_replay_of_the_turn_renders_one_copy(engine_workspace):
    asyncio.run(_frames(chat_service.stream_chat(
        "alice", "", "what is your role?", workspace_id="testws")))
    conv_id = _only_conversation_id()

    # Re-read the way the UI does on reconnect and re-finalize: still one copy,
    # one message, and stable under repetition.
    conv = chat_service.get_conversation("alice", conv_id)
    for _ in range(3):
        conv = chat_service.sanitize_conversation(
            chat_service.get_conversation("alice", conv_id))
        assistants = [m for m in conv["messages"] if m["role"] == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["content"].count(ANSWER) == 1
        assert chat_service.sanitize_assistant_text(
            assistants[0]["content"]) == assistants[0]["content"]


def test_replay_of_a_previously_stored_duplicate_renders_one_copy():
    """A conversation persisted before the fix must still render one copy."""
    conv = chat_service.create_conversation("alice", "Legacy duplicate")
    conv["messages"] = [
        {"id": "u1", "role": "user", "content": "what is your role?",
         "created_at": "t"},
        {"id": "a1", "role": "assistant", "content": DONE_SHAPE,
         "created_at": "t"},
    ]
    chat_service._write("alice", conv)

    from fastapi.testclient import TestClient
    import main

    main.sessions["sess-dup"] = "alice"
    client = TestClient(main.app, cookies={"session": "sess-dup"})
    body = client.get(f"/api/conversations/{conv['conversation_id']}").json()

    assistants = [m for m in body["messages"] if m["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == ANSWER
    assert assistants[0]["content"].count(ANSWER) == 1


# ── 5. errors are not duplicated (and never persisted as content) ────

def test_error_turn_emits_one_error_frame_and_persists_no_assistant_message():
    with patch("chat_service.get_provider", return_value=FakeProvider("boom")):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "hi")))

    terminals = [f for f in frames
                 if f.get("type") in ("status",) and f.get("status") != "complete"]
    assert len(terminals) == 1, "exactly one terminal frame"
    assert terminals[0]["status"] == "error"
    assert terminals[0]["message"].count("boom") == 1
    assert _deltas(frames) == []
    # No assistant message is recorded for a failed turn.
    convs = chat_service.list_conversations("alice")
    if convs:
        assert _assistants("alice", convs[0]["conversation_id"]) == []


# ── 6. delegation / status / result relay is unchanged ──────────────

def test_delegation_and_result_frames_pass_through_unchanged():
    """The presentation change must not touch the relay contract."""
    async def gen():
        yield {"type": "delegation",
               "delegation": {"delegation_id": "d1", "status": "running"}}
        yield {"type": "delegation_result",
               "delegation_id": "d1", "target_bot_id": "dev", "status": "done",
               "summary": "finished"}
        yield {"type": "status", "status": "complete", "content": ANSWER}

    raw = asyncio.run(_frames(chat_api._drive_stream(gen(), "r1", "c1")))
    frames = [json.loads(f[len("data: "):].strip()) for f in raw]

    assert frames[0] == {"type": "delegation",
                         "delegation": {"delegation_id": "d1",
                                        "status": "running"}}
    assert frames[1] == {"type": "delegation_result", "delegation_id": "d1",
                         "target_bot_id": "dev", "status": "done",
                         "summary": "finished"}
    assert frames[2] == {"type": "done", "content": ANSWER,
                         "conversation_id": "c1"}
    assert len(frames) == 3, "exactly one terminal event"
