"""Regression: a migrated unified Calendar Bot whose id is "calendar" must be
able to run the pinned Calendar read.

``migrate_legacy_calendar_bots`` deliberately creates ONE Bot whose id is
``"calendar"``. The Calendar read route (``serve._run_calendar_read_task``)
used to reject ``bot_id == "calendar"`` as if it were the synthetic UNBOUND
context (built with ``bot_id = executor_prefix``) -- but a genuinely unbound
context ALREADY carries an EMPTY owner, so that sentinel wrongly rejected the
owner-bound migrated Bot.

This drives the REAL route seam end to end (real ``serve.build_context`` over a
real registry entry -> real ``_run_calendar_read_task``, with only the
connector/task-store/audit seams faked): the owner-bound ``"calendar"`` Bot
SUCCEEDS, while genuinely unbound, ownerless, cross-owner, and policy-invalid
contexts still FAIL CLOSED.

Run: python3 -m pytest test_calendar_bot_id_sentinel.py
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR",
                      tempfile.mkdtemp(prefix="kx-cal-sentinel-"))
os.environ.setdefault("WEB_SESSION_SECRET", "cal-sentinel-test-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bots          # noqa: E402
import connectors    # noqa: E402
import serve         # noqa: E402
import task_store    # noqa: E402

BOT_ID = "calendar"   # the id the legacy-calendar migration creates
OWNER = "alice"
COMMAND = "calendar: today"

_AUDIT = []


class _FakeCalendar:
    """Owner-scoped connector: only OWNER has a live Google connection."""

    def __init__(self, owner):
        self._owner = owner

    def events(self, **kwargs):
        if self._owner != OWNER:
            raise connectors.ConnectorUnavailable("connector is not connected")
        return [{
            "summary": "Standup",
            "start": {"dateTime": "2025-03-09T13:00:00+00:00"},
            "end": {"dateTime": "2025-03-09T13:15:00+00:00"},
        }]


class _FakeStore:
    def calendar(self, owner):
        return _FakeCalendar(owner)

    def preferred_calendar(self, owner):
        return "primary"


class _FakeTaskStore:
    def __init__(self, *args, **kwargs):
        pass

    def get(self, task_id):
        return {"task_id": task_id, "status": task_store.STATUS_RUNNING,
                "cancel_requested": 0}


class _Recorder:
    def __init__(self):
        self.sent = []
        self.results = []

    def send(self, chat_id, text):
        self.sent.append(text)

    def on_result(self, payload):
        self.results.append(payload)


@pytest.fixture(autouse=True)
def _seams(monkeypatch, tmp_path):
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    bots.save_bots({})
    monkeypatch.setattr(connectors, "default_store", lambda: _FakeStore())
    monkeypatch.setattr(task_store, "CloudTaskStore", _FakeTaskStore)
    _AUDIT.clear()
    monkeypatch.setattr(serve.audit, "log", lambda **kw: _AUDIT.append(kw))
    yield
    bots.save_bots({})


def _register(bot_id=BOT_ID, owner=OWNER, policy=None, status="running"):
    if policy is None:
        policy = serve.calendar_preset_policy()
    bots.save_bots({bot_id: {
        "id": bot_id, "name": "Calendar Bot", "model": "openai:gpt-test",
        "rift": "/tmp", "repo": "", "system_prompt": "", "owner": owner,
        "browser_allowlist": [], "provider_profile_id": "",
        "policy": policy, "created_at": "", "status": status,
    }})


def _run(session_key=BOT_ID, prefix="calendar", text=COMMAND, task_id="t-1"):
    """Build the context the worker builds, then run the REAL route."""
    ctx = serve.build_context(session_key, prefix, allow_bot_resolution=True)
    rec = _Recorder()
    serve._run_calendar_read_task(
        ctx, "chat", text, task_id, rec.send, on_result=rec.on_result)
    return ctx, rec


# ── the regression ───────────────────────────────────────────────────

def test_migrated_owner_bound_calendar_bot_id_reads_successfully():
    """The id the migration creates ("calendar") is a LEGITIMATE bound id."""
    _register()                       # id="calendar", owner="alice", running
    ctx, rec = _run()
    assert ctx.bot_id == "calendar", ctx.bot_id
    assert ctx.bot_owner == OWNER, ctx.bot_owner

    joined = "\n".join(rec.sent)
    assert "Standup" in joined, joined                 # the read ran
    assert "failed closed" not in joined, joined        # NOT the id sentinel
    # The durable terminal result carries the same readable lines.
    assert rec.results, "no durable terminal result"
    assert "Standup" in rec.results[-1]["final_response"]


def test_genuinely_unbound_context_fails_closed():
    """No Bot registered -> the synthetic UNBOUND context (empty owner)."""
    ctx, rec = _run()                 # nothing registered for "calendar"
    assert ctx.bot_id == "calendar", ctx.bot_id          # == executor_prefix
    assert ctx.bot_owner == "", ctx.bot_owner            # genuinely unbound

    last = rec.sent[-1]
    assert "failed closed" in last, last
    assert "bound Bot with an owner" in last, last
    assert not rec.results


def test_ownerless_bot_fails_closed():
    """A registered but OWNERLESS Bot is not a bound Bot."""
    _register(owner="")
    ctx, rec = _run()
    assert ctx.bot_owner == "", ctx.bot_owner
    last = rec.sent[-1]
    assert "failed closed" in last, last
    assert "bound Bot with an owner" in last, last


def test_cross_owner_context_fails_closed():
    """Another owner's context can never read THIS owner's calendar."""
    _register(owner="bob")            # the Bot belongs to bob, not alice
    ctx, rec = _run()
    assert ctx.bot_owner == "bob", ctx.bot_owner

    joined = "\n".join(rec.sent)
    assert "Standup" not in joined, joined               # no cross-owner data
    assert "unavailable" in joined.lower() or "failed closed" in joined.lower()


@pytest.mark.parametrize("policy", [
    {},                     # no grant at all
    {"cal:list": 1},        # raised tier (host tier for cal:list is 0)
    {"*": 0},               # wildcard is never an exact cal:list grant
])
def test_policy_invalid_context_fails_closed(policy):
    _register(policy=policy)
    ctx, rec = _run()
    joined = "\n".join(rec.sent)
    assert "Standup" not in joined, joined
    assert ("denied" in joined.lower()
            or "failed closed" in joined.lower()), joined


def test_bound_calendar_id_is_not_treated_as_the_unbound_sentinel():
    """Direct contrast: same bot_id, but owner present => not the sentinel."""
    _register()
    bound, rec_bound = _run()
    # Remove the Bot -> identical bot_id ("calendar"), but now UNBOUND.
    bots.save_bots({})
    unbound, rec_unbound = _run()

    assert bound.bot_id == unbound.bot_id == "calendar"
    assert bound.bot_owner == OWNER and unbound.bot_owner == ""
    assert "Standup" in "\n".join(rec_bound.sent)
    assert "failed closed" in rec_unbound.sent[-1]
