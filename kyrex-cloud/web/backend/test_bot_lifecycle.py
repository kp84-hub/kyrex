"""Bot lifecycle controls in Kyrex Chat.

A Bot's ``status`` is a work-eligibility LABEL on Kyrex's shared worker — it
never starts or stops a separate process. These tests prove the semantics and
that they are enforced SERVER-SIDE (never by the UI alone):

  * ``running`` — eligible for new Bot-bound conversations and task submissions
  * ``paused``  — no new turns/tasks (already-accepted work is not interrupted)
  * ``stopped`` — no new turns/tasks (already-accepted work is not interrupted)

Coverage:
  1.  stopped/paused Bots reject a NEW conversation binding
  2.  stopped/paused Bots reject a NEW chat turn on an existing conversation
  3.  Start (running) makes a Bot usable again (chat turn + task submission)
  4.  stopped/paused Bots reject writable task submission (submit_bot_task)
  5.  non-owners cannot alter status (403); anonymous callers get 401
  6.  a status change does not affect another Bot
  7.  a status change does not cancel/interfere with an already-submitted task
  8.  the status response makes no claim that a separate process was launched
  9.  a stopped Bot is still DISCOVERABLE (so the owner can start it)

Run: python3 -m pytest test_bot_lifecycle.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bot-lifecycle-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402  (after env setup; seeds the shared app/session map)
import chat_service  # noqa: E402
import bots  # noqa: E402  — the authoritative registry under test
import dev_bot  # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    bots.save_bots({})
    chat_service._engine_sessions.clear()


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()


def _rift_dir() -> str:
    return tempfile.mkdtemp(prefix="kyrex-bot-lifecycle-rift-")


def _bot(bot_id="qa", owner="alice", status="stopped", policy=None, rift=None):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "anthropic:claude-test",
        rift or _rift_dir(), policy=policy, status=status, owner=owner,
    )


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


async def _frames(agen):
    out = []
    async for f in agen:
        out.append(f)
    return out


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


class _FakeEngine:
    """EngineSession stand-in that answers one turn (no process spawned)."""

    def __init__(self, user, conversation_id, workspace, bot_cfg=None):
        self.workspace = Path(workspace)

    def run_turn(self, text, on_token, cancel_check=None):
        on_token("bot answer")
        return "bot answer", None

    def interrupt(self):
        pass

    def close(self):
        pass


def _must_not_request_engine(*args, **kwargs):
    raise AssertionError(
        "a non-running Bot must never reach the engine session")


def _fake_executor(**kwargs):
    """TaskWorker executor stub: reports a successful (no-change) result."""
    kwargs["on_result"]({"status": "no_changes", "final_response": "done"})


# ── 1. stopped/paused reject a NEW binding ─────────────────────────

@pytest.mark.parametrize("status", ["stopped", "paused"])
def test_non_running_bot_rejects_new_conversation_binding(status):
    _bot("qa", owner="alice", status=status)

    r = _client("alice").post("/api/conversations", json={"bot_id": "qa"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"].lower()
    assert status in detail and "start" in detail, detail

    with pytest.raises(chat_service.BotUnavailable):
        chat_service.create_conversation("alice", bot_id="qa")
    # Nothing was created.
    assert chat_service.list_conversations("alice") == []


# ── 2. stopped/paused reject a NEW turn ────────────────────────────

@pytest.mark.parametrize("status", ["stopped", "paused"])
def test_non_running_bot_rejects_new_chat_turn(status):
    # Bind while running, then transition away.
    _bot("qa", owner="alice", status="running")
    conv = chat_service.create_conversation("alice", bot_id="qa")
    bots.set_status("qa", status)

    with patch("chat_service._get_engine_session", new=_must_not_request_engine):
        with pytest.raises(chat_service.ChatUnavailable) as exc:
            asyncio.run(_frames(
                chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    msg = str(exc.value).lower()
    assert status in msg and "start" in msg, msg
    # The binding is preserved; nothing was persisted for the rejected turn.
    stored = chat_service.get_conversation("alice", conv["conversation_id"])
    assert stored["bot_id"] == "qa"
    assert stored["messages"] == []


# ── 3. Start makes a Bot usable ────────────────────────────────────

def test_start_makes_bot_usable_for_chat_and_tasks():
    _bot("qa", owner="alice", status="stopped")
    _bot("dev", owner="alice", status="stopped", policy={"fs:write": 1})

    # While stopped: binding is refused.
    assert _client("alice").post(
        "/api/conversations", json={"bot_id": "qa"}).status_code == 400

    # Start them.
    assert _client("alice").patch(
        "/api/bots/qa", json={"status": "running"}).status_code == 200
    assert _client("alice").patch(
        "/api/bots/dev", json={"status": "running"}).status_code == 200

    # Chat turn now works.
    conv = chat_service.create_conversation("alice", bot_id="qa")
    with patch.object(chat_service, "_get_engine_session", _FakeEngine):
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "hi")))
    assert _terminal(frames)["status"] == "complete"

    # Task submission now works.
    store = CloudTaskStore(db_path=tempfile.mktemp(suffix=".db"))
    tid = dev_bot.submit_bot_task(
        "alice", bots.get_bot("dev"), "fix it", store=store)
    assert store.get(tid)["bot_id"] == "dev"


# ── 4. stopped/paused reject writable task submission ──────────────

@pytest.mark.parametrize("status", ["stopped", "paused"])
def test_non_running_bot_rejects_writable_task_submission(status):
    _bot("dev", owner="alice", status="running", policy={"fs:write": 1})
    store = CloudTaskStore(db_path=tempfile.mktemp(suffix=".db"))

    # Accepted while running...
    tid = dev_bot.submit_bot_task(
        "alice", bots.get_bot("dev"), "do it", store=store)
    assert store.get(tid) is not None

    # ...rejected once the Bot is no longer running.
    bots.set_status("dev", status)
    with pytest.raises(dev_bot.DevBotError) as exc:
        dev_bot.submit_bot_task(
            "alice", bots.get_bot("dev"), "again", store=store)
    msg = str(exc.value).lower()
    assert status in msg and "start" in msg, msg
    # Exactly one task exists — the rejected submission created nothing.
    assert len(store.list_tasks()) == 1


# ── 5. ownership ───────────────────────────────────────────────────

def test_non_owner_cannot_alter_status():
    _bot("owned", owner="alice", status="stopped")
    _bot("bobs", owner="bob", status="running")
    _bot("opbot", owner="", status="running")  # operator-created: not manageable

    c = _client("alice")
    assert c.patch("/api/bots/bobs", json={"status": "paused"}).status_code == 403
    assert c.patch("/api/bots/opbot", json={"status": "paused"}).status_code == 403
    # Neither Bot was mutated.
    assert bots.get_bot("bobs")["status"] == "running"
    assert bots.get_bot("opbot")["status"] == "running"

    # The owner CAN change their own Bot.
    ok = c.patch("/api/bots/owned", json={"status": "running"})
    assert ok.status_code == 200 and ok.json()["status"] == "running"

    # Anonymous callers are rejected outright.
    from fastapi.testclient import TestClient
    assert TestClient(main.app).patch(
        "/api/bots/owned", json={"status": "stopped"}).status_code == 401


# ── 6. isolation: one Bot's status never affects another ───────────

def test_status_change_does_not_affect_another_bot():
    _bot("a", owner="alice", status="stopped")
    _bot("b", owner="alice", status="running")
    c = _client("alice")

    assert c.patch("/api/bots/a", json={"status": "running"}).status_code == 200
    assert bots.get_bot("a")["status"] == "running"
    assert bots.get_bot("b")["status"] == "running"   # untouched

    assert c.patch("/api/bots/b", json={"status": "stopped"}).status_code == 200
    assert bots.get_bot("b")["status"] == "stopped"
    assert bots.get_bot("a")["status"] == "running"   # still untouched


# ── 7. status changes do not disturb already-submitted work ────────

def test_status_change_does_not_cancel_already_submitted_task():
    _bot("dev", owner="alice", status="running", policy={"fs:write": 1})
    store = CloudTaskStore(db_path=tempfile.mktemp(suffix=".db"))
    tid = dev_bot.submit_bot_task(
        "alice", bots.get_bot("dev"), "fix it", store=store)

    # The Bot is stopped AFTER the task was accepted.
    bots.set_status("dev", "stopped")

    # The worker does not consult Bot status at claim/execute time: it runs the
    # durable task to completion regardless.
    TaskWorker(store, worker_id="w1", executor=_fake_executor).execute_task(
        store.get(tid))
    assert store.status(tid) == "done"


# ── 8. no claim that a separate process was launched ───────────────

def test_status_response_makes_no_process_launch_claim():
    _bot("owned", owner="alice", status="stopped")
    body = _client("alice").patch(
        "/api/bots/owned", json={"status": "running"}).json()

    # The response is pure lifecycle metadata...
    assert set(body.keys()) == {
        "id", "name", "status", "model", "available", "manageable", "claimable"}
    assert body["status"] == "running"
    assert body["manageable"] is True and body["claimable"] is False
    # ...and carries no process/daemon/pid notion of any kind.
    serialized = str(body).lower()
    for banned in ("pid", "process", "daemon", "launched", "started"):
        assert banned not in serialized, banned


# ── 9. stopped Bots stay discoverable (so the UI can offer Start) ──

def test_stopped_bot_is_still_discoverable():
    _bot("qa", owner="alice", status="stopped")
    r = _client("alice").get("/api/bots")
    assert r.status_code == 200, r.text
    qa = next(b for b in r.json()["bots"] if b["id"] == "qa")
    assert qa["status"] == "stopped"
    assert qa["manageable"] is True
