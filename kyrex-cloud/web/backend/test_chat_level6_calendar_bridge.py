"""Chat -> Level 6 Calendar bridge tests.

Proves the selected-Bot route added to chat_service for the ONE pinned
``level6: calendar`` command:

    running, non-write-capable Bot holding the EXACT dedicated Level 6
      Calendar grant
      + the byte-exact text ``level6: calendar``
      -> the pinned durable level6 task
         (dev_bot.submit_level6_calendar_task -> CloudTaskStore ->
          TaskWorker -> serve.run_task(executor_prefix="level6"))
      -> the in-process deterministic Monday-Saturday week from the OWNER's
         primary Google Calendar (owner-scoped encrypted connector store)
         + the pinned Glofox trusted-date read
      -> the six-line result (or the explicit fail-closed error) relayed
         into Kyrex Chat.

Asserted negatively: the level6 path NEVER submits a writable repo task, a
browser task, or a calendar-reader task; a foreign owner, a stopped Bot, a
missing/partial grant, a malformed request, and suffix/URL/date variants all
fail closed (no writable repo submission, and — for the routing cases — no
task at all). The command NEVER falls through to the engine/LLM.

One end-to-end test drives the REAL handler: the connector read is faked at
the serve seam (``serve._level6_calendar_read_events``) with exactly the six
all-day contract events, the Glofox trusted-date read is faked at the real
seam (``glofox_api._week_0830_classes_for_dates``), and the week selection
seam is pinned to a deterministic Monday-Saturday — proving the real
contract validation + exact-date join produce the six readable lines.

``submit_level6_calendar_task`` accepts ONLY the fixed structured request
and exposes no caller-controlled URL/date/branch/method/body/filter surface.

Run: python3 -m pytest test_chat_level6_calendar_bridge.py
"""

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("WEB_SESSION_SECRET", "chat-l6cal-bridge-test")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bots              # noqa: E402
import chat_service      # noqa: E402
import dev_bot           # noqa: E402
import glofox_api        # noqa: E402
import level6_calendar   # noqa: E402
import provider_profiles  # noqa: E402
import serve             # noqa: E402
import task_store        # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402

OWNER = "alice"
BOT = "l6cal-bot"
_PROFILE_ID = "l6cal-bridge-test-profile"

WEEK_ISO = ["2026-09-21", "2026-09-22", "2026-09-23",
            "2026-09-24", "2026-09-25", "2026-09-26"]

_SIX_LINES = [
    "Monday 2026-09-21 — Back Squat — trainer: Ann",
    "Tuesday 2026-09-22 — Deadlift — trainer: Ann",
    "Wednesday 2026-09-23 — Clean — trainer: Bo",
    "Thursday 2026-09-24 — Snatch — trainer: Bo",
    "Friday 2026-09-25 — Front Squat — trainer: Cy",
    "Saturday 2026-09-26 — Conditioning — trainer: Cy",
]

_SIX_EVENTS = [
    {"start": {"date": d}, "summary": title}
    for d, title in zip(
        WEEK_ISO,
        ["Level 6 Workout: Back Squat", "Level 6 Workout: Deadlift",
         "Level 6 Workout: Clean", "Level 6 Workout: Snatch",
         "Level 6 Workout: Front Squat", "Level 6 Workout: Conditioning"])
]

_SIX_ROWS = [
    {"date": d, "class_name": "Group Fitness Class", "trainer_id": f"t{i}",
     "trainer_name": name, "event_id": f"e{i}"}
    for i, (d, name) in enumerate(zip(
        WEEK_ISO, ["Ann", "Ann", "Bo", "Bo", "Cy", "Cy"]))
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
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    raw = CloudTaskStore()
    store = _RecordingStore(raw)
    monkeypatch.setattr(chat_service, "_task_store_instance", store,
                        raising=False)
    chat_service._engine_sessions.clear()
    yield {"store": store, "raw": raw, "tmp": tmp_path}
    chat_service._engine_sessions.clear()


def _ensure_profile(owner):
    existing = provider_profiles.get_profile(owner, _PROFILE_ID)
    models = list(existing["models"]) if existing else ["x"]
    if "x" not in models:
        models.append("x")
    provider_profiles.save_profile(owner, {
        "id": _PROFILE_ID,
        "name": "Level 6 Calendar Bridge Test Profile",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": os.environ.get("KYREX_API_KEY") or "sk-test",
        "models": models,
    })
    return _PROFILE_ID


def _l6cal_bot(tmp, *, bot_id=BOT, owner=OWNER, status="running",
               policy=None):
    return bots.add_bot(
        bot_id, "Level 6 Calendar", "openai:x", str(tmp),
        owner=owner, status=status,
        policy=serve.level6_calendar_preset_policy() if policy is None
        else policy,
        browser_allowlist=[],
        provider_profile_id=_ensure_profile(owner))


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


# ═════════════════════════════════════════════════════════════════════
# 1. exact command -> level6 executor -> six durable lines into Chat
# ═════════════════════════════════════════════════════════════════════

def test_exact_command_routes_to_level6_and_relays_six_lines(rig, monkeypatch):
    _l6cal_bot(rig["tmp"])
    worker = TaskWorker(rig["raw"], worker_id="l6cal-bridge",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(level6_calendar, "run_calendar_week",
                          return_value=list(_SIX_LINES)):
            recv = chat_service.create_conversation(OWNER, bot_id=BOT)
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], serve.LEVEL6_CALENDAR_TASK_TEXT)))
    finally:
        worker.stop()

    subs = rig["store"].submissions
    assert len(subs) == 1, subs
    sub = subs[0]
    assert sub["executor_prefix"] == "level6"
    assert sub["session_key"] == BOT
    assert sub["bot_id"] == BOT                 # owner/Bot identity retained
    assert sub["repo_url"] is None              # no repository
    assert sub["task_text"] == serve.LEVEL6_CALENDAR_REQUEST == "calendar"
    _assert_no_non_level6_submission(rig["store"])

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    content = terminal["content"] or ""
    assert "WORKOUT WEEK" in content
    assert "trainer: Ann" in content
    for line in _SIX_LINES:
        assert line in content, line
    assert "2026-09-21" in _last_assistant(OWNER, recv["conversation_id"])


def test_real_handler_validates_contract_and_joins_exact_dates(rig, monkeypatch):
    """End-to-end through the REAL serve handler: the faked connector read
    returns exactly the six all-day contract events, the faked Glofox seam
    receives EXACTLY those six dates, and the six readable lines come back."""
    _l6cal_bot(rig["tmp"])
    worker = TaskWorker(rig["raw"], worker_id="l6cal-real",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    seen_dates = []
    try:
        with patch.object(level6_calendar, "select_week_dates",
                          return_value=tuple(__import__("datetime").date(
                              2026, 9, 21) + __import__("datetime").timedelta(
                                  days=i) for i in range(6))), \
                patch.object(serve, "_level6_calendar_read_events",
                             lambda owner, tmin, tmax: _SIX_EVENTS), \
                patch.object(glofox_api, "_week_0830_classes_for_dates",
                             lambda dates: seen_dates.append(list(dates))
                             or _SIX_ROWS):
            recv = chat_service.create_conversation(OWNER, bot_id=BOT)
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], "level6: calendar")))
    finally:
        worker.stop()

    assert seen_dates and seen_dates[0] == WEEK_ISO, seen_dates
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    for line in _SIX_LINES:
        assert line in (terminal["content"] or ""), line


def test_fail_closed_error_is_relayed_into_chat(rig, monkeypatch):
    _l6cal_bot(rig["tmp"])
    worker = TaskWorker(rig["raw"], worker_id="l6cal-err",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(
                level6_calendar, "run_calendar_week",
                side_effect=level6_calendar.Level6CalendarError(
                    "missing workout event on 2026-09-21")):
            recv = chat_service.create_conversation(OWNER, bot_id=BOT)
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], "level6: calendar")))
    finally:
        worker.stop()

    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["executor_prefix"] == "level6"
    _assert_no_non_level6_submission(rig["store"])
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "error", frames
    message = terminal.get("message") or ""
    assert "missing workout event on 2026-09-21" in message
    assert "no result produced by executor" not in message


# ═════════════════════════════════════════════════════════════════════
# 2. no fallthrough: only the byte-exact command intercepts
# ═════════════════════════════════════════════════════════════════════

def test_only_byte_exact_command_intercepts(rig, monkeypatch):
    _l6cal_bot(rig["tmp"])
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    for text in (
        "level6: calendar 2020-01-01",            # suffix date
        "level6: calendar?url=https://evil.example/",  # alternate URL
        "level6: calendar branch=main",           # extra args
        "level6:  calendar",                      # double space (stripped)
        "LEVEL6: calendar",                       # case variant (no route)
        "level6: weekly",                         # the OTHER command
        "level6 calendar",                        # missing colon
        "level6:",                                # empty request
        "please run level6: calendar",            # not the whole message
        # NOTE: "calendar: week" is deliberately NOT here — the Level 6
        # Calendar Bot grants cal:list, so the byte-exact Calendar Reader
        # commands still route through the EXISTING calendar path (unchanged
        # behaviour, covered by the Calendar Reader suites).
    ):
        recv = chat_service.create_conversation(OWNER, bot_id=BOT)
        frames = asyncio.run(_frames(chat_service.stream_chat(
            OWNER, recv["conversation_id"], text)))
        terminal = _terminal(frames)
        assert terminal is not None and terminal["status"] == "complete", (
            text, frames)
    # None of the variants submitted a level6: calendar task.
    for sub in rig["store"].submissions:
        assert sub.get("executor_prefix") != "level6" \
            or sub.get("task_text") != "calendar", sub


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


def test_foreign_owner_fails_closed(rig):
    _l6cal_bot(rig["tmp"])                      # owned by alice
    _write_conv_for("bob", bot_id=BOT)
    with pytest.raises(chat_service.ChatUnavailable):
        asyncio.run(_frames(chat_service.stream_chat(
            "bob", "cid-foreign", "level6: calendar")))
    assert rig["store"].submissions == []


def test_foreign_direct_submission_fails_closed(rig):
    _l6cal_bot(rig["tmp"])                      # owned by alice
    with pytest.raises(dev_bot.DevBotError, match="another owner"):
        dev_bot.submit_level6_calendar_task(
            "bob", bots.get_bot(BOT), "level6: calendar", store=rig["store"])
    assert rig["store"].submissions == []


def test_stopped_bot_fails_closed(rig):
    _l6cal_bot(rig["tmp"], status="stopped")
    _write_conv_for(OWNER, bot_id=BOT)
    with pytest.raises(chat_service.ChatUnavailable):
        asyncio.run(_frames(chat_service.stream_chat(
            OWNER, "cid-foreign", "level6: calendar")))
    assert rig["store"].submissions == []