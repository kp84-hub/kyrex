"""Focused tests: Chief-of-Staff delegation to a Calendar Reader Bot.

Root cause under test: a delegated calendar task used to be submitted with the
generic ``repo`` executor, so the worker spawned ``git_workflow`` against the
Calendar Reader's (empty) workspace Rift and died with
``RuntimeError: empty --rift requires --repo-url to clone``.

These tests prove the fix:

  1. a delegated calendar intent is normalized to the ONE exact command
     (``calendar: today|tomorrow|week``) and the target task carries
     ``executor_prefix="calendar"`` with NO repo URL -- dispatched through the
     EXISTING in-process calendar branch, never the generic repo executor;
  2. an unsupported/ambiguous ``calendar:`` request fails closed BEFORE any
     durable record is written;
  3. a calendar read may only target a configured Calendar Reader (exactly
     ``cal:list`` at tier 0); a write-capable/ordinary Bot is refused;
  4. a Calendar Reader refuses a non-calendar task (a fixed read-only
     capability never falls to the repo executor);
  5. ordinary/developer delegation behaviour is byte-identical (executor_prefix
     stays ``repo``; an explicit caller prefix is still honored);
  6. END TO END: Chief -> Calendar Reader completes ONE durable final response
     with no repo URL and without touching the repo executor.

Run: python3 -m pytest test_delegation_calendar.py
"""
import os
import sys
import tempfile

import pytest

# Isolate the data root BEFORE importing paths-dependent modules.
_TMP = tempfile.mkdtemp(prefix="kyrex_delegation_calendar_")
os.environ["KYREX_DATA_DIR"] = _TMP

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import bots            # noqa: E402
import serve           # noqa: E402
import delegation      # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402


COORD_POLICY = {"fs:read": 0, "bot:delegate": 0}
READER_POLICY = {"cal:list": 0}
PLAIN_POLICY = {"fs:read": 0}
DEV_POLICY = {"fs:read": 0, "repo:read": 0, "fs:write": 1, "repo:pr": 1}


def _store(tmp_path):
    return CloudTaskStore(db_path=tmp_path / "delegation-cal.db")


def _rift(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _register(monkeypatch, tmp_path, bot_id, *, owner, status="running",
              policy=None, provider=True):
    """Register a Bot with a resolvable Rift and (by default) a provider."""
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "test:model", _rift(tmp_path, f"rift-{bot_id}"),
        policy=policy or {}, status=status, owner=owner,
        provider_profile_id="p1" if provider else "",
    )


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    """Provider resolution is configured by default (hermetic, no secrets)."""
    monkeypatch.setattr(
        serve, "_bot_llm_config",
        lambda bot: {"provider": "openai", "api_key": "x", "model": "m"},
    )


# ── 1. normalization + no repo URL ─────────────────────────────────────

def test_delegated_calendar_intent_routes_to_the_calendar_executor(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "reader", owner="alice",
              policy=READER_POLICY)

    cases = (
        ("calendar: today", "calendar: today"),
        ("calendar: tomorrow", "calendar: tomorrow"),
        ("calendar: week", "calendar: week"),
        # case/whitespace are normalized to the byte-exact command.
        ("Calendar:  WEEK", "calendar: week"),
        ("  calendar: today  ", "calendar: today"),
    )
    for text, canonical in cases:
        view = delegation.submit_delegation(
            "alice", chief, "reader", text, store=store)
        task = store.get(view["task_id"])
        assert task["executor_prefix"] == "calendar", text
        assert not task.get("repo_url"), text          # never a repo URL
        assert task["task_text"] == canonical, text    # normalized
        assert task["bot_id"] == "reader"
        assert task["chat_id"] == "alice"              # owner-scoped
        assert task["parent_delegation_id"] == view["delegation_id"]


# ── 2. unsupported/ambiguous fails closed ──────────────────────────────

