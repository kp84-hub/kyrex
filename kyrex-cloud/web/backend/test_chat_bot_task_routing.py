"""Step 2 focused tests: Bot Chat -> submit_bot_task -> executor path.

Proves the wiring without a live LLM or worker process:

  1. A writable Bot-bound Chat turn routes to submit_bot_task (executor path)
     and never requests the read-only engine session.
  2. A read-only Bot-bound Chat turn keeps the read-only engine path.
  3. A Bot-bound turn rejects a workspace attach (the Bot rift is authoritative;
     the user's connected repo is never substituted).
  4. submit_bot_task -> TaskWorker -> serve.run_task uses the Bot's own rift
     writable (allowed write) and read-only Bots refuse submission (fs:write
     denial).
  5. Durable task events (start/progress/approval/completion/error) map to Chat
     control frames and terminal status.
  6. The Chat approval reply endpoint is scoped to the task's chat owner and
     never resolves another task/session.

Run: python3 -m pytest test_chat_bot_task_routing.py
"""

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-bot-task-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402  (after env setup)
import chat_service  # noqa: E402
import bots  # noqa: E402
import serve  # noqa: E402
import dev_bot  # noqa: E402
import flux  # noqa: E402
from task_store import CloudTaskStore  # noqa: E402


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
    return tempfile.mkdtemp(prefix="kyrex-bot-task-rift-")


def _register_bot(monkeypatch, tmp_path, bot_id, rift, policy):
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    # Running by default: a Bot must be started to accept new work.
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "test:model", rift,
        policy=policy, status="running",
    )


def _bot(bot_id="dev", owner="alice", policy=None, rift=None):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "anthropic:claude-test", rift or _rift_dir(),
        policy=policy, status="running", owner=owner,
    )


async def _frames(agen):
    out = []
    async for f in agen:
        out.append(f)
    return out


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


class _FakeStore:
    """Duck-typed store for terminal-framing tests (no SQLite)."""

    def __init__(self, status="done", result=None, error=None, pending=None):
        self._status = status
        self._result = result
        self._error = error
        self._pending = pending or {}

    def get_pending_approval(self, task_id):
        return dict(self._pending)

    def get(self, task_id):
        return {
            "task_id": task_id,
            "status": self._status,
            "result": self._result,
            "error": self._error,
        }

    def status(self, task_id):
        return self._status

    def request_cancel(self, task_id):
        return False


def _fake_stream_events(store, task_id, after_event_id=0, max_seconds=None):
    yield {"event_id": 1, "type": "submitted", "payload": {}, "created_at": ""}
    yield {"event_id": 2, "type": "progress",
           "payload": {"tool": "read_local_file"}, "created_at": ""}
    yield {"event_id": 3, "type": "approval_requested",
           "payload": {"tier": 1, "summary": "write file",
                       "approval_id": "apr-1"}, "created_at": ""}
    yield {"event_id": 4, "type": "approval_resolved",
           "payload": {"decision": "approved"}, "created_at": ""}
    yield {"event_id": 5, "type": "result",
           "payload": {"status": "no_changes",
                       "final_response": "did the thing"}, "created_at": ""}


class _FakeProc:
    """Minimal Popen stand-in: empty output, immediate exit."""

    def __init__(self):
        self.stdout = []
        self.stderr = []
        self.stdin = io.StringIO()
        self.returncode = 0
        self.pid = 1

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, *a, **kw):
        return 0


def _capture_popen(captured):
    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        return _FakeProc()
    return fake_popen


# ── 1-2. routing decision ──────────────────────────────────────────

def test_writable_bot_chat_routes_to_executor_not_engine(tmp_path, monkeypatch):
    bot = _bot("dev", owner="alice", policy={"fs:write": 1})
    conv = chat_service.create_conversation("alice", bot_id="dev")

    routed = []

    async def fake_stream(user, conv_, bot_, text, cid, cancel):
        routed.append((user, bot_.get("id"), text, cid))
        yield {"type": "task", "task_id": "task-1", "status": "queued"}
        yield {"type": "status", "status": "complete", "content": "done"}

    with patch.object(chat_service, "_stream_writable_bot_task",
                      side_effect=fake_stream), \
         patch.object(chat_service, "_get_engine_session",
                      side_effect=AssertionError("engine must not be requested")):
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "fix it")))

    assert routed == [("alice", "dev", "fix it", conv["conversation_id"])]
    assert any(f["type"] == "task" for f in frames)
    assert _terminal(frames)["status"] == "complete"


def test_readonly_bot_chat_keeps_engine_path(tmp_path):
    bot = _bot("qa", owner="alice", policy={})
    conv = chat_service.create_conversation("alice", bot_id="qa")

    class FakeEngine:
        def __init__(self, user, conversation_id, workspace, bot_cfg=None):
            self.workspace = Path(workspace)

        def run_turn(self, text, on_token, cancel_check=None):
            on_token("bot answer")
            return "bot answer", None

        def interrupt(self):
            pass

        def close(self):
            pass

    with patch.object(chat_service, "_get_engine_session",
                      side_effect=FakeEngine), \
         patch.object(chat_service, "_stream_writable_bot_task",
                      side_effect=AssertionError("executor must not be requested")):
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "read only")))

    assert _terminal(frames)["status"] == "complete"
    assert _terminal(frames)["content"] == "bot answer"


# ── 3. Bot rift authoritative, never the user's workspace ─────────

def test_writable_bot_turn_rejects_workspace_attach(tmp_path):
    _bot("dev", owner="alice", policy={"fs:write": 1})
    conv = chat_service.create_conversation("alice", bot_id="dev")

    with pytest.raises(chat_service.ChatUnavailable) as exc:
        asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "fix",
                                     workspace_id="default")))
    assert "workspace" in str(exc.value).lower()
    assert "bot" in str(exc.value).lower()


# ── 4. submit_bot_task -> worker -> serve.run_task scoping ────────

def test_submit_bot_task_worker_uses_bot_rift_writable(tmp_path, monkeypatch):
    rift = _rift_dir()
    _register_bot(monkeypatch, tmp_path, "dev", rift, {"fs:write": 1})
    store = CloudTaskStore(db_path=str(tmp_path / "cloud_tasks.db"))
    tid = dev_bot.submit_bot_task("alice", bots.get_bot("dev"), "fix", store=store)

    captured = {}
    with patch("serve.subprocess.Popen", _capture_popen(captured)):
        from task_store import TaskWorker
        TaskWorker(store, worker_id="w1").execute_task(store.get(tid))

    cmd = captured["cmd"]
    env = captured["env"]
    assert cmd[1].endswith("git_workflow.py")
    assert "--rift" in cmd
    assert cmd[cmd.index("--rift") + 1] == rift
    assert "--read-only" not in cmd
    assert env is not None
    assert env.get("KYREX_FS_ROOT") == rift
    assert env.get("KYREX_READ_ONLY_REPO") != "1"


def test_readonly_bot_cannot_submit_to_executor(tmp_path, monkeypatch):
    rift = _rift_dir()
    _register_bot(monkeypatch, tmp_path, "readonly", rift, {})
    store = CloudTaskStore(db_path=str(tmp_path / "cloud_tasks.db"))

    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_bot_task("alice", bots.get_bot("readonly"), "fix",
                                store=store)


def test_readonly_bound_bot_run_task_enforces_read_only(tmp_path, monkeypatch):
    rift = _rift_dir()
    _register_bot(monkeypatch, tmp_path, "readonly", rift, {})

    captured = {}
    with patch("serve.subprocess.Popen", _capture_popen(captured)):
        serve.run_task(
            "chat", None, "inspect", session_key="readonly",
            send=lambda *_args: 1, edit=lambda *_args: None,
        )

    cmd = captured["cmd"]
    env = captured["env"]
    assert "--rift" in cmd
    assert cmd[cmd.index("--rift") + 1] == rift
    assert "--read-only" in cmd
    assert env is not None
    assert env.get("KYREX_FS_ROOT") == rift
    assert env.get("KYREX_READ_ONLY_REPO") == "1"
    assert "GITHUB_TOKEN" not in env


