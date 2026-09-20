"""Focused tests: Chief-of-Staff delegation to a Level 6 Calendar Bot.

Proves, against the REAL production code, that a delegated ``level6:
calendar`` intent:

  1. is normalized to the ONE exact command (``level6: calendar``) and the
     target task carries ``executor_prefix="level6"`` with
     ``task_text=="calendar"`` and NO repo URL — dispatched through the
     EXISTING in-process level6 branch, never the generic repo executor,
     never the engine/LLM, never a browser;
  2. an unsupported/ambiguous ``level6:`` request (``level6: something``,
     ``level6:calendar`` without the separating space, a case-variant
     ``level6: Calendar``) fails closed BEFORE any durable record is written;
  3. a ``level6: calendar`` read may only target a configured Level 6
     Calendar Bot (EXACTLY ``cal:list`` + ``glofox:read`` at tier 0); a
     write-capable/ordinary Bot, a Calendar Reader, or a Glofox Reader are
     refused;
  4. the pinned ``level6: weekly`` delegation behaviour is byte-identical to
     the pre-existing pass-through (never widened, never rerouted);
  5. ordinary/developer delegation behaviour is byte-identical.

Run: python3 -m pytest test_delegation_level6_calendar.py
"""
import os
import sys
import tempfile

import pytest

# Isolate the data root BEFORE importing paths-dependent modules.
_TMP = tempfile.mkdtemp(prefix="kyrex_delegation_l6cal_")
os.environ.setdefault("KYREX_DATA_DIR", _TMP)

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import bots            # noqa: E402
import serve           # noqa: E402
import delegation      # noqa: E402
from task_store import CloudTaskStore  # noqa: E402

COORD_POLICY = {"fs:read": 0, "bot:delegate": 0}
L6CAL_POLICY = {"cal:list": 0, "glofox:read": 0}
READER_POLICY = {"cal:list": 0}
GLOFOX_POLICY = {"glofox:read": 0}
PLAIN_POLICY = {"fs:read": 0}
DEV_POLICY = {"fs:read": 0, "repo:read": 0, "fs:write": 1, "repo:pr": 1}


def _store(tmp_path):
    return CloudTaskStore(db_path=tmp_path / "delegation-l6cal.db")


def _rift(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _register(monkeypatch, tmp_path, bot_id, *, owner, status="running",
              policy=None, provider=True):
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "test:model", _rift(tmp_path, f"rift-{bot_id}"),
        policy=policy or {}, status=status, owner=owner,
        provider_profile_id="p1" if provider else "",
    )


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    monkeypatch.setattr(
        serve, "_bot_llm_config",
        lambda bot: {"provider": "openai", "api_key": "x", "model": "m"},
    )


# ── 1. normalization + no repo URL ─────────────────────────────────────

def test_delegated_level6_calendar_routes_to_the_level6_executor(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "l6cal", owner="alice",
              policy=L6CAL_POLICY)

    view = delegation.submit_delegation(
        "alice", chief, "l6cal", "level6: calendar", store=store)
    task = store.get(view["task_id"])
    assert task["executor_prefix"] == "level6"
    assert task["task_text"] == "calendar"       # prefix-stripped exact request
    assert not task.get("repo_url")              # never a repo URL
    assert task["bot_id"] == "l6cal"
    assert task["chat_id"] == "alice"            # owner-scoped
    assert task["parent_delegation_id"] == view["delegation_id"]
    # The delegation row stores the canonical normalized request, exactly
    # like the calendar-reader rows store "calendar: <command>".
    row = store.get_delegation(view["delegation_id"]) or {}
    assert row.get("task_text") == "calendar", row
    assert row.get("target_bot_id") == "l6cal", row


# ── 2. unsupported/ambiguous fails closed ──────────────────────────────

def test_unsupported_or_ambiguous_level6_requests_fail_closed(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "l6cal", owner="alice",
              policy=L6CAL_POLICY)

    for bad in ("level6: something", "level6: calendar now",
                "level6:calendar", "level6: Calendar", "level6: schedule",
                "level6: weeklyx"):
        with pytest.raises(delegation.DelegationError):
            delegation.submit_delegation("alice", chief, "l6cal", bad,
                                         store=store)
    # Nothing durable was written for any refused request.
    assert store.list_delegations(owner="alice") == []


# ── 3. a level6: calendar read must target a Level 6 Calendar Bot ──────

@pytest.mark.parametrize("policy", [
    DEV_POLICY,                     # write-capable
    READER_POLICY,                  # cal:list only
    GLOFOX_POLICY,                  # glofox:read only
    PLAIN_POLICY,                   # ordinary read-only
    {"cal:list": 0, "glofox:read": 0, "fs:read": 0},  # extra capability
])
def test_level6_calendar_intent_to_a_non_l6cal_is_refused(
        tmp_path, monkeypatch, policy):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "other", owner="alice", policy=policy)

    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", chief, "other",
                                     "level6: calendar", store=store)
    assert store.list_delegations(owner="alice") == []


# ── 4. the pinned weekly delegation is byte-identical (unchanged) ──────

def test_level6_weekly_delegation_unchanged(tmp_path, monkeypatch):
    """The pre-existing pass-through for ``level6: weekly`` is untouched:
    the caller's executor prefix and text are preserved exactly as before."""
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice", policy=DEV_POLICY)

    view = delegation.submit_delegation(
        "alice", chief, "dev", "level6: weekly", store=store)
    task = store.get(view["task_id"])
    assert task["executor_prefix"] == "repo"     # caller default, unchanged
    assert task["task_text"] == "level6: weekly"


# ── 5. ordinary/developer delegation is byte-identical ─────────────────

def test_ordinary_delegation_unchanged(tmp_path, monkeypatch):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice", policy=DEV_POLICY)

    view = delegation.submit_delegation(
        "alice", chief, "dev", "summarize the repo", store=store)
    task = store.get(view["task_id"])
    assert task["executor_prefix"] == "repo"
    assert task["task_text"] == "summarize the repo"
    assert not task.get("repo_url")          # resolved at run time, as always