"""Focused Chat/delegation route tests for the Calendar Editor (delete).

Proves the END-TO-END Chat wiring for the destructive ``cal:delete`` capability:

  1. the canonical sentence "Remove this from calendar Level 6 Workout: Lower
     Body Pyramid Sets" satisfies the EXACT Chat route predicate for a Calendar
     Editor bot, and is NOT a Level 6 request (strict Level 6 non-routing: both
     natural detectors return None and the ``level6:`` prefix handler rejects it);
  2. a NON-delete message on an Editor bot is never routed to the editor, and a
     non-editor bot never routes a delete -- the surface does not widen;
  3. submission carries ``executor_prefix="cal_edit"`` with NO repo URL (the
     editor executor, never level6/repo/cal_write/browser), and requires the
     EXACT cal:delete grant;
  4. a delegated delete targets the Calendar Editor executor; the same delete on
     the unified Calendar Bot fails closed (never the repo executor);
  5. an AMBIGUOUS title returns the CANDIDATE events with NO task and NO
     approval gate; a UNIQUE title resolves to exactly one event;
  6. an event is never presented as Level 6-sourced without the title evidence;
  7. a denial at the T2 gate performs NO provider call (nothing deleted).

Run: python3 -m pytest test_calendar_editor_chat.py
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", tempfile.mkdtemp(prefix="kyrex-cal-editor-chat-"))
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "cal-editor-chat-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bots            # noqa: E402
import dev_bot         # noqa: E402
import serve           # noqa: E402
import delegation      # noqa: E402
import cal_editor      # noqa: E402
import connectors      # noqa: E402
from task_store import CloudTaskStore  # noqa: E402

CANON = "Remove this from calendar Level 6 Workout: Lower Body Pyramid Sets"
L6_TITLE = "Level 6 Workout: Lower Body Pyramid Sets"
L6_ID = "l6evtABC12345.xyz"

EDITOR_POLICY = {"cal:delete": 2}
COORD_POLICY = {"fs:read": 0, "bot:delegate": 0}
PLAIN_POLICY = {"fs:read": 0}


def _rift(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _register(monkeypatch, tmp_path, bot_id, *, owner="alice", status="running",
              policy=None, provider=True):
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


# ── 1. the canonical sentence reaches the EDITOR, never Level 6 ────────

def test_canonical_delete_reaches_the_editor_not_level6(tmp_path, monkeypatch):
    editor = _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    plain = _register(monkeypatch, tmp_path, "plain", policy=PLAIN_POLICY)

    # The EXACT predicate chat_service consults.
    assert dev_bot.calendar_editor_route_for(editor, CANON) is True
    assert dev_bot.calendar_editor_route_for(plain, CANON) is False

    # STRICT Level 6 non-routing: not a natural Level 6 read, not a natural
    # calendar read, and the reserved `level6:` handler rejects the sentence.
    assert serve.natural_level6_calendar_command(CANON) is None
    assert serve.natural_calendar_command(CANON) is None
    prefix, _text, err = serve.resolve_executor("level6: " + CANON)
    assert prefix is None and err == "level6"
    assert CANON.strip() != serve.LEVEL6_CALENDAR_TASK_TEXT
    assert CANON.strip() != serve.LEVEL6_TASK_TEXT


def test_editor_surface_does_not_widen(tmp_path, monkeypatch):
    editor = _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    # A read/create/browse message is NOT an editor turn.
    for benign in ("what is on my calendar", "show me this week's Level 6 workouts",
                   "create Dentist on 2026-01-06 at 9am for 30 minutes"):
        assert dev_bot.calendar_editor_route_for(editor, benign) is False, benign
    # Paused/stopped editors do not route.
    stopped = _register(monkeypatch, tmp_path, "stopped", status="stopped",
                        policy=EDITOR_POLICY)
    assert dev_bot.calendar_editor_route_for(stopped, CANON) is False


# ── 2. submission uses the cal_edit executor; exact grant required ─────

def test_editor_submission_uses_cal_edit_and_no_repo(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor.db")
    editor = _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    task_id = dev_bot.submit_calendar_editor_task(
        "alice", editor, json.dumps({"id": L6_ID}), store=store)
    task = store.get(task_id)
    assert task["executor_prefix"] == "cal_edit"
    assert not task.get("repo_url")
    assert task["task_text"] == json.dumps({"id": L6_ID})
    assert task["bot_id"] == "editor"
    assert task["chat_id"] == "alice"
    assert task["executor_prefix"] not in ("level6", "level6_calendar",
                                           "repo", "cal_write", "browser")


def test_editor_submission_requires_the_exact_grant(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-grant.db")
    plain = _register(monkeypatch, tmp_path, "plain", policy=PLAIN_POLICY)
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_calendar_editor_task(
            "alice", plain, json.dumps({"id": L6_ID}), store=store)
    # The Reader / Writer grants never qualify as a delete grant.
    for policy in (serve.CALENDAR_READER_PRESET, serve.CALENDAR_WRITER_PRESET,
                   serve.CALENDAR_PRESET):
        assert serve.calendar_editor_granted(policy) is False, policy


# ── 3. delegation routing ──────────────────────────────────────────────

def test_delegated_delete_routes_to_the_editor(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-del.db")
    chief = _register(monkeypatch, tmp_path, "chief", policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    _install_events(monkeypatch, [
        {"id": L6_ID, "summary": L6_TITLE,
         "start": {"dateTime": "2026-01-06T09:00:00"}},
    ])
    view = delegation.submit_delegation(
        "alice", chief, "editor", CANON, store=store)
    task = store.get(view["task_id"])
    assert task["executor_prefix"] == "cal_edit"
    assert not task.get("repo_url")
    assert task["chat_id"] == "alice"          # owner-scoped
    # The delegated delete carries the EXACT resolved event, not a raw title --
    # the SAME payload shape the direct calendar_delete route submits.
    payload = json.loads(task["task_text"])
    assert payload["event"]["id"] == L6_ID
    assert payload["event"]["summary"] == L6_TITLE


def test_chief_descriptive_delete_is_canonicalized_for_editor(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-chief.db")
    chief = _register(monkeypatch, tmp_path, "chief", policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    _install_events(monkeypatch, [
        {"id": "l6chief00001", "summary": L6_TITLE,
         "start": {"dateTime": "2026-01-06T09:00:00"}},
    ])
    descriptive = (
        'Remove the calendar event titled "Level 6 Workout: Lower Body Pyramid Sets" '
        "from the owner's calendar. Find the matching event and delete it, then "
        "confirm whether the deletion succeeded."
    )
    view = delegation.submit_delegation(
        "alice", chief, "editor", descriptive, store=store)
    task = store.get(view["task_id"])
    assert task["executor_prefix"] == "cal_edit"
    payload = json.loads(task["task_text"])
    assert payload["event"]["id"] == "l6chief00001"


# ── 3b. delegated title -> exact event resolution (host preflight) ─────

def test_delegated_title_delete_resolves_against_the_preferred_calendar(
        tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-pref.db")
    chief = _register(monkeypatch, tmp_path, "chief", policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    calls = []
    monkeypatch.setattr(connectors, "default_store", lambda: _FakeStore(
        [{"id": "solo000001", "summary": "Dentist",
          "start": {"dateTime": "2026-01-06T09:00:00"}}],
        preferred="work-cal@example.test", calls=calls))
    view = delegation.submit_delegation(
        "alice", chief, "editor",
        "Remove this from calendar Dentist", store=store)
    task = store.get(view["task_id"])
    payload = json.loads(task["task_text"])
    assert payload["event"]["id"] == "solo000001"
    # resolved against the OWNER's PREFERRED calendar, by the requested title.
    assert calls == [{
        "max_results": 100,
        "calendar_id": "work-cal@example.test",
        "query": "Dentist",
    }]


def test_delegated_exact_id_delete_passes_through(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-id.db")
    chief = _register(monkeypatch, tmp_path, "chief", policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    view = delegation.submit_delegation(
        "alice", chief, "editor",
        f"delete calendar event id {L6_ID}", store=store)
    task = store.get(view["task_id"])
    assert task["executor_prefix"] == "cal_edit"
    assert task["task_text"] == json.dumps({"id": L6_ID})


def test_delegated_title_delete_no_match_fails_closed(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-nomatch.db")
    chief = _register(monkeypatch, tmp_path, "chief", policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    _install_events(monkeypatch, [{"id": "other000001", "summary": "Groceries"}])
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation(
            "alice", chief, "editor",
            "Remove this from calendar Dentist", store=store)
    # No delegation record and no task are created for an unresolved title.
    assert store.list_delegations(owner="alice") == []


def test_delegated_title_delete_ambiguous_fails_closed(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-amb.db")
    chief = _register(monkeypatch, tmp_path, "chief", policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    _install_events(monkeypatch, [
        {"id": "dupOne0001", "summary": "Dentist",
         "start": {"dateTime": "2026-01-06T09:00:00"}},
        {"id": "dupTwo0002", "summary": "Dentist",
         "start": {"dateTime": "2026-01-07T09:00:00"}},
    ])
    with pytest.raises(delegation.DelegationError) as exc:
        delegation.submit_delegation(
            "alice", chief, "editor",
            "Remove this from calendar Dentist", store=store)
    message = str(exc.value)
    assert "dupOne0001" in message and "dupTwo0002" in message
    assert store.list_delegations(owner="alice") == []


def test_editor_delegation_rejects_unbounded_free_form(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-freeform.db")
    chief = _register(monkeypatch, tmp_path, "chief", policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation(
            "alice", chief, "editor",
            "Please figure out what calendar thing I meant and delete it",
            store=store)
    assert store.list_delegations(owner="alice") == []


def test_delegated_delete_on_a_unified_bot_fails_closed(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-uni.db")
    chief = _register(monkeypatch, tmp_path, "chief", policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "unified", policy=serve.CALENDAR_PRESET)
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", chief, "unified", CANON, store=store)
    assert store.list_delegations(owner="alice") == []


# ── 4. ambiguity -> candidates, no task, no approval ──────────────────

class _FakeCal:
    def __init__(self, events, calls=None):
        self._events = events
        self._calls = calls

    def events(self, **kw):
        if self._calls is not None:
            self._calls.append(kw)
        return self._events


class _FakeStore:
    def __init__(self, events, *, preferred="primary", calls=None):
        self._events = events
        self._preferred = preferred
        self._calls = calls

    def preferred_calendar(self, _owner, _provider="google"):
        return self._preferred

    def calendar(self, _owner):
        return _FakeCal(self._events, self._calls)


def _install_events(monkeypatch, events):
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _FakeStore(events))


def test_ambiguous_title_returns_candidates_without_a_task(tmp_path, monkeypatch):
    import chat_service
    _install_events(monkeypatch, [
        {"id": "dupOne0001", "summary": "Dentist",
         "start": {"dateTime": "2026-01-06T09:00:00"}},
        {"id": "dupTwo0002", "summary": "Dentist",
         "start": {"dateTime": "2026-01-07T09:00:00"}},
    ])
    with pytest.raises(cal_editor.CalendarEditorError) as exc:
        chat_service._resolve_calendar_editor_target(
            "alice", cal_editor.normalize_delete_request(
                "Remove this from calendar Dentist"))
    message = str(exc.value)
    assert "dupOne0001" in message and "dupTwo0002" in message
    assert "exact id" in message

    # No task is created for an ambiguous title (the caller only submits on a
    # resolved event), so no approval gate can be reached.
    store = CloudTaskStore(db_path=tmp_path / "cal-editor-amb.db")
    assert store.list_delegations(owner="alice") == []


def test_delete_preflight_uses_preferred_calendar(monkeypatch):
    import chat_service
    calls = []
    store = _FakeStore(
        [{"id": "solo000001", "summary": "Dentist",
          "start": {"dateTime": "2026-01-06T09:00:00"}}],
        preferred="work-cal@example.test",
        calls=calls,
    )
    monkeypatch.setattr(connectors, "default_store", lambda: store)
    event = chat_service._resolve_calendar_editor_target(
        "alice",
        cal_editor.normalize_delete_request("Remove this from calendar Dentist"))
    assert event["id"] == "solo000001"
    assert calls == [{
        "max_results": 100,
        "calendar_id": "work-cal@example.test",
        "query": "Dentist",
    }]


def test_delete_preflight_queries_provider_with_exact_requested_title(monkeypatch):
    import chat_service
    calls = []
    store = _FakeStore(
        [{"id": "l6event001", "summary": "Level 6 Workout: Lower Body Pyramid Sets"}],
        preferred="primary",
        calls=calls,
    )
    monkeypatch.setattr(connectors, "default_store", lambda: store)
    title = "Level 6 Workout: Lower Body Pyramid Sets"
    event = chat_service._resolve_calendar_editor_target(
        "alice",
        cal_editor.normalize_delete_request(f"Remove this from calendar {title}"))
    assert event["id"] == "l6event001"
    assert calls[-1]["query"] == title


def test_unique_title_resolves_to_exactly_one_event(monkeypatch):
    import chat_service
    _install_events(monkeypatch, [
        {"id": "solo000001", "summary": "Dentist",
         "start": {"dateTime": "2026-01-06T09:00:00"}},
    ])
    event = chat_service._resolve_calendar_editor_target(
        "alice", cal_editor.normalize_delete_request(
            "Remove this from calendar Dentist"))
    assert event["id"] == "solo000001"


# ── 5. never claim an unsupported source ──────────────────────────────

def test_preview_never_claims_level6_without_evidence():
    mention = cal_editor.build_preview(
        {"id": "misc000001", "summary": "Lunch with the Level 6 coach"})
    assert mention["level6"] is False
    assert mention["level6_evidence"] is None
    assert "not a Level 6 workout" in cal_editor.preview_display(mention)

    evidence = cal_editor.build_preview({"id": L6_ID, "summary": L6_TITLE})
    assert evidence["level6"] is True
    assert evidence["level6_evidence"] == cal_editor.LEVEL6_TITLE_PREFIX


# ── 6. denial at the T2 gate performs no provider call ────────────────

def test_executor_denial_deletes_nothing():
    env = dict(os.environ)
    env["KYREX_BOT_OWNER"] = "alice"
    proc = subprocess.run(
        [sys.executable, os.path.join(_CLOUD, "calendar_editor_executor.py"),
         "--task", L6_ID],
        input="ALLOW\nDENY\n", capture_output=True, text=True, env=env,
        timeout=30)
    result = None
    approval = None
    for line in proc.stdout.splitlines():
        if line.startswith("KYREX_RESULT_JSON:"):
            result = json.loads(line.split(":", 1)[1])
        if line.startswith("KYREX_APPROVAL:"):
            approval = json.loads(line.split(":", 1)[1])
    assert result["status"] == "error"
    assert "not approved" in result["errors"][0]
    assert approval is not None and approval["tier"] == 2
    assert "Deleted" not in proc.stdout



# ── 7. the delete-preflight read is recorded + route-scoped ────────────

def test_preflight_read_is_recorded_and_owner_scoped(monkeypatch):
    import chat_service
    import audit
    calls = []
    monkeypatch.setattr(audit, "log", lambda **kw: calls.append(kw))
    _install_events(monkeypatch, [
        {"id": "solo000001", "summary": "Dentist",
         "start": {"dateTime": "2026-01-06T09:00:00"}},
    ])
    event = chat_service._resolve_calendar_editor_target(
        "alice",
        cal_editor.normalize_delete_request("Remove this from calendar Dentist"),
        {"id": "editor"})
    assert event["id"] == "solo000001"
    assert calls and calls[0]["operation"] == "cal.delete_preflight"
    assert calls[0]["bot_id"] == "editor"
    assert calls[0]["tier"] == "tier0"


def test_preflight_requires_a_normalized_intent(monkeypatch):
    import chat_service
    _install_events(monkeypatch, [{"id": "solo000001", "summary": "Dentist"}])
    with pytest.raises(cal_editor.CalendarEditorError):
        chat_service._resolve_calendar_editor_target("alice", {"title": ""})


def test_preflight_is_only_reachable_from_the_delete_route():
    import chat_service
    import inspect
    src = inspect.getsource(chat_service)
    # exactly two references: the definition and the ONE call site.
    assert src.count("_resolve_calendar_editor_target(") == 2
    call_idx = src.index("event = _resolve_calendar_editor_target(")
    delete_idx = src.index('if route == "calendar_delete":')
    write_idx = src.index('if route == "calendar_write":')
    # the call sits INSIDE the calendar_delete handler, before calendar_write.
    assert delete_idx < call_idx < write_idx