# ── 5. executor events reach Chat (completion + error) ────────────

def test_bot_task_stream_maps_events_and_completion(tmp_path, monkeypatch):
    store = _FakeStore(
        status="done",
        result={"status": "no_changes", "final_response": "did the thing"},
        pending={"tier": 1, "summary": "write file", "detail": "", "token": ""},
    )
    monkeypatch.setattr(chat_service, "_task_store", lambda: store)
    monkeypatch.setattr(dev_bot, "submit_bot_task",
                        lambda user, bot, text, store=None, conversation_id=None: "task-1")
    monkeypatch.setattr(flux, "stream_events", _fake_stream_events)
    conv = chat_service.create_conversation("alice")

    frames = asyncio.run(_frames(
        chat_service._stream_writable_bot_task(
            "alice", conv, {"id": "dev"}, "fix",
            conv["conversation_id"], asyncio.Event())))

    types = [f["type"] for f in frames]
    assert types[0] == "conversation"
    assert any(f["type"] == "task" and f["status"] == "queued" for f in frames)
    assert any(f["type"] == "progress" for f in frames)
    assert any(f["type"] == "approval_request" for f in frames)
    assert any(f["type"] == "approval_result" for f in frames)
    term = _terminal(frames)
    assert term["status"] == "complete"
    assert term["content"] == "did the thing"

    saved = chat_service.get_conversation("alice", conv["conversation_id"])
    assert [m["role"] for m in saved["messages"]] == ["assistant"]
    assert saved["messages"][0]["content"] == "did the thing"


def test_bot_task_stream_error_reaches_chat(tmp_path, monkeypatch):
    store = _FakeStore(
        status="failed",
        result={"status": "error", "errors": ["boom"]},
    )
    monkeypatch.setattr(chat_service, "_task_store", lambda: store)
    monkeypatch.setattr(dev_bot, "submit_bot_task",
                        lambda user, bot, text, store=None, conversation_id=None: "task-1")

    def one_event(store, task_id, after_event_id=0, max_seconds=None):
        return iter([
            {"event_id": 1, "type": "submitted", "payload": {}, "created_at": ""},
        ])

    monkeypatch.setattr(flux, "stream_events", one_event)

    frames = asyncio.run(_frames(
        chat_service._stream_writable_bot_task(
            "alice", {}, {"id": "dev"}, "fix", "cid", asyncio.Event())))

    term = _terminal(frames)
    assert term["status"] == "error"
    assert "boom" in term["message"]


# ── 6. approval reply scoping ─────────────────────────────────────

def test_chat_respond_endpoint_scopes_to_chat_owner(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=str(tmp_path / "cloud_tasks.db"))
    monkeypatch.setattr(main, "store", store)

    a = store.submit(session_key="botA", task_text="fix A", repo_url=None,
                     executor_prefix="repo", bot_id="botA", chat_id="alice",
                     resolve_bot=False)
    b = store.submit(session_key="botB", task_text="fix B", repo_url=None,
                     executor_prefix="repo", bot_id="botB", chat_id="bob",
                     resolve_bot=False)
    store.persist_approval_request(a, "botA", "msg-a", 1, "", "approve A", "")
    store.persist_approval_request(b, "botB", "msg-b", 1, "", "approve B", "")

    from fastapi.testclient import TestClient
    alice = TestClient(main.app, cookies={"session": "sess-alice"})
    bob = TestClient(main.app, cookies={"session": "sess-bob"})

    r = alice.post(f"/api/task/{a}/respond", json={"text": "y"})
    assert r.status_code == 200, r.text
    assert r.json()["recorded"] is True

    # Cross-user replies are rejected (not silently misrouted).
    assert bob.post(f"/api/task/{a}/respond", json={"text": "y"}).status_code == 404
    assert alice.post(f"/api/task/{b}/respond", json={"text": "y"}).status_code == 404

    # The reply landed only on the correct task's pending approval.
    assert store.get_pending_approval(a)["operator_reply"] == "y"
    assert store.get_pending_approval(b).get("operator_reply") is None
