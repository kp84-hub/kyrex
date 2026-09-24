"""HTTP-level tests for the Gmail READER wiring in connections_api.py.

Drives the real FastAPI routes with TestClient, monkeypatching ONLY the owner
auth (so no full Cloud app/DB is needed), the token exchange (so no network),
and the provider transport (so no Google). Verifies the Gmail slice end-to-end
through the HTTP surface:

  * GET /api/connections advertises the gmail_bot read capability and derives
    ``has_gmail_scope`` from the stored grant (never a token);
  * POST /google/upgrade-gmail starts an OAuth round-trip that ADDS only the
    gmail.readonly scope, UNIONED with the owner's existing scopes (Calendar is
    preserved) and NEVER a mutation scope;
  * the Gmail reader routes return SAFE projections only -- ``{owner,id,
    thread_id}`` stubs, and ONE message's Subject/From/Date + snippet -- with no
    token / raw body / Authorization header, and read-only fail-closed (409) when
    the connector is not connected;
  * a granted gmail scope flips ``has_gmail_scope`` true but does NOT widen the
    Calendar behaviour;
  * a missing connector core fails closed (503).

Run either way:
  python3 -m pytest test_gmail_connections_api.py
  python3 test_gmail_connections_api.py
"""
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent                # web/backend/
CLOUD = HERE.parent.parent                            # kyrex-cloud/
for path in (str(CLOUD), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

# Connector environment BEFORE importing the module that reads it.
os.environ["WEB_SESSION_SECRET"] = "unit-test-gmail-api-secret"
os.environ["KYREX_DATA_DIR"] = tempfile.mkdtemp(prefix="kx-gmail-api-")
os.environ.setdefault("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "client-secret-value")
os.environ.setdefault("GOOGLE_REDIRECT_URI",
                      "https://kyrex.example/api/connections/google/callback")

import pytest                                    # noqa: E402
from fastapi import FastAPI                      # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402

import connectors as connectors_core             # noqa: E402
import connections_api                           # noqa: E402

CAL = connectors_core.GOOGLE_CALENDAR_READ_SCOPE
GMAIL = connectors_core.GOOGLE_GMAIL_READ_SCOPE
WRITE = connectors_core.GOOGLE_CALENDAR_WRITE_SCOPE
REDIRECT = os.environ["GOOGLE_REDIRECT_URI"]

# Fixed owner, swappable for the isolation case.
OWNER = {"id": "alice"}
connections_api._require_user = lambda request: OWNER["id"]


# ── helpers ────────────────────────────────────────────────────────────

def _exchange_for(scopes):
    def _ex(code, redirect, client):
        assert redirect == REDIRECT, redirect
        return {"access_token": "ya29.SECRET-ACCESS",
                "refresh_token": "1//SECRET-REFRESH", "expires_in": 3600,
                "scope": " ".join(scopes)}
    return _ex


class _TransportStore(connectors_core.ConnectorStore):
    """A real store whose Gmail reader uses an injected transport."""

    def __init__(self, transport, path=None):
        super().__init__(path)
        self._tp = transport

    def gmail(self, owner, *, transport=None, provider="google"):
        return super().gmail(owner, transport=transport or self._tp,
                             provider=provider)


def _client(store):
    connections_api._DEFAULT_STORE = store
    app = FastAPI()
    app.include_router(connections_api.router)
    return TestClient(app)


def _state_from(started):
    qs = urllib.parse.parse_qs(
        urllib.parse.urlparse(started["authorization_url"]).query)
    return qs["state"][0]


def _connect_with_gmail(client, scopes=(CAL, GMAIL)):
    """Complete the Gmail read upgrade so the owner holds the gmail scope."""
    connections_api.exchange_override = _exchange_for(list(scopes))
    started = client.post("/api/connections/google/upgrade-gmail").json()
    state = _state_from(started)
    r = client.get("/api/connections/google/callback",
                   params={"state": state, "code": "authcode"})
    assert r.status_code == 200, r.status_code
    return started


# ── 1. status advertises the gmail capability + has_gmail_scope ────────

def test_view_advertises_gmail_capability_and_scope_flag():
    client = _client(connectors_core.ConnectorStore())
    body = client.get("/api/connections").json()
    view = body["connectors"][0]
    assert "has_gmail_scope" not in view.get("sealed", {})  # no sealed blob
    assert view.get("has_gmail_scope") is False, view
    bots = view["capabilities"]["bots"]
    assert bots["gmail_bot"]["capabilities"] == ["gmail.read"]
    assert bots["gmail_bot"]["read_only"] is True
    for cap in ("gmail.send", "gmail.delete", "gmail.archive",
                "gmail.modify", "gmail.label"):
        assert cap in bots["gmail_bot"]["unsupported"]
    assert bots["gmail_bot"]["read_only"] is True
    assert not (set(view) & {"sealed", "access_token", "refresh_token",
                             "client_secret"})


# ── 2. upgrade-gmail unions scopes, adds ONLY gmail.readonly ───────────

def test_upgrade_gmail_returns_read_scope_and_preserves_calendar():
    store = connectors_core.ConnectorStore()
    client = _client(store)
    # A Calendar-connected owner first, so we can prove the union.
    connections_api.exchange_override = _exchange_for([CAL])
    plain = client.post("/api/connections/google/connect").json()
    client.get("/api/connections/google/callback",
               params={"state": _state_from(plain), "code": "c"})

    started = client.post("/api/connections/google/upgrade-gmail").json()
    assert started["read_scope"] == GMAIL
    assert set(started["scopes"]) == {CAL, GMAIL}, started["scopes"]
    assert started["authorization_url"]
    assert not (set(started) & {"access_token", "refresh_token",
                                "client_secret", "sealed", "code"})
    # The union guarantees Calendar was preserved, never dropped.
    assert CAL in started["scopes"]


def test_upgrade_gmail_never_requests_a_mutation_scope():
    client = _client(connectors_core.ConnectorStore())
    connections_api.exchange_override = _exchange_for([CAL, GMAIL])
    started = client.post("/api/connections/google/upgrade-gmail").json()
    allowed = connectors_core.GOOGLE_ALLOWED_SCOPES
    assert set(started["scopes"]) <= set(allowed)
    for mutation in ("gmail.send", "gmail.modify", "gmail.compose",
                     "gmail.labels"):
        assert not any(mutation in s for s in started["scopes"])
    assert WRITE not in started["scopes"], "gmail upgrade never adds calendar write"


# ── 3. a completed gmail grant flips has_gmail_scope (and only that) ───

def test_granted_gmail_scope_reflects_in_status():
    store = connectors_core.ConnectorStore()
    client = _client(store)
    _connect_with_gmail(client)
    view = client.get("/api/connections").json()["connectors"][0]
    assert view["has_gmail_scope"] is True, view
    assert CAL in view["scopes"]
    assert GMAIL in view["scopes"]
    # Calendar write was never involved.
    assert view["has_write_scope"] is False


# ── 4. the Gmail reader routes return SAFE projections ─────────────────

SEEN = {}


def _transport(method, url, token, params=None, body=None):
    SEEN.update(method=method, url=url, params=params)
    if url.endswith("/users/me/messages"):
        return {"messages": [{"id": "m1", "threadId": "t1",
                              "snippet": "SECRET should not surface"}]}
    return {
        "id": "m1", "threadId": "t1", "snippet": "hi",
        "payload": {"headers": [
            {"name": "Subject", "value": "Hello"},
            {"name": "From", "value": "someone@example.com"},
            {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            {"name": "Authorization", "value": "Bearer ya29.LEAK"},
        ]},
        "access_token": "ya29.LEAK-TOKEN", "raw": "SECRET-BODY",
    }


def test_gmail_search_route_returns_safe_stubs():
    store = _TransportStore(_transport)
    client = _client(store)
    _connect_with_gmail(client)

    r = client.get("/api/connections/google/gmail/search",
                   params={"q": "from:me", "max_results": 5})
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body["read_only"] is True
    assert body["messages"] == [{"owner": "alice", "id": "m1",
                                 "thread_id": "t1"}], body
    assert "snippet" not in str(body)
    assert SEEN["url"].endswith("/users/me/messages")
    assert SEEN["params"].get("q") == "from:me"


def test_gmail_message_route_returns_safe_projection():
    store = _TransportStore(_transport)
    client = _client(store)
    _connect_with_gmail(client)

    r = client.get("/api/connections/google/gmail/message/m1")
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body["read_only"] is True
    msg = body["message"]
    assert msg["id"] == "m1"
    assert msg["headers"].get("Subject") == "Hello"
    blob = str(body)
    assert "ya29" not in blob, blob
    assert "SECRET-BODY" not in blob, blob
    assert "Authorization" not in msg["headers"]
    # Metadata format only.
    assert SEEN["params"].get("format") == "metadata"


def test_gmail_message_bad_id_fails_closed_400():
    store = _TransportStore(_transport)
    client = _client(store)
    _connect_with_gmail(client)
    r = client.get("/api/connections/google/gmail/message/" + ("x" * 600))
    assert r.status_code == 400, (r.status_code, r.text[:120])


# ── 5. the reader fails closed (409) when not connected ────────────────

def test_gmail_reader_fails_closed_when_not_connected():
    client = _client(connectors_core.ConnectorStore())
    r = client.get("/api/connections/google/gmail/search")
    assert r.status_code == 409, (r.status_code, r.text[:120])
    r = client.get("/api/connections/google/gmail/message/m1")
    assert r.status_code == 409, (r.status_code, r.text[:120])


def test_gmail_reader_fails_closed_without_the_gmail_scope():
    # A Calendar-only grant can never read mail through the reader routes.
    store = connectors_core.ConnectorStore()
    client = _client(store)
    connections_api.exchange_override = _exchange_for([CAL])
    plain = client.post("/api/connections/google/connect").json()
    client.get("/api/connections/google/callback",
               params={"state": _state_from(plain), "code": "c"})
    r = client.get("/api/connections/google/gmail/search")
    assert r.status_code == 409, (r.status_code, r.text[:120])


# ── 6. missing connector core fails closed (503) ───────────────────────

def test_missing_core_fails_closed_503():
    connections_api._DEFAULT_STORE = None
    saved = connections_api.connectors_core
    connections_api.connectors_core = None
    try:
        app = FastAPI()
        app.include_router(connections_api.router)
        client = TestClient(app)
        assert client.get("/api/connections").status_code == 503
        assert client.post(
            "/api/connections/google/upgrade-gmail").status_code == 503
        assert client.get(
            "/api/connections/google/gmail/search").status_code == 503
    finally:
        connections_api.connectors_core = saved


if __name__ == "__main__":  # direct run; pytest imports the module instead
    sys.exit(pytest.main([__file__, "-q"]))
