"""Focused tests for the native Calendar Writer delta on current main.

Covers: exact ``cal:create:0`` preset isolation (never widening the Reader or
any other capability); deterministic bounded create-intent validation;
the executor's mandatory exact-payload confirmation gate (denial => no provider
call; approval => safe receipt); the SEPARATE owner-scoped event-write OAuth
scope + upgrade (a Reader read-only token can never write); delegation routing
to the writer executor; and the Calendar Reader remaining unchanged.

Run: python3 -m pytest test_calendar_writer.py
"""
import io
import json
import os
import sys
import urllib.parse
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

os.environ.setdefault("WEB_SESSION_SECRET", "calwriter-native-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "https://app.example/oauth/google/callback")

import serve          # noqa: E402
import cal_writer     # noqa: E402
import connectors as C  # noqa: E402
import delegation     # noqa: E402

WRITER = serve.CALENDAR_WRITER_PRESET


# ── 1. exact "cal:create:0" preset isolation ───────────────────────────

class TestPresetIsolation:
    def test_exact_cal_create_tier0(self):
        assert WRITER == {"cal:create": 0}
        assert serve.OPERATION_TIERS["cal:create"] == 0
        assert serve.calendar_writer_granted(WRITER) is True
        assert serve.is_calendar_writer_policy(WRITER) is True
        assert serve.effective_permissions(WRITER)["cal:create"] == 0

    def test_never_widens_reader_or_any_other_op(self):
        perms = serve.effective_permissions(WRITER)
        for op, tier in perms.items():
            if op == "cal:create":
                continue
            assert tier == "deny", f"{op} must be denied, got {tier!r}"
        # Reader (cal:list) distinct and untouched.
        assert serve.OPERATION_TIERS["cal:list"] == 0
        assert serve.is_calendar_writer_policy({"cal:list": 0}) is False
        assert serve.calendar_writer_granted({"cal:list": 0}) is False
        assert serve.cal_list_granted(WRITER) is False

    def test_wildcards_and_raised_tier_are_not_the_grant(self):
        for policy in ({"cal:*": 0}, {"*": 0}, {"cal:create": 2}):
            assert serve.calendar_writer_granted(policy) is False, policy
            assert serve.is_calendar_writer_policy(policy) is False, policy

    def test_extra_capability_disqualifies(self):
        for extra in ({"cal:list": 0}, {"fs:read": 0}, {"browser:read": 0},
                      {"fs:write": 1}, {"bot:delegate": 0}):
            policy = dict(WRITER)
            policy.update(extra)
            assert serve.calendar_writer_granted(policy) is True
            assert serve.is_calendar_writer_policy(policy) is False, extra


# ── 2. bounded deterministic validation ────────────────────────────────

class TestValidation:
    def test_explicit_from_to(self):
        intent = cal_writer.parse_create_request(
            "create Design review on 2025-03-04 from 09:00 to 10:30")
        assert intent == {"title": "Design review",
                          "start": "2025-03-04T09:00:00",
                          "end": "2025-03-04T10:30:00", "all_day": False}

    def test_direct_chat_namespace_prefix(self):
        intent = cal_writer.parse_create_request(
            "calendar: create Test Kyrex Event on 2026-09-21 "
            "from 19:00 to 19:15")
        assert intent == {"title": "Test Kyrex Event",
                          "start": "2026-09-21T19:00:00",
                          "end": "2026-09-21T19:15:00", "all_day": False}

    def test_chief_of_staff_delegated_titled_phrase(self):
        intent = cal_writer.parse_create_request(
            'Create a calendar event titled "Chief of Staff Delegation Test" '
            "on 2026-09-21 from 19:30 to 19:45.")
        assert intent == {"title": "Chief of Staff Delegation Test",
                          "start": "2026-09-21T19:30:00",
                          "end": "2026-09-21T19:45:00", "all_day": False}

    def test_am_pm_and_duration(self):
        assert cal_writer.parse_create_request(
            "schedule Standup on 2025-03-04 from 9am to 10:15am")["start"] \
            == "2025-03-04T09:00:00"
        assert cal_writer.parse_create_request(
            "add Focus on 2025-03-04 at 14:00 for 45 minutes")["end"] \
            == "2025-03-04T14:45:00"

    def test_all_day(self):
        intent = cal_writer.parse_create_request("create Holiday on 2025-12-25 all day")
        assert intent["all_day"] is True and intent["end"] == "2025-12-26"

    def test_ambiguous_or_unsupported_fails_closed(self):
        for bad in ("create Meeting on 2025-03-04", "lunch tomorrow at noon",
                    "create X on 2025-03-04 from 09:00",
                    "create X on 2025-03-04 09:00-10:00"):
            with pytest.raises(cal_writer.CalendarWriterError):
                cal_writer.parse_create_request(bad)

    def test_unknown_field_rejected(self):
        for extra in ({"attendees": ["a@example.com"]},
                      {"recurrence": ["RRULE:FREQ=DAILY"]},
                      {"location": "Room 1"}, {"description": "x"}):
            payload = {"title": "X", "start": "2025-03-04T09:00:00",
                       "end": "2025-03-04T10:00:00", "all_day": False}
            payload.update(extra)
            with pytest.raises(cal_writer.CalendarWriterError):
                cal_writer.validate_intent(payload)

    def test_primary_and_tz_defaults(self):
        intent = cal_writer.parse_create_request(
            "create X on 2025-03-04 from 09:00 to 10:00")
        assert cal_writer.CALENDAR_ID == "primary"
        assert cal_writer.TIMEZONE == "America/New_York"
        event = cal_writer.to_google_event(intent)
        assert event["start"] == {"dateTime": "2025-03-04T09:00:00",
                                  "timeZone": "America/New_York"}
        # Only summary/start/end are ever emitted.
        assert set(event) == {"summary", "start", "end"}


# ── 3. executor mandatory confirmation gate ────────────────────────────

class _FakeWriter:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {"id": "evt-1"}
        self.error = error
        self.calls = []

    def create_event(self, event):
        self.calls.append(event)
        if self.error is not None:
            raise self.error
        return self.result


class _FakeStore:
    def __init__(self, writer):
        self.writer = writer
        self.owners = []

    def calendar_writer(self, owner, **kw):
        self.owners.append(owner)
        return self.writer


def _run_executor(task, stdin_text, owner="alice", store=None):
    import calendar_writer_executor as ex
    out = io.StringIO()
    old_out, old_in, old_argv = sys.stdout, sys.stdin, sys.argv
    old_owner = os.environ.get("KYREX_BOT_OWNER")
    os.environ["KYREX_BOT_OWNER"] = owner
    sys.stdout = out
    sys.stdin = io.StringIO(stdin_text)
    sys.argv = ["calendar_writer_executor.py", "--task", task]
    try:
        if store is not None:
            with patch.object(C, "default_store", return_value=store):
                ex.main()
        else:
            with patch.object(C, "default_store",
                              side_effect=AssertionError("no provider call expected")):
                ex.main()
    finally:
        sys.stdout, sys.stdin, sys.argv = old_out, old_in, old_argv
        if old_owner is None:
            os.environ.pop("KYREX_BOT_OWNER", None)
        else:
            os.environ["KYREX_BOT_OWNER"] = old_owner
    return out.getvalue()


def _result(output):
    for line in output.splitlines():
        if line.startswith("KYREX_RESULT_JSON:"):
            return json.loads(line[len("KYREX_RESULT_JSON:"):])
    return {}


GOOD = "create Review on 2025-03-04 from 09:00 to 10:00"


class TestExecutorGate:
    def test_denial_makes_no_provider_call(self):
        out = _run_executor(GOOD, "ALLOW\nDENIED\n", store=None)
        assert _result(out)["status"] == "error"
        assert any(l.startswith("KYREX_APPROVAL:") for l in out.splitlines())

    def test_host_deny_makes_no_provider_call(self):
        out = _run_executor(GOOD, "DENY\n", store=None)
        assert _result(out)["status"] == "error"

    def test_ambiguous_fails_before_gate(self):
        out = _run_executor("lunch tomorrow at noon", "", store=None)
        assert _result(out)["status"] == "error"
        assert not any(l.startswith("KYREX_APPROVAL:") for l in out.splitlines())

    def test_missing_owner_fails_closed(self):
        out = _run_executor(GOOD, "ALLOW\n", owner="", store=None)
        assert _result(out)["status"] == "error"

    def test_approval_creates_and_returns_receipt(self):
        writer = _FakeWriter(result={"id": "evt-42", "summary": "Review"})
        store = _FakeStore(writer)
        out = _run_executor(GOOD, "ALLOW\nAPPROVED\n", store=store)
        result = _result(out)
        assert result["status"] == "ok"
        assert "Review" in result["final_response"] and "evt-42" in result["final_response"]
        assert writer.calls == [{
            "summary": "Review",
            "start": {"dateTime": "2025-03-04T09:00:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2025-03-04T10:00:00", "timeZone": "America/New_York"},
        }]
        assert store.owners == ["alice"]

    def test_missing_write_authorization_fails_closed(self):
        writer = _FakeWriter(error=C.ConnectorUnavailable("no write scope"))
        out = _run_executor(GOOD, "ALLOW\nAPPROVED\n", store=_FakeStore(writer))
        assert _result(out)["status"] == "error"

    def test_malformed_provider_response_fails_closed(self):
        writer = _FakeWriter(error=C.ConnectorError("garbage"))
        out = _run_executor(GOOD, "ALLOW\nAPPROVED\n", store=_FakeStore(writer))
        assert _result(out)["status"] == "error"


# ── 4. OAuth scope separation + upgrade ────────────────────────────────

class _FakeTransport:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.calls = []

    def __call__(self, method, url, token, params=None, body=None):
        self.calls.append({"method": method, "url": url})
        return self.payload


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    return C.ConnectorStore(path=tmp_path / "connectors.json")


def _connect(store, owner="alice", scopes=None):
    scopes = list(scopes or C.GOOGLE_READ_SCOPES)
    begun = store.begin_oauth(owner, scopes=scopes)
    store.complete_oauth(
        owner, begun["state"], "code",
        exchange=lambda code, redirect, client: {
            "access_token": "ya29.X", "refresh_token": "1//R",
            "expires_in": 3600, "scope": " ".join(scopes)})
    return store


class TestOAuthScopeSeparation:
    def test_default_connect_requests_read_only(self, store):
        begun = store.begin_oauth("alice")
        assert C.GOOGLE_CALENDAR_WRITE_SCOPE not in begun["scopes"]
        assert "auth/calendar.events" not in urllib.parse.unquote(
            begun["authorization_url"])

    def test_upgrade_adds_the_write_scope(self, store):
        begun = store.begin_calendar_write_upgrade("alice")
        assert C.GOOGLE_CALENDAR_WRITE_SCOPE in begun["scopes"]
        assert "auth/calendar.events" in urllib.parse.unquote(
            begun["authorization_url"])

    def test_arbitrary_scope_refused(self, store):
        with pytest.raises(C.ConnectorError):
            store.begin_oauth("alice", scopes=["https://www.googleapis.com/auth/drive"])

    def test_reader_token_cannot_write(self, store):
        _connect(store, scopes=C.GOOGLE_READ_SCOPES)
        transport = _FakeTransport({"id": "e1"})
        with pytest.raises(C.ConnectorUnavailable):
            store.calendar_writer("alice", transport=transport).create_event(
                {"summary": "X", "start": {}, "end": {}})
        assert transport.calls == []

    def test_write_token_creates_and_receipt_is_safe(self, store):
        _connect(store, scopes=list(C.GOOGLE_READ_SCOPES) + [C.GOOGLE_CALENDAR_WRITE_SCOPE])
        transport = _FakeTransport({
            "id": "evt-9", "status": "confirmed", "summary": "X",
            "start": {"dateTime": "2025-03-04T09:00:00"},
            "end": {"dateTime": "2025-03-04T10:00:00"},
            "htmlLink": "https://c.example/e?token=SECRET"})
        created = store.calendar_writer("alice", transport=transport).create_event(
            {"summary": "X", "start": {"dateTime": "2025-03-04T09:00:00"},
             "end": {"dateTime": "2025-03-04T10:00:00"}})
        assert created["id"] == "evt-9"
        assert "SECRET" not in json.dumps(created)
        assert transport.calls[0]["method"] == "POST"
        assert "/calendars/primary/events" in transport.calls[0]["url"]

    def test_malformed_response_fails_closed(self, store):
        _connect(store, scopes=list(C.GOOGLE_READ_SCOPES) + [C.GOOGLE_CALENDAR_WRITE_SCOPE])
        with pytest.raises(C.ConnectorError):
            store.calendar_writer("alice", transport=_FakeTransport({})).create_event(
                {"summary": "X", "start": {}, "end": {}})

    def test_reader_route_is_still_read_only(self, store):
        _connect(store, scopes=C.GOOGLE_READ_SCOPES)
        assert store.route_capability("alice", "calendar.read")["read_only"] is True


# ── 5. delegation routing ──────────────────────────────────────────────

class TestDelegationRouting:
    def test_writer_target_routes_to_its_own_executor(self):
        assert delegation._is_calendar_writer({"policy": WRITER}) is True
        prefix, text = delegation._resolve_delegated_route(
            "repo", {"policy": WRITER, "id": "w"},
            "create Review on 2025-03-04 from 09:00 to 10:00")
        assert prefix == "cal_write"
        assert text == "create Review on 2025-03-04 from 09:00 to 10:00"
        assert serve.EXECUTORS["cal_write"] == "calendar_writer_executor.py"

    def test_empty_writer_request_fails_closed(self):
        with pytest.raises(delegation.DelegationError):
            delegation._resolve_delegated_route("repo", {"policy": WRITER, "id": "w"}, "   ")

    def test_reader_target_is_not_a_writer(self):
        assert delegation._is_calendar_writer({"policy": {"cal:list": 0}}) is False


# ── 6. Reader unchanged ────────────────────────────────────────────────

class TestReaderUnchanged:
    def test_reader_registrations_intact(self):
        assert serve.EXECUTORS["cal"] == "cal_executor.py"
        assert serve.OPERATION_TIERS["cal:list"] == 0
        assert serve.is_calendar_writer_policy({"cal:list": 0}) is False
        assert "cal.create" in serve.KNOWN_OPERATIONS
