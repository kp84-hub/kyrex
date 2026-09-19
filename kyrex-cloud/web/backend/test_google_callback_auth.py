"""Focused regression tests: the Google OAuth BROWSER CALLBACK must
authenticate ONLY by validating and atomically consuming the single-use,
owner-bound, redirect-bound ``state`` -- never by a bearer token or the SPA's
session credential.

Regression under test: the callback previously resolved the owner with
``_require_user()`` (session/bearer), so a real Google redirect -- which
carries the ``state`` but neither credential -- could never complete, even
with a perfectly valid state.

Run: python3 -m pytest test_google_callback_auth.py
"""
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

os.environ["WEB_SESSION_SECRET"] = "google-callback-auth-test-secret"
os.environ["KYREX_DATA_DIR"] = tempfile.mkdtemp(prefix="kx-gcb-")
os.environ["GOOGLE_CLIENT_ID"] = "cid.apps.googleusercontent.com"
os.environ["GOOGLE_CLIENT_SECRET"] = "client-secret-value"
REDIRECT = "https://kyrex.example/api/connections/google/callback"
os.environ["GOOGLE_REDIRECT_URI"] = REDIRECT

HERE = Path(__file__).resolve().parent                 # web/backend/
CLOUD = HERE.parent.parent                             # kyrex-cloud/
for _p in (str(CLOUD), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest                                          # noqa: E402
from fastapi import FastAPI                            # noqa: E402
from fastapi.testclient import TestClient              # noqa: E402

import connections_api                                 # noqa: E402


def _app():
    app = FastAPI()
    app.include_router(connections_api.router)
    return app


def _drop_store_file():
    import connectors as connectors_core
    path = (Path(os.environ["KYREX_DATA_DIR"])
            / connectors_core.DEFAULT_CONNECTORS_FILE)
    try:
        path.unlink()
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _reset():
    _drop_store_file()
    connections_api._DEFAULT_STORE = None
    connections_api.exchange_override = None
    yield
    _drop_store_file()
    connections_api._DEFAULT_STORE = None
    connections_api.exchange_override = None


def _client():
    # Deliberately NO cookies and NO Authorization header: a provider
    # redirect carries neither, and the callback must not need them.
    return TestClient(_app())


def _fake_exchange(code, redirect, client):
    assert redirect == REDIRECT, redirect
    return {
        "access_token": "ya29.FAKE-ACCESS",
        "refresh_token": "1//FAKE-REFRESH",
        "expires_in": 3600,
        "scope": connections_api.connectors_core.GOOGLE_CALENDAR_READ_SCOPE,
    }


def _state_from(url):
    return urllib.parse.parse_qs(
        urllib.parse.urlparse(url).query)["state"][0]


# ── the fix: state alone authenticates the browser callback ────────────

def test_callback_succeeds_with_no_session_or_bearer(monkeypatch):
    """A valid state must complete the flow with NO auth credential present,
    and the callback must not even consult _require_user()."""
    def _must_not_be_called(request):
        raise AssertionError("callback must not require session/bearer auth")

    monkeypatch.setattr(connections_api, "_require_user", _must_not_be_called)

    # Mint a state directly (the authenticated /connect path is covered
    # elsewhere); the callback must derive the owner from this state.
    state = connections_api._store().begin_oauth("alice")["state"]
    connections_api.exchange_override = _fake_exchange

    resp = _client().get("/api/connections/google/callback",
                         params={"state": state, "code": "authcode"})
    assert resp.status_code == 200
    assert "connected" in resp.text.lower()
    # owner came from the state, not a session
    assert connections_api._store().status("alice")["connected"] is True


def test_full_connect_then_callback_without_session_cookie(monkeypatch):
    """End-to-end: /connect (authenticated) mints the state; the callback
    completes with a cookie-less client."""
    monkeypatch.setattr(connections_api, "_require_user",
                        lambda request: "alice")
    client = _client()
    started = client.post("/api/connections/google/connect")
    assert started.status_code == 200, started.text
    state = _state_from(started.json()["authorization_url"])
    connections_api.exchange_override = _fake_exchange

    cookie_free = TestClient(_app())            # brand-new, no cookies
    resp = cookie_free.get("/api/connections/google/callback",
                           params={"state": state, "code": "authcode"})
    assert resp.status_code == 200 and "connected" in resp.text.lower()


# ── the state is validated + consumed, fail-closed ─────────────────────

def test_callback_state_is_single_use(monkeypatch):
    monkeypatch.setattr(connections_api, "_require_user",
                        lambda request: "alice")
    client = _client()
    state = _state_from(
        client.post("/api/connections/google/connect").json()
        ["authorization_url"])
    connections_api.exchange_override = _fake_exchange

    first = client.get("/api/connections/google/callback",
                       params={"state": state, "code": "c1"})
    assert "connected" in first.text.lower()
    replay = client.get("/api/connections/google/callback",
                        params={"state": state, "code": "c2"})
    assert "no longer valid" in replay.text.lower()


def test_callback_unknown_state_fails_closed(monkeypatch):
    monkeypatch.setattr(connections_api, "_require_user",
                        lambda request: "alice")
    connections_api.exchange_override = _fake_exchange
    resp = _client().get("/api/connections/google/callback",
                         params={"state": "not-a-real-state", "code": "c"})
    assert "no longer valid" in resp.text.lower()


def test_callback_redirect_mismatch_fails_closed(monkeypatch):
    """A state bound to a different callback origin can never complete here."""
    monkeypatch.setattr(connections_api, "_require_user",
                        lambda request: "alice")
    state = connections_api._store().begin_oauth(
        "alice", redirect_uri="https://evil.example/cb")["state"]
    connections_api.exchange_override = _fake_exchange
    resp = _client().get("/api/connections/google/callback",
                         params={"state": state, "code": "c"})
    assert "no longer valid" in resp.text.lower()
    assert connections_api._store().status("alice")["connected"] is False


def test_callback_page_never_echoes_code_or_state(monkeypatch):
    monkeypatch.setattr(connections_api, "_require_user",
                        lambda request: "alice")
    connections_api.exchange_override = _fake_exchange
    state = connections_api._store().begin_oauth("alice")["state"]
    ok = _client().get("/api/connections/google/callback",
                       params={"state": state, "code": "4/secret-code"})
    bad = _client().get("/api/connections/google/callback",
                        params={"state": "bogus", "code": "4/secret-code"})
    for page in (ok.text, bad.text):
        assert "4/secret-code" not in page
        assert state not in page
