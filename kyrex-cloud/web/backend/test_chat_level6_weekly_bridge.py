"""Chat -> Level 6 weekly MVP bridge tests.

Proves the selected-Bot route added to chat_service for the ONE pinned
``level6: weekly`` command:

    running, non-write-capable Bot holding the EXACT dedicated Level 6 Weekly
      grant
      + the byte-exact text ``level6: weekly``
      -> the pinned durable level6 task
         (dev_bot.submit_level6_task -> CloudTaskStore -> TaskWorker ->
          serve.run_task(executor_prefix="level6"))
      -> the in-process pinned capture (persistent ``browser-bot`` Browser
         Host profile) + the pinned Glofox schedule read
      -> the six-line result (or the explicit fail-closed error) relayed into
         Kyrex Chat.

Asserted negatively: the level6 path NEVER submits a writable repo task or a
browser task, and a foreign owner, a stopped Bot, a missing/partial grant, a
malformed request, a suffix/URL/date variant, and a cancelled turn all fail
closed (no writable repo submission, and — for the routing cases — no task at
all). The command NEVER falls through to the engine/LLM.

``submit_level6_task`` accepts ONLY the fixed structured request and exposes
no caller-controlled URL/date/branch/method/body/filter surface.

Run: python3 -m pytest test_chat_level6_weekly_bridge.py
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
os.environ.setdefault("WEB_SESSION_SECRET", "chat-level6-bridge-test")
os.environ.setdefault("KYREX_PROVIDER", "openai")
# Match the sibling Chat suites' server defaults so importing THIS module
# first never changes another suite's resolved model/api key.
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
import level6_weekly     # noqa: E402
import provider_profiles  # noqa: E402
import serve             # noqa: E402
import task_store        # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402

OWNER = "alice"
BOT = "weekly-bot"
_PROFILE_ID = "level6-bridge-test-profile"

_SIX_LINES = [
    "Monday 2026-09-21 — Back Squat — trainer: Ann",
    "Tuesday 2026-09-22 — Deadlift — trainer: Ann",
    "Wednesday 2026-09-23 — Clean — trainer: Bo",
    "Thursday 2026-09-24 — Snatch — trainer: Bo",
    "Friday 2026-09-25 — Front Squat — trainer: Cy",
    "Saturday 2026-09-26 — Conditioning — trainer: Cy",
]


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


def _assert_no_non_level6_submission(store):
    for sub in store.submissions:
        assert sub.get("executor_prefix") == "level6", sub


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
        "name": "Level 6 Bridge Test Profile",
        "provider": provider,
        "base_url": ("https://api.anthropic.com" if provider == "anthropic"
                     else "https://api.openai.com/v1"),
        "api_key": os.environ.get("KYREX_API_KEY") or "sk-test",
        "models": models,
    })
    return _PROFILE_ID


def _level6_bot(tmp, *, bot_id=BOT, owner=OWNER, status="running", policy=None):
    return bots.add_bot(
        bot_id, "Level 6 Weekly", "anthropic:x", str(tmp),
        owner=owner, status=status,
        policy=serve.level6_weekly_preset_policy() if policy is None else policy,
        browser_allowlist=serve.level6_weekly_preset_allowlist(),
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
# 1. authorized Bot: the exact command reaches the level6 executor, and the
#    six-line durable result is relayed into Kyrex Chat
# ═════════════════════════════════════════════════════════════════════════

def test_exact_command_routes_to_level6_and_relays_six_lines(rig, monkeypatch):
    _level6_bot(rig["tmp"])
    worker = TaskWorker(rig["raw"], worker_id="level6-bridge",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(level6_weekly, "run_weekly",
                          return_value=list(_SIX_LINES)):
            recv = chat_service.create_conversation(OWNER, bot_id=BOT)
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], serve.LEVEL6_TASK_TEXT)))
    finally:
        worker.stop()

    subs = rig["store"].submissions
    assert len(subs) == 1, subs
    sub = subs[0]
    assert sub["executor_prefix"] == "level6"
    assert sub["session_key"] == BOT
    assert sub["bot_id"] == BOT                 # owner/Bot identity retained
    assert sub["repo_url"] is None              # no repository
    # The pinned owner-facing command is validated by dev_bot; the durable task
    # carries the executor's own (prefix-stripped) request text so the
    # in-process level6 handler accepts it.
    assert sub["task_text"] == serve.LEVEL6_WEEKLY_REQUEST == "weekly"
    _assert_no_non_level6_submission(rig["store"])

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    content = terminal["content"] or ""
    assert "2026-09-21" in content
    assert "trainer: Ann" in content
    # All six lines are present.
    for line in _SIX_LINES:
        assert line in content, line
    # The result is appended to the transcript exactly once.
    assert "2026-09-21" in _last_assistant(OWNER, recv["conversation_id"])


def test_fail_closed_error_is_relayed_into_chat(rig, monkeypatch):
    _level6_bot(rig["tmp"])
    worker = TaskWorker(rig["raw"], worker_id="level6-err",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(
                level6_weekly, "run_weekly",
                side_effect=level6_weekly.Level6Error(
                    "weekly post not available")):
            recv = chat_service.create_conversation(OWNER, bot_id=BOT)
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], serve.LEVEL6_TASK_TEXT)))
    finally:
        worker.stop()

    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["executor_prefix"] == "level6"
    _assert_no_non_level6_submission(rig["store"])

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "error", frames
    message = terminal.get("message") or ""
    assert "weekly post not available" in message
    assert "no result produced by executor" not in message


# ═════════════════════════════════════════════════════════════════════════
# 2. no fallthrough: only the byte-exact command intercepts; variants stay on
#    the ordinary engine path and never submit a task
# ═════════════════════════════════════════════════════════════════════════

def test_only_byte_exact_command_intercepts(rig, monkeypatch):
    _level6_bot(rig["tmp"])
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    for text in (
        "level6: weekly 2020-01-01",             # suffix date
        "level6: weekly?url=https://evil.example/",  # alternate URL
        "level6: weekly branch=main method=POST",  # extra args
        "level6:  weekly",                        # double space
        "LEVEL6: weekly",                         # case matters (prefix/parse)
        "level6 weekly",                          # missing colon
        "level6:",                                # empty request
        "please run level6: weekly",              # not the whole message
    ):
        recv = chat_service.create_conversation(OWNER, bot_id=BOT)
        frames = asyncio.run(_frames(chat_service.stream_chat(
            OWNER, recv["conversation_id"], text)))
        terminal = _terminal(frames)
        assert terminal is not None and terminal["status"] == "complete", (
            text, frames)
    # None of the variants submitted a task; the command is the ONLY selector.
    assert rig["store"].submissions == []


def test_stripped_exact_command_still_intercepts(rig, monkeypatch):
    """Surrounding whitespace is trimmed by the route, exactly like the
    pinned glofox command — the stripped text is the ONE selector."""
    _level6_bot(rig["tmp"])
    worker = TaskWorker(rig["raw"], worker_id="level6-ws",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(level6_weekly, "run_weekly",
                          return_value=list(_SIX_LINES)):
            recv = chat_service.create_conversation(OWNER, bot_id=BOT)
            asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], "  level6: weekly  ")))
    finally:
        worker.stop()
    assert len(rig["store"].submissions) == 1
    assert rig["store"].submissions[0]["executor_prefix"] == "level6"


def test_exact_command_wins_over_browser_even_if_bound(rig, monkeypatch):
    """A Level 6 Weekly Bot that ALSO has its own Browser Host binding still
    routes the exact command to the level6 executor — it never falls through
    to the browser route."""
    _level6_bot(rig["tmp"])
    import browser_hosts as bh
    bh.enroll_host(OWNER, "alice-host", name="Alice Host",
                   allowlist=["facebook.com"])
    bh.bind_bot(OWNER, BOT, "alice-host")
    bot = bots.get_bot(BOT)
    assert dev_bot.browser_route_ready(bot) is True   # bound + allowlisted
    assert dev_bot.level6_route_ready(bot) is True
    worker = TaskWorker(rig["raw"], worker_id="level6-prio",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(level6_weekly, "run_weekly",
                          return_value=list(_SIX_LINES)):
            recv = chat_service.create_conversation(OWNER, bot_id=BOT)
            asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], serve.LEVEL6_TASK_TEXT)))
    finally:
        worker.stop()
    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["executor_prefix"] == "level6"


# ═════════════════════════════════════════════════════════════════════════
# 3. fail-closed routing cases — never a writable repo / browser submission
# ═════════════════════════════════════════════════════════════════════════

def test_foreign_owner_fails_closed(rig):
    _level6_bot(rig["tmp"])                      # owned by alice
    _write_conv_for("bob", bot_id=BOT)
    with pytest.raises(chat_service.ChatUnavailable):
        asyncio.run(_frames(chat_service.stream_chat(
            "bob", "cid-foreign", serve.LEVEL6_TASK_TEXT)))
    assert rig["store"].submissions == []


def test_foreign_direct_submission_fails_closed(rig):
    _level6_bot(rig["tmp"])                      # owned by alice
    with pytest.raises(dev_bot.DevBotError, match="another owner"):
        dev_bot.submit_level6_task("bob", bots.get_bot(BOT),
                                   serve.LEVEL6_TASK_TEXT, store=rig["store"])
    assert rig["store"].submissions == []


def test_stopped_bot_fails_closed(rig):
    _level6_bot(rig["tmp"], status="stopped")
    _write_conv_for(OWNER, bot_id=BOT)
    with pytest.raises(chat_service.ChatUnavailable):
        asyncio.run(_frames(chat_service.stream_chat(
            OWNER, "cid-foreign", serve.LEVEL6_TASK_TEXT)))
    assert rig["store"].submissions == []


def test_missing_grant_does_not_route_to_level6(rig, monkeypatch):
    """A running Bot WITHOUT the exact Level 6 Weekly grant never routes here;
    the exact command falls back to the engine (never a task)."""
    _level6_bot(rig["tmp"], policy={})
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], serve.LEVEL6_TASK_TEXT)))

    assert rig["store"].submissions == []
    assert _terminal(frames)["status"] == "complete"
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_level6_task(OWNER, bots.get_bot(BOT),
                                   serve.LEVEL6_TASK_TEXT, store=rig["store"])
    assert rig["store"].submissions == []


def test_wildcard_grant_never_counts(rig):
    """A ``browser:*`` / ``glofox:*`` / ``*`` wildcard is not the exact grant."""
    _level6_bot(rig["tmp"], policy={"browser:*": 0, "glofox:*": 0, "*": 0})
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_level6_task(OWNER, bots.get_bot(BOT),
                                   serve.LEVEL6_TASK_TEXT, store=rig["store"])
    assert rig["store"].submissions == []


def test_variants_rejected_at_submission(rig):
    """The pinned text is the ONLY accepted request; no arbitrary surface."""
    _level6_bot(rig["tmp"])                      # exact grant
    for bad in (
        "level6: weekly 2020-01-01",
        "level6: weekly?url=https://evil.example/",
        "level6: weekly branch=999 method=POST",
        "level6: filter=trainer body={}",
        "level6:",
        "glofox: schedule",
    ):
        with pytest.raises(dev_bot.DevBotError):
            dev_bot.submit_level6_task(OWNER, bots.get_bot(BOT), bad,
                                       store=rig["store"])
    assert rig["store"].submissions == []


def test_submit_level6_task_surface_is_pinned():
    """The submission accepts ONLY (user, bot, task_text, store, conv id)."""
    params = set(inspect.signature(dev_bot.submit_level6_task).parameters)
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
        dev_bot, "submit_level6_task",
        lambda user, bot, text, store=None, conversation_id=None: "l6task-1")

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
        OWNER, conv, {"id": BOT}, serve.LEVEL6_TASK_TEXT,
        conv["conversation_id"], cancel, mode="level6")))

    assert fake.cancelled == ["l6task-1"]
    assert _terminal(frames)["status"] == "cancelled"
    assert rig["store"].submissions == []


# ═════════════════════════════════════════════════════════════════════════
# 4. route readiness — never repo, never browser-forced; exact grant only
# ═════════════════════════════════════════════════════════════════════════

def test_level6_route_ready_predicate():
    policy = serve.level6_weekly_preset_policy()
    running = {"id": BOT, "owner": OWNER, "status": "running", "policy": policy}
    assert dev_bot.level6_route_ready(running) is True
    assert dev_bot.level6_route_ready(dict(running, status="stopped")) is False
    assert dev_bot.level6_route_ready(dict(running, policy={})) is False
    assert dev_bot.level6_route_ready(
        dict(running, policy=dict(policy, **{"fs:write": 1}))) is False


def test_level6_grant_is_exactly_four_ops_and_is_its_own_preset():
    policy = serve.level6_weekly_preset_policy()
    assert policy == {
        "browser:navigate": 0,
        "browser:read": 0,
        "browser:screenshot": 0,
        "glofox:read": 0,
    }
    assert serve.level6_weekly_granted(policy) is True
    # Distinct from the Glofox Reader and Browser presets (neither widened).
    assert serve.is_glofox_reader_policy(policy) is False
    assert serve.is_browser_bot_policy(policy) is False
    assert serve.level6_weekly_granted(
        serve.glofox_reader_preset_policy()) is False
    assert serve.level6_weekly_granted(serve.browser_preset_policy()) is False
    assert serve.is_writable_bot_policy(policy) is False
    # The preset's browser allowlist is exactly the pinned Facebook host.
    assert serve.level6_weekly_preset_allowlist() == ["facebook.com"]
