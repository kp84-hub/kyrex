"""Focused tests for the native Calendar Editor (delete) delta on current main.

Covers: the EXACT ``cal:delete`` tier-2 preset isolation (never widening the
Reader or the Writer); deterministic delete-intent validation; exact event-ID
targeting; ambiguous-title DISAMBIGUATION (never guessed); the user-visible
PREVIEW with its Level 6 evidence rule; the SEPARATE owner-scoped event-write
scope (a Reader read-only token can never delete); and the executor's mandatory
T2 approval gate (denial => no provider call).

Run: python3 -m pytest test_calendar_editor.py
"""
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

os.environ.setdefault("WEB_SESSION_SECRET", "caleditor-native-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "https://app.example/oauth/google/callback")

import serve          # noqa: E402
import cal_editor     # noqa: E402
import connectors as C  # noqa: E402

EDITOR = serve.CALENDAR_EDITOR_PRESET
L6_TITLE = "Level 6 Workout: Lower Body Pyramid Sets"
L6_ID = "l6evtABC12345.xyz"


# ── 1. exact "cal:delete":2 preset isolation ───────────────────────────

class TestPresetIsolation:
    def test_exact_cal_delete_tier2(self):
        assert EDITOR == {"cal:delete": 2}
        assert serve.OPERATION_TIERS["cal:delete"] == 2
        assert serve.calendar_editor_granted(EDITOR) is True
        assert serve.is_calendar_editor_policy(EDITOR) is True
        assert serve.effective_permissions(EDITOR)["cal:delete"] == 2

    def test_never_widens_reader_or_writer(self):
        perms = serve.effective_permissions(EDITOR)
        for op, tier in perms.items():
            if op == "cal:delete":
                continue
            assert tier == "deny", f"{op} must be denied, got {tier!r}"
        # Reader (cal:list) and Writer (cal:create) are distinct and untouched.
        assert serve.OPERATION_TIERS["cal:list"] == 0
        assert serve.OPERATION_TIERS["cal:create"] == 0
        assert serve.is_calendar_editor_policy({"cal:list": 0}) is False
        assert serve.is_calendar_editor_policy({"cal:create": 0}) is False
        assert serve.calendar_editor_granted({"cal:list": 0}) is False
        assert serve.calendar_editor_granted(serve.CALENDAR_WRITER_PRESET) is False
        assert serve.calendar_editor_granted(serve.CALENDAR_READER_PRESET) is False

    def test_wildcards_are_not_the_grant_and_a_lower_tier_is_raised(self):
        # No EXACT rule -> never a delete grant.
        for policy in ({"cal:*": 2}, {"*": 2}):
            assert serve.calendar_editor_granted(policy) is False, policy
            assert serve.is_calendar_editor_policy(policy) is False, policy
        # A lower declared tier is RAISED to the host's destructive tier 2, so
        # it still requires the mandatory T2 approval gate before any delete.
        for policy in ({"cal:delete": 0}, {"cal:delete": 1}):
            assert serve.calendar_editor_granted(policy) is True, policy
            assert serve.effective_permissions(policy)["cal:delete"] == 2, policy

    def test_extra_capability_disqualifies_the_exact_preset(self):
        for extra in ({"cal:list": 0}, {"cal:create": 0}, {"fs:read": 0},
                      {"browser:read": 0}):
            policy = dict(EDITOR)
            policy.update(extra)
            assert serve.calendar_editor_granted(policy) is True
            assert serve.is_calendar_editor_policy(policy) is False, policy


# ── 2. capability declaration + routing ────────────────────────────────

class TestCapability:
    def test_declared_and_routed(self):
        decl = C.CAPABILITY_DECLARATIONS["calendar_editor"]
        assert decl["capabilities"] == ("calendar.delete",)
        assert decl["read_only"] is False
        assert C.CAPABILITY_ROUTING["calendar.delete"] == "calendar_editor"

    def test_reader_and_writer_unchanged(self):
        assert C.CAPABILITY_DECLARATIONS["calendar_bot"]["capabilities"] == (
            "calendar.read",)
        assert C.CAPABILITY_DECLARATIONS["calendar_bot"]["read_only"] is True
        assert C.CAPABILITY_DECLARATIONS["calendar_writer"]["capabilities"] == (
            "calendar.create",)


# ── 3. intent normalisation ────────────────────────────────────────────

class TestIntent:
    def test_exact_id_form(self):
        intent = cal_editor.normalize_delete_request(
            f"delete calendar event id {L6_ID}")
        assert intent == {"event_id": L6_ID, "title": None}

    def test_title_form_from_the_canonical_sentence(self):
        intent = cal_editor.normalize_delete_request(
            "Remove this from calendar " + L6_TITLE)
        assert intent == {"event_id": None, "title": L6_TITLE}

    def test_empty_and_unreadable_requests_fail_closed(self):
        for bad in ("", "   ", "please do something", "what is on my calendar"):
            with pytest.raises(cal_editor.CalendarEditorError):
                cal_editor.normalize_delete_request(bad)


# ── 4. exact-ID targeting + ambiguous-title disambiguation ─────────────

def _events():
    return [
        {"id": L6_ID, "summary": L6_TITLE,
         "start": {"date": "2026-01-05"}},
        {"id": "dupOne0001", "summary": "Dentist",
         "start": {"dateTime": "2026-01-06T09:00:00"}},
        {"id": "dupTwo0002", "summary": "dentist",
         "start": {"dateTime": "2026-01-07T09:00:00"}},
        {"id": "misc000001", "summary": "Lunch with the Level 6 coach",
         "start": {"dateTime": "2026-01-08T12:00:00"}},
    ]


class TestTargeting:
    def test_exact_id_wins(self):
        intent = {"event_id": L6_ID}
        assert cal_editor.target_event(_events(), intent)["id"] == L6_ID

    def test_single_title_match(self):
        got = cal_editor.target_event(_events(), {"title": L6_TITLE})
        assert got["id"] == L6_ID

    def test_ambiguous_title_is_never_guessed(self):
        with pytest.raises(cal_editor.CalendarEditorError) as exc:
            cal_editor.target_event(_events(), {"title": "Dentist"})
        assert "2 events" in str(exc.value)
        assert "dupOne0001" in str(exc.value) and "dupTwo0002" in str(exc.value)

    def test_unknown_id_and_title_fail_closed(self):
        with pytest.raises(cal_editor.CalendarEditorError):
            cal_editor.target_event(_events(), {"event_id": "noSuchId0001"})
        with pytest.raises(cal_editor.CalendarEditorError):
            cal_editor.target_event(_events(), {"title": "Nonexistent"})


# ── 5. preview + Level 6 evidence rule ─────────────────────────────────

class TestPreview:
    def test_level6_claim_requires_the_title_evidence(self):
        good = _events()[0]
        preview = cal_editor.build_preview(good)
        assert preview["level6"] is True
        assert preview["level6_evidence"] == cal_editor.LEVEL6_TITLE_PREFIX
        assert "Level 6 workout" in cal_editor.preview_display(preview)

    def test_bare_level6_mention_is_not_evidence(self):
        # mentions "Level 6" but the title carries no evidence -> NOT Level 6
        event = _events()[3]
        preview = cal_editor.build_preview(event)
        assert preview["level6"] is False
        assert preview["level6_evidence"] is None
        assert "not a Level 6 workout" in cal_editor.preview_display(preview)

    def test_preview_is_user_visible_and_targets_the_id(self):
        preview = cal_editor.build_preview(_events()[0])
        assert preview["event_id"] == L6_ID
        assert preview["title"] == L6_TITLE
        assert preview["when"] == "2026-01-05 (all day)"
        display = cal_editor.preview_display(preview)
        assert L6_ID in display and L6_TITLE in display

    def test_approval_tier_is_two(self):
        assert cal_editor.APPROVAL_TIER == 2


# ── 6. the connector boundary (separate write scope; exact id) ─────────

class _FakeStore:
    def __init__(self, scopes, connected=True):
        self._scopes = list(scopes)
        self._connected = connected

    def route_capability(self, owner, cap, provider="google"):
        role = C.CAPABILITY_ROUTING.get(cap)
        if role is None:
            raise C.ConnectorUnavailable(f"capability {cap!r} unsupported")
        return {"capability": cap, "connector": provider, "bot_role": role,
                "read_only": C.CAPABILITY_DECLARATIONS[role]["read_only"],
                "available": True}

    def status(self, owner, provider="google"):
        return {"connected": self._connected, "scopes": list(self._scopes)}

    def access_token(self, owner, provider="google"):
        return "fake-access-token"

    def preferred_calendar(self, owner, provider="google"):
        return "primary"


class TestConnectorBoundary:
    def test_reader_read_only_token_can_never_delete(self):
        store = _FakeStore(scopes=[C.GOOGLE_CALENDAR_READ_SCOPE])
        edit = C.CalendarEdit(store, "owner", transport=lambda *a, **k: {})
        with pytest.raises(C.ConnectorUnavailable):
            edit.delete_event(L6_ID)

    def test_delete_targets_the_exact_event_id(self):
        store = _FakeStore(scopes=[C.GOOGLE_CALENDAR_WRITE_SCOPE])
        seen = {}

        def transport(method, url, token, params=None, body=None):
            seen.update(method=method, url=url, body=body)
            return {}

        out = C.CalendarEdit(store, "owner", transport=transport).delete_event(L6_ID)
        assert out == {"deleted": True, "id": L6_ID}
        assert seen["method"] == "DELETE"
        assert seen["url"].endswith(f"/calendars/primary/events/{L6_ID}")
        assert seen["body"] is None

    def test_empty_or_malformed_id_fails_closed_before_any_call(self):
        store = _FakeStore(scopes=[C.GOOGLE_CALENDAR_WRITE_SCOPE])
        calls = []
        edit = C.CalendarEdit(
            store, "owner", transport=lambda *a, **k: calls.append(a))
        for bad in ("", "   ", "has space", "x" * 2000):
            with pytest.raises(C.ConnectorError):
                edit.delete_event(bad)
        assert calls == []


# ── 7. executor: executor boundary + the T2 approval gate ──────────────

def _run_executor(task, stdin, owner="owner"):
    env = dict(os.environ)
    if owner is None:
        env.pop("KYREX_BOT_OWNER", None)
    else:
        env["KYREX_BOT_OWNER"] = owner
    return subprocess.run(
        [sys.executable, os.path.join(_HERE, "calendar_editor_executor.py"),
         "--task", task],
        input=stdin, capture_output=True, text=True, env=env, timeout=30)


def _result(stdout):
    for line in stdout.splitlines():
        if line.startswith("KYREX_RESULT_JSON:"):
            return json.loads(line.split(":", 1)[1])
    return None


def _approval(stdout):
    for line in stdout.splitlines():
        if line.startswith("KYREX_APPROVAL:"):
            return json.loads(line.split(":", 1)[1])
    return None


class TestExecutor:
    def test_title_text_is_refused_at_the_executor_boundary(self):
        proc = _run_executor("Remove this from calendar " + L6_TITLE, "ALLOW\nAPPROVED\n")
        result = _result(proc.stdout)
        assert result["status"] == "error"
        assert "exact event id" in result["errors"][0]

    def test_host_policy_deny_means_no_preview_and_no_delete(self):
        proc = _run_executor(L6_ID, "DENY\n")
        result = _result(proc.stdout)
        assert result["status"] == "error"
        assert "denied by host policy" in result["errors"][0]
        assert _approval(proc.stdout) is None

    def test_t2_gate_denial_deletes_nothing(self):
        proc = _run_executor(L6_ID, "ALLOW\nDENY\n")
        result = _result(proc.stdout)
        assert result["status"] == "error"
        assert "not approved" in result["errors"][0]
        approval = _approval(proc.stdout)
        assert approval is not None
        assert approval["tier"] == 2
        assert L6_ID in approval["detail"]
        assert "Deleted" not in proc.stdout

    def test_missing_owner_fails_closed(self):
        proc = _run_executor(L6_ID, "ALLOW\nAPPROVED\n", owner=None)
        result = _result(proc.stdout)
        assert result["status"] == "error"
        assert "owner-scoped" in result["errors"][0]



# ── 8. stale event / provider failure AFTER approval ───────────────────

class TestStaleEvent:
    def test_provider_404_after_approval_is_a_safe_no_receipt_failure(self):
        """A provider 404 at DELETE time (the event vanished after approval)
        fails closed: no receipt, no success claim."""
        store = _FakeStore(scopes=[C.GOOGLE_CALENDAR_WRITE_SCOPE])

        def transport(*_a, **_k):
            # default_transport maps a provider HTTPError (e.g. 404) to
            # ConnectorUnavailable, so a stale id can never look deleted.
            raise C.ConnectorUnavailable("provider call failed: HTTPError")

        edit = C.CalendarEdit(store, "owner", transport=transport)
        with pytest.raises(C.ConnectorUnavailable):
            edit.delete_event(L6_ID)


def test_executor_after_approval_provider_failure_returns_no_receipt(tmp_path):
    """End to end: approval granted, but the provider call fails closed -> the
    executor emits an ERROR result and NO 'Deleted' receipt."""
    env = dict(os.environ)
    env["KYREX_BOT_OWNER"] = "alice"
    env["KYREX_DATA_DIR"] = str(tmp_path)   # no stored connector -> fail closed
    proc = subprocess.run(
        [sys.executable, os.path.join(_HERE, "calendar_editor_executor.py"),
         "--task", L6_ID],
        input="ALLOW\nAPPROVED\n", capture_output=True, text=True, env=env,
        timeout=30)
    result = _result(proc.stdout)
    assert result is not None and result["status"] == "error"
    assert "Deleted" not in proc.stdout
