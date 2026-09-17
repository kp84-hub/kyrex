"""Chat -> Level 6 Glofox reader bridge tests.

Proves the selected-Bot route added to chat_service for the ONE pinned
Glofox command:

    running, non-write-capable Bot + the EXACT ``glofox:read`` grant
      + the exact text ``glofox: schedule``
      -> the pinned durable glofox task
         (dev_bot.submit_glofox_task -> CloudTaskStore -> TaskWorker ->
          serve.run_task(executor_prefix="glofox"))
      -> the in-process read-only connector (glofox_api.week_0830_classes)

Asserted negatively: the glofox path NEVER submits a writable repo task, and
a foreign owner, a stopped Bot, a missing grant, a malformed request, a
cancelled turn, and a connector error all fail closed (no writable repo
submission, and — for the routing cases — no task at all).

``submit_glofox_task`` accepts ONLY the fixed structured request and exposes
no caller-controlled URL/date/branch/method/body/filter surface.

Run: python3 -m pytest test_chat_glofox_bridge.py
"""

import asyncio
import inspect
import os
import sys
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("WEB_SESSION_SECRET", "chat-glofox-bridge-test")
os.environ.setdefault("KYREX_PROVIDER", "openai")
# Match the sibling Chat suites' server defaults so importing THIS module
# first never changes another suite's resolved model/api key (the env is
# process-global and `setdefault` is first-writer-wins).
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))      # web/backend
_ROOT = os.path.dirname(os.path.dirname(_BACKEND))         # kyrex-cloud/
for _p in (_BACKEND, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bots              # noqa: E402
import chat_service      # noqa: E402
import dev_bot           # noqa: E402
import flux              # noqa: E402
import glofox_api        # noqa: E402
import provider_profiles  # noqa: E402
import serve             # noqa: E402
import task_store        # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402

OWNER = "alice"
BOT = "level6"
_PROFILE_ID = "glofox-bridge-test-profile"

_OK_ROWS = [
    {"date": "2026-09-21", "class_name": "Level 6 Training",
     "trainer_id": "t1", "trainer_name": "Ann", "event_id": "M1"},
    {"date": "2026-09-26", "class_name": "Level 6 Training",
     "trainer_id": "t1", "trainer_name": "Ann", "event_id": "S6"},
]

_NO_REPO = "repo"


async def _frames(agen):
    return [frame async for frame in agen]


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


def _last_assistant(user, conversation_id):
    conv = chat_service.get_conversation(user, conversation_id) or {}
    msgs = [m.get("content", "") for m in conv.get("messages", [])
            if m.get("role") == "assistant"]
    return msgs[-1] if msgs else ""


def _write_conv_for(user, *, bot_id=None, cid="cid-foreign"):
    now = chat_service._now_iso()
    conv = {
        "conversation_id": cid,
        "title": "t",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    if bot_id:
        conv["bot_id"] = bot_id
    chat_service._write(user, conv)
    return conv


def _assert_no_repo_submission(store):
    for sub in store.submissions:
        assert sub.get("executor_prefix") != _NO_REPO, sub


class _RecordingStore:
    """Wraps a CloudTaskStore, recording every submit before delegating."""

    def __init__(self, store):
        self._store = store
        self.submissions = []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        return self._store.submit(**kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """One isolated environment per test: registry, data dir, task store."""
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    # serve's glofox path opens its OWN CloudTaskStore() (the DATA_DIR-driven
    # default path). Point DATA_DIR at this test's dir so both connections
    # resolve to the SAME sqlite file.
    monkeypatch.setattr(task_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    raw = CloudTaskStore()                       # DATA_DIR/cloud_tasks.db
    store = _RecordingStore(raw)
    monkeypatch.setattr(chat_service, "_task_store_instance", store,
                        raising=False)
    chat_service._engine_sessions.clear()
    yield {"store": store, "raw": raw, "tmp": tmp_path}
    chat_service._engine_sessions.clear()


def _ensure_profile(owner, model):
    provider = "anthropic" if str(model).startswith("anthropic") else "openai"
    bare = str(model).split(":", 1)[1] if ":" in str(model) else str(model)
    existing = provider_profiles.get_profile(owner, _PROFILE_ID)
    models = list(existing["models"]) if existing else []
    if bare not in models:
        models.append(bare)
    provider_profiles.save_profile(owner, {
        "id": _PROFILE_ID,
        "name": "Glofox Bridge Test Profile",
        "provider": provider,
        "base_url": ("https://api.anthropic.com" if provider == "anthropic"
                     else "https://api.openai.com/v1"),
        "api_key": os.environ.get("KYREX_API_KEY") or "sk-test",
        "models": models,
    })
    return _PROFILE_ID


def _glofox_bot(tmp, *, bot_id=BOT, owner=OWNER, status="running", policy=None):
    return bots.add_bot(
        bot_id, "Level 6 Reader", "anthropic:x", str(tmp),
        owner=owner, status=status,
        policy={"glofox:read": 0} if policy is None else policy,
        provider_profile_id=_ensure_profile(owner, "anthropic:x"))


class _RecordingEngine:
    def __init__(self, user, conversation_id, workspace, bot_cfg=None):
        self.workspace = workspace

    def run_turn(self, text, on_token, cancel_check=None):
        on_token("bot answer")
        return "bot answer", None

    def interrupt(self):
        pass

    def close(self):
        pass


# ═════════════════════════════════════════════════════════════════════════
# 1. authorized Bot: the Chat command reaches the connector, end to end
# ═════════════════════════════════════════════════════════════════════════

def test_authorized_glofox_command_reaches_connector(rig, monkeypatch):
    _glofox_bot(rig["tmp"])
    worker = TaskWorker(rig["raw"], worker_id="glofox-bridge",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(glofox_api, "week_0830_classes",
                          return_value=_OK_ROWS):
            recv = chat_service.create_conversation(OWNER, bot_id=BOT)
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], serve.GLOFOX_TASK_TEXT)))
    finally:
        worker.stop()

    subs = rig["store"].submissions
    assert len(subs) == 1, subs
    sub = subs[0]
    assert sub["executor_prefix"] == "glofox"
    assert sub["session_key"] == BOT
    assert sub["bot_id"] == BOT                # owner/Bot identity retained
    assert sub["repo_url"] is None             # no repository
    assert sub["task_text"] == serve.GLOFOX_TASK_TEXT
    _assert_no_repo_submission(rig["store"])

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    assert "2026-09-21" in (terminal["content"] or "")
    assert "Ann" in (terminal["content"] or "")
    # Result appended to the transcript exactly once.
    assert "2026-09-21" in _last_assistant(OWNER, recv["conversation_id"])


# ═════════════════════════════════════════════════════════════════════════
# 2. fail-closed cases — never a writable repo submission
# ═════════════════════════════════════════════════════════════════════════

def test_foreign_owner_fails_closed(rig):
    """A conversation bound to another owner's Bot cannot run the command."""
    _glofox_bot(rig["tmp"])                      # owned by alice
    _write_conv_for("bob", bot_id=BOT)
    with pytest.raises(chat_service.ChatUnavailable):
        asyncio.run(_frames(chat_service.stream_chat(
            "bob", "cid-foreign", serve.GLOFOX_TASK_TEXT)))
    assert rig["store"].submissions == []


def test_foreign_direct_submission_fails_closed(rig):
    """Even a direct call refuses a Bot owned by someone else."""
    _glofox_bot(rig["tmp"])                      # owned by alice
    with pytest.raises(dev_bot.DevBotError, match="another owner"):
        dev_bot.submit_glofox_task("bob", bots.get_bot(BOT),
                                   serve.GLOFOX_TASK_TEXT, store=rig["store"])
    assert rig["store"].submissions == []


def test_stopped_bot_fails_closed(rig):
    _glofox_bot(rig["tmp"], status="stopped")
    _write_conv_for(OWNER, bot_id=BOT)
    with pytest.raises(chat_service.ChatUnavailable):
        asyncio.run(_frames(chat_service.stream_chat(
            OWNER, "cid-foreign", serve.GLOFOX_TASK_TEXT)))
    assert rig["store"].submissions == []


def test_missing_grant_does_not_route_to_glofox(rig, monkeypatch):
    """A running Bot WITHOUT the exact glofox:read grant never routes here."""
    _glofox_bot(rig["tmp"], policy={})
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], serve.GLOFOX_TASK_TEXT)))

    assert rig["store"].submissions == []
    assert _terminal(frames)["status"] == "complete"
    # The direct submission also fails closed with no task written.
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_glofox_task(OWNER, bots.get_bot(BOT),
                                   serve.GLOFOX_TASK_TEXT, store=rig["store"])
    assert rig["store"].submissions == []


