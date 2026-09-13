"""Dedicated tests for managed Browser Sessions (``browser_sessions.py``).

A managed browser session is the durable identity of ONE Bot's isolated
browser "computer", keyed by ``(owner, bot_id)``. This suite pins the contract
the Browser Session phase relies on:

  1. state machine  — every legal transition, and that illegal ones raise.
  2. isolation      — ``(owner, bot_id)`` is the key; records/dirs never shared.
  3. sealing        — credential metadata is Fernet-sealed; a missing key FAILS
                      CLOSED (a plaintext credential is never written).
  4. redaction      — ``public_view`` never carries the token or the plaintext.
  5. reconnect/expiry — a live session reconnects; a dead one is retired.
  6. retention      — terminal records are purged only after the window.
  7. preservation   — detaching keeps the session LIVE, so a parked approval's
                      session + sealed metadata survive a reconnect; a UI going
                      away never ends the session.
  8. traversal      — session ids/paths cannot escape the session root.

Fully deterministic: every lifecycle call is driven by an explicit ``now``.

Run: python3 -m pytest test_browser_sessions.py
"""

import json
import os
import sys

import pytest

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import browser_sessions as bs  # noqa: E402

SECRET = "browser-session-test-secret"
PLAINTEXT = "PARKED-APPROVAL-REF-9f2a"
T = 1_700_000_000.0


# ── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """A fresh data dir plus a configured seal key for every test."""
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_SESSION_SECRET", SECRET)
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    yield tmp_path


def _index_records():
    path = bs._index_path()
    if not path.exists():
        return []
    return json.loads(path.read_text())


def _record(session_id):
    return [r for r in _index_records() if r["session_id"] == session_id][0]


# ── 1. state machine ─────────────────────────────────────────────────

def test_new_session_starts_in_starting():
    s = bs.create_session("owner", "bot", now=T)
    assert s.state == bs.STATE_STARTING
    assert s.session_id
    assert s.is_live and not s.is_terminal


def test_full_legal_transition_path():
    s, reused = bs.get_or_create("owner", "bot", now=T)
    assert s.state == bs.STATE_STARTING and reused is False

    s2, reused2 = bs.get_or_create("owner", "bot", now=T + 1)
    assert s2.state == bs.STATE_CONNECTED and reused2 is True
    assert s2.session_id == s.session_id

    d = bs.mark_disconnected("owner", "bot", now=T + 2)
    assert d.state == bs.STATE_DISCONNECTED

    c = bs.mark_connected("owner", "bot", now=T + 3)
    assert c.state == bs.STATE_CONNECTED

    assert bs.end_session("owner", "bot", now=T + 4) is True
    assert bs.get_session("owner", "bot", now=T + 4).state == bs.STATE_ENDED


def test_invalid_transitions_raise():
    # From the pre-creation state (None) only "starting" is legal.
    rec = {"state": None}
    bs._transition(rec, bs.STATE_STARTING, T)
    assert rec["state"] == bs.STATE_STARTING
    for bad in (bs.STATE_CONNECTED, bs.STATE_DISCONNECTED,
                bs.STATE_EXPIRED, bs.STATE_ENDED):
        with pytest.raises(bs.SessionError):
            bs._transition({"state": None}, bad, T)

    # "ended" is terminal: nothing may follow it.
    for bad in bs.STATES:
        with pytest.raises(bs.SessionError):
            bs._transition({"state": bs.STATE_ENDED}, bad, T)

    # "expired" may only proceed to "ended".
    with pytest.raises(bs.SessionError):
        bs._transition({"state": bs.STATE_EXPIRED}, bs.STATE_CONNECTED, T)
    ok = {"state": bs.STATE_EXPIRED}
    bs._transition(ok, bs.STATE_ENDED, T)
    assert ok["state"] == bs.STATE_ENDED


def test_invalid_transition_leaves_record_untouched():
    rec = {"state": bs.STATE_ENDED, "updated_at": 123.0}
    with pytest.raises(bs.SessionError):
        bs._transition(rec, bs.STATE_CONNECTED, T)
    assert rec == {"state": bs.STATE_ENDED, "updated_at": 123.0}


def test_terminal_session_is_superseded_not_reused():
    a, _ = bs.get_or_create("owner", "bot", now=T)
    bs.end_session("owner", "bot", now=T + 1)
    b, reused = bs.get_or_create("owner", "bot", now=T + 2)
    assert reused is False
    assert b.session_id != a.session_id
    assert b.state == bs.STATE_STARTING


def test_create_supersedes_a_live_session():
    a = bs.create_session("owner", "bot", now=T)
    b = bs.create_session("owner", "bot", now=T + 1)
    assert b.session_id != a.session_id
    assert _record(a.session_id)["state"] == bs.STATE_ENDED


def test_end_is_idempotent_and_missing_is_false():
    bs.get_or_create("owner", "bot", now=T)
    assert bs.end_session("owner", "bot", now=T + 1) is True
    assert bs.end_session("owner", "bot", now=T + 2) is False
    assert bs.end_session("owner", "bot", now=T + 3) is False
    assert bs.end_session("owner", "missing", now=T) is False


def test_require_key_rejects_ownerless_or_botless():
    for owner, bot in (("", "b"), ("o", ""), ("", "")):
        with pytest.raises(bs.SessionError):
            bs.get_session(owner, bot, now=T)
        with pytest.raises(bs.SessionError):
            bs.end_session(owner, bot, now=T)


def test_mark_and_get_on_missing_session_are_safe():
    assert bs.get_session("o", "none", now=T) is None
    assert bs.mark_disconnected("o", "none", now=T) is None
    assert bs.mark_connected("o", "none", now=T) is None


# ── 2. owner + Bot isolation ─────────────────────────────────────────

def test_owner_and_bot_isolation():
    ab = bs.get_or_create("ownerA", "bot", now=T)[0]
    ob = bs.get_or_create("ownerB", "bot", now=T)[0]
    a2 = bs.get_or_create("ownerA", "bot2", now=T)[0]
    assert len({ab.session_id, ob.session_id, a2.session_id}) == 3

    again, reused = bs.get_or_create("ownerA", "bot", now=T + 1)
    assert reused is True and again.session_id == ab.session_id


def test_session_dirs_are_isolated_to_the_key():
    a = bs.get_or_create("ownerA", "bot", now=T)[0]
    b = bs.get_or_create("ownerB", "bot", now=T)[0]
    assert bs.session_dir(a.session_id) != bs.session_dir(b.session_id)
    root = bs._root()
    assert root in bs.session_dir(a.session_id).parents


# ── 3. encrypted metadata / sealing + fail-closed ────────────────────

def test_metadata_is_sealed_not_plaintext():
    meta = {"approval": PLAINTEXT}
    s = bs.create_session("o", "b", metadata=meta, now=T)
    blob = _record(s.session_id)["sealed"]
    assert isinstance(blob, str) and blob
    assert PLAINTEXT not in blob
    assert PLAINTEXT not in bs._index_path().read_text()
    assert bs.unseal_metadata(blob) == meta
    assert s.has_sealed_metadata() is True


def test_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    with pytest.raises(bs.SessionError):
        bs.seal_metadata({"approval": PLAINTEXT})
    with pytest.raises(bs.SessionError):
        bs.create_session("o", "b", metadata={"approval": PLAINTEXT}, now=T)
    # Nothing was written: no record, and certainly no plaintext.
    assert not bs._index_path().exists()


def test_lifecycle_without_credential_needs_no_key(monkeypatch):
    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    s = bs.create_session("o", "b", now=T)
    assert s.state == bs.STATE_STARTING
    assert s.has_sealed_metadata() is False


def test_unseal_with_rotated_key_returns_empty(monkeypatch):
    s = bs.create_session("o", "b", metadata={"approval": PLAINTEXT}, now=T)
    blob = _record(s.session_id)["sealed"]
    monkeypatch.setenv("WEB_SESSION_SECRET", "rotated-secret")
    assert bs.unseal_metadata(blob) == {}  # fail closed, never raise


def test_unseal_handles_absent_and_garbage():
    assert bs.unseal_metadata(None) == {}
    assert bs.unseal_metadata("") == {}
    assert bs.unseal_metadata("not-a-token") == {}


# ── 4. public_view secret redaction ──────────────────────────────────

def test_public_view_never_leaks_the_secret():
    s = bs.create_session("o", "b", metadata={"approval": PLAINTEXT}, now=T)
    view = bs.public_view(s, now=T)
    assert "sealed" not in view and "metadata" not in view
    assert view["has_credential"] is True
    assert PLAINTEXT not in json.dumps(view)
    assert not any("approval" in k for k in view)


def test_public_view_has_credential_false_without_metadata():
    s = bs.create_session("o", "b", now=T)
    assert bs.public_view(s, now=T)["has_credential"] is False


def test_public_view_reports_effective_state_past_deadline():
    s = bs.create_session("o", "b", now=T, ttl=10)
    view = bs.public_view(s, now=T + 11)
    assert view["state"] == bs.STATE_EXPIRED
    assert view["reusable"] is False


def test_public_view_accepts_a_raw_record():
    bs.create_session("o", "b", now=T)
    view = bs.public_view(_index_records()[0], now=T)
    assert view["session_id"]
    assert "sealed" not in view


# ── 5. reconnect and expiry ──────────────────────────────────────────

def test_reconnect_reuses_and_refreshes_deadline():
    a, _ = bs.get_or_create("o", "b", now=T, ttl=100)
    r = bs.reconnect("o", "b", now=T + 50, ttl=100)
    assert r.session_id == a.session_id
    assert r.state == bs.STATE_CONNECTED
    assert r.expires_at == (T + 50) + 100


def test_expired_session_is_retired_and_superseded():
    a, _ = bs.get_or_create("o", "b", now=T, ttl=100)
    b, reused = bs.get_or_create("o", "b", now=T + 1000, ttl=100)
    assert reused is False and b.session_id != a.session_id
    assert _record(a.session_id)["state"] == bs.STATE_EXPIRED