def test_unsupported_or_ambiguous_calendar_requests_fail_closed(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "reader", owner="alice",
              policy=READER_POLICY)

    for bad in ("calendar: yesterday", "calendar: this week",
                "calendar:today please", "calendar:today", "calendar:x"):
        with pytest.raises(delegation.DelegationError):
            delegation.submit_delegation("alice", chief, "reader", bad,
                                         store=store)
    # Nothing durable was written for any refused request.
    assert store.list_delegations(owner="alice") == []


# ── 3. a calendar read must target a configured Calendar Reader ────────

def test_calendar_intent_to_a_non_reader_is_refused(tmp_path, monkeypatch):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice", policy=DEV_POLICY)

    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", chief, "dev", "calendar: week",
                                     store=store)
    assert store.list_delegations(owner="alice") == []


# ── 4. a Calendar Reader refuses a non-calendar task ───────────────────

def test_a_reader_refuses_a_non_calendar_task(tmp_path, monkeypatch):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "reader", owner="alice",
              policy=READER_POLICY)

    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", chief, "reader",
                                     "write a file into the repo", store=store)
    assert store.list_delegations(owner="alice") == []


# ── 5. ordinary/developer delegation is unchanged ──────────────────────

def test_ordinary_and_developer_delegation_are_unchanged(tmp_path, monkeypatch):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "plain", owner="alice",
              policy=PLAIN_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice", policy=DEV_POLICY)

    for target in ("plain", "dev"):
        view = delegation.submit_delegation(
            "alice", chief, target, "summarize the repo", store=store)
        task = store.get(view["task_id"])
        assert task["executor_prefix"] == "repo", target   # unchanged default
        assert task["task_text"] == "summarize the repo", target
        assert not task.get("repo_url")


def test_explicit_caller_executor_prefix_still_honored(tmp_path, monkeypatch):
    # A non-calendar delegated task keeps the caller's executor prefix, exactly
    # as before this change.
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "plain", owner="alice",
              policy=PLAIN_POLICY)
    view = delegation.submit_delegation(
        "alice", chief, "plain", "read a file", store=store,
        executor_prefix="fs")
    assert store.get(view["task_id"])["executor_prefix"] == "fs"


# ── 6. end to end: Chief -> Calendar Reader, no repo URL, one result ───

def test_delegated_calendar_task_completes_without_a_repo(tmp_path, monkeypatch):
    store = _store(tmp_path)
    chief = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "reader", owner="alice",
              policy=READER_POLICY)

    view = delegation.submit_delegation("alice", chief, "reader",
                                        "calendar: week", store=store)
    task = store.get(view["task_id"])
    assert task["executor_prefix"] == "calendar"
    assert not task.get("repo_url")

    seen = {}

    def fake_calendar(ctx, chat_id, task_text, task_id, send,
                      on_progress=None, on_result=None):
        # Record that the IN-PROCESS calendar branch ran (never git_workflow).
        seen["task_text"] = task_text
        seen["bot_id"] = getattr(ctx, "bot_id", "")
        seen["owner"] = getattr(ctx, "bot_owner", "")
        seen["granted"] = serve.cal_list_granted(getattr(ctx, "policy", {}))
        if on_result is not None:
            on_result({"status": "no_changes", "count": 1,
                       "final_response": "Calendar: week\n- Mon standup"})

    monkeypatch.setattr(serve, "_run_calendar_read_task", fake_calendar)

    # Drive the EXISTING worker -> serve.run_task path (no repo, no Rift error).
    worker = TaskWorker(store, worker_id="cal-w", executor=serve.run_task,
                        max_workers=1)
    worker.execute_task(store.get(view["task_id"]))

    assert seen.get("task_text") == "calendar: week"
    assert seen.get("bot_id") == "reader"
    assert seen.get("owner") == "alice"
    assert seen.get("granted") is True

    assert store.status(view["task_id"]) == "done"
    result_events = [e for e in store.get_events(view["task_id"])
                     if e.get("type") == "result"]
    assert len(result_events) == 1                      # ONE durable response
    assert "Mon standup" in (result_events[0]["payload"] or {}).get(
        "final_response", "")