def test_wildcard_grant_never_counts(rig):
    """A ``glofox:*`` / ``*`` wildcard is not the exact grant."""
    _glofox_bot(rig["tmp"], policy={"glofox:*": 0, "*": 0})
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_glofox_task(OWNER, bots.get_bot(BOT),
                                   serve.GLOFOX_TASK_TEXT, store=rig["store"])
    assert rig["store"].submissions == []


def test_malformed_request_not_accepted(rig, monkeypatch):
    """The pinged text is the ONLY accepted request; no arbitrary surface."""
    _glofox_bot(rig["tmp"])                      # exact grant
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], "glofox: delete branches --all")))

    assert rig["store"].submissions == []        # not the pinned text -> engine
    assert _terminal(frames)["status"] == "complete"

    for bad in (
        "glofox: schedule 2020-01-01",
        "glofox: schedule?url=https://evil.example/",
        "glofox: schedule branch=999 method=POST",
        "glofox: filter=trainer body={}",
        "glofox:",
    ):
        with pytest.raises(dev_bot.DevBotError):
            dev_bot.submit_glofox_task(OWNER, bots.get_bot(BOT), bad,
                                       store=rig["store"])
    assert rig["store"].submissions == []


def test_submit_glofox_task_surface_is_pinned():
    """The submission accepts ONLY (user, bot, task_text, store, conv id)."""
    params = set(inspect.signature(dev_bot.submit_glofox_task).parameters)
    assert params == {"user", "bot", "task_text", "store", "conversation_id"}
    for forbidden in ("url", "date", "branch", "method", "body", "filter",
                      "steps", "repo_url", "executor_prefix", "policy"):
        assert forbidden not in params