def test_expire_stale_sweeps_and_is_idempotent():
    bs.create_session("o", "b", now=T, ttl=10)
    swept = bs.expire_stale(now=T + 11)
    assert len(swept) == 1
    assert swept[0]["state"] == bs.STATE_EXPIRED
    assert "sealed" not in swept[0]  # public views only
    assert bs.expire_stale(now=T + 11) == []


def test_get_session_is_read_only_on_expiry():
    bs.create_session("o", "b", now=T, ttl=10)
    s = bs.get_session("o", "b", now=T + 11)
    assert s.effective_state(now=T + 11) == bs.STATE_EXPIRED
    assert s.is_reusable(now=T + 11) is False
    # A read did NOT mutate the stored record.
    assert _record(s.session_id)["state"] == bs.STATE_STARTING


# ── 6. retention cleanup ─────────────────────────────────────────────

def test_cleanup_removes_terminal_records_after_retention():
    s = bs.create_session("o", "b", now=T)
    bs.end_session("o", "b", now=T)
    d = bs.session_dir(s.session_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "profile").write_text("x")

    assert bs.cleanup(now=T + 1, retention=100) == []   # too soon
    assert d.exists()

    removed = bs.cleanup(now=T + 200, retention=100)
    assert removed == [s.session_id]
    assert not d.exists()
    assert _index_records() == []


def test_cleanup_keeps_live_sessions():
    bs.create_session("o", "b", now=T, ttl=100000)
    assert bs.cleanup(now=T + 100, retention=1) == []
    assert bs.get_session("o", "b", now=T + 100) is not None


def test_cleanup_expires_before_purging():
    # A past-deadline live session is EXPIRED (kept for retention), not purged.
    bs.create_session("o", "b", now=T, ttl=10)
    assert bs.cleanup(now=T + 20, retention=1000) == []
    assert bs.get_session("o", "b", now=T + 20).state == bs.STATE_EXPIRED


# ── 7. active / parked-approval session preservation ─────────────────

def test_detach_keeps_session_live_and_preserves_metadata():
    meta = {"approval": PLAINTEXT}
    s, _ = bs.get_or_create("o", "b", metadata=meta, now=T)
    d = bs.session_dir(s.session_id)
    d.mkdir(parents=True, exist_ok=True)

    detached = bs.mark_disconnected("o", "b", now=T + 1)
    assert detached.state == bs.STATE_DISCONNECTED
    assert detached.state in bs.LIVE_STATES       # NOT ended by detaching
    assert detached.is_reusable(now=T + 2) is True
    # The parked-approval reference survives the detach...
    assert bs.unseal_metadata(_record(s.session_id)["sealed"]) == meta

    # ...and a reconnect returns the SAME session, credential intact.
    back = bs.reconnect("o", "b", now=T + 2)
    assert back.session_id == s.session_id
    assert back.state == bs.STATE_CONNECTED
    assert back.has_sealed_metadata() is True
    assert back.metadata == meta
    # The browser profile directory survives too (reconnect, not start-cold).
    assert d.exists()


def test_reconnect_does_not_erase_sealed_metadata():
    meta = {"approval": PLAINTEXT}
    bs.get_or_create("o", "b", metadata=meta, now=T)
    # A reconnect WITHOUT metadata must not wipe the parked reference.
    back = bs.reconnect("o", "b", now=T + 1)
    assert back.metadata == meta
    assert back.has_sealed_metadata() is True


# ── 8. path / identifier traversal rejection ─────────────────────────

def test_session_dir_confines_traversal_inputs():
    root = bs._root()
    for evil in ("../../evil", "..", "../..", "/etc/passwd",
                 "a/../../b", "....//....//x"):
        p = bs.session_dir(evil)
        assert ".." not in p.parts
        assert str(p.resolve()).startswith(str(root.resolve()))


def test_remove_dir_refuses_non_matching_ids():
    s = bs.create_session("o", "b", now=T)
    d = bs.session_dir(s.session_id)
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "keep"
    marker.write_text("x")

    for evil in ("../" + s.session_id, "/" + s.session_id,
                 s.session_id + " x", "..", ""):
        bs._remove_dir(evil)
    assert marker.exists()            # nothing removed

    bs._remove_dir(s.session_id)      # a well-formed id IS removed
    assert not d.exists()


def test_session_id_regex_rejects_bad_ids():
    for bad in ("..", "a/b", "a b", "", "x" * 65, "a;b"):
        assert bs._SESSION_ID_RE.match(bad) is None
    for good in ("abc", "A_b-1", "x" * 64):
        assert bs._SESSION_ID_RE.match(good) is not None


def test_session_env_carries_no_secret():
    s = bs.create_session("o", "b", metadata={"approval": PLAINTEXT}, now=T)
    env = bs.session_env(s)
    assert env["KYREX_BROWSER_SESSION_ID"] == s.session_id
    assert env["KYREX_BROWSER_SESSION_DIR"] == str(bs.session_dir(s.session_id))
    assert PLAINTEXT not in json.dumps(env)