def test_cancelled_turn_fails_closed(rig, monkeypatch):
    class _FakeStore:
        def __init__(self):
            self.cancelled = []

        def get_pending_approval(self, task_id):
            return {}

        def get(self, task_id):
            return {"task_id": task_id, "status": "running",
                    "result": None, "error": None}

        def request_cancel(self, task_id):
            self.cancelled.append(task_id)
            return True

    fake = _FakeStore()
    monkeypatch.setattr(chat_service, "_task_store", lambda: fake)
    monkeypatch.setattr(
        dev_bot, "submit_glofox_task",
        lambda user, bot, text, store=None, conversation_id=None: "gtask-1")

    def slow_events(store, task_id, after_event_id=0, max_seconds=None):
        yield {"event_id": 1, "type": "submitted", "payload": {},
               "created_at": ""}
        import time
        time.sleep(2.0)                          # never terminal in-window

    monkeypatch.setattr(flux, "stream_events", slow_events)

    cancel = asyncio.Event()
    cancel.set()
    conv = chat_service.create_conversation(OWNER)
    frames = asyncio.run(_frames(chat_service._stream_writable_bot_task(
        OWNER, conv, {"id": BOT}, serve.GLOFOX_TASK_TEXT,
        conv["conversation_id"], cancel, mode="glofox")))

    assert fake.cancelled == ["gtask-1"]
    assert _terminal(frames)["status"] == "cancelled"
    assert rig["store"].submissions == []


def test_connector_error_fails_closed(rig):
    _glofox_bot(rig["tmp"])
    worker = TaskWorker(rig["raw"], worker_id="glofox-err",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(
                glofox_api, "week_0830_classes",
                side_effect=glofox_api.GlofoxTransportError(
                    "HTTP 503 from api.glofox.com")):
            recv = chat_service.create_conversation(OWNER, bot_id=BOT)
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], serve.GLOFOX_TASK_TEXT)))
    finally:
        worker.stop()

    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["executor_prefix"] == "glofox"
    _assert_no_repo_submission(rig["store"])

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "error", frames
    message = terminal.get("message") or ""
    assert "HTTP 503" in message
    assert "no result produced by executor" not in message
