"""Connection-screen refresh and scope-preserving reconnect regressions."""
import io
import json
import urllib.error
import urllib.parse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import connectors
import connections_api


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "connection-refresh-test-secret")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://example.test/callback")
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    store = connectors.ConnectorStore(tmp_path / "connectors.json")
    monkeypatch.setattr(connections_api, "_DEFAULT_STORE", store)
    monkeypatch.setattr(connections_api, "_require_user", lambda request: "alice")
    monkeypatch.setattr(connections_api, "exchange_override", None)
    app = FastAPI()
    app.include_router(connections_api.router)
    return store, TestClient(app)


def connect(store, *, owner="alice", refresh="refresh-value", scopes=None):
    scopes = scopes or [connectors.GOOGLE_CALENDAR_READ_SCOPE,
                        connectors.GOOGLE_GMAIL_READ_SCOPE,
                        connectors.GOOGLE_CALENDAR_WRITE_SCOPE]
    begun = store.begin_oauth(owner, scopes=scopes, now=1)
    store.complete_oauth(owner, begun["state"], "fake-code", now=2,
                         exchange=lambda *_: {
                             "access_token": "access-value",
                             "refresh_token": refresh,
                             "expires_in": 1, "scope": " ".join(scopes)})


def test_hub_refreshes_expired_shared_grant_once_and_keeps_scopes(setup, monkeypatch):
    store, client = setup
    connect(store)
    calls = []
    monkeypatch.setattr(store, "_default_refresh", lambda token, config:
                        calls.append(token) or {
                            "access_token": "renewed-value", "expires_in": 3600})
    view = client.get("/api/connections").json()["connectors"][0]
    assert view["connected"] and view["usable"] and not view["expired"]
    assert view["has_gmail_scope"] and view["has_write_scope"]
    assert client.get("/api/connections").json()["connectors"][0]["usable"]
    assert calls == ["refresh-value"]
    assert not (set(view) & {"access_token", "refresh_token", "sealed"})
    assert "renewed-value" not in json.dumps(view)
    assert "renewed-value" not in store.path.read_text()


def test_reconnect_preserves_prior_scopes_without_adding_new_grants(setup):
    store, client = setup
    scopes = [connectors.GOOGLE_CALENDAR_READ_SCOPE,
              connectors.GOOGLE_GMAIL_READ_SCOPE]
    connect(store, scopes=scopes)
    begun = client.post("/api/connections/google/connect").json()
    assert set(begun["scopes"]) == set(scopes)
    assert connectors.GOOGLE_CALENDAR_WRITE_SCOPE not in begun["scopes"]
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(begun["authorization_url"]).query)
    assert set(qs["scope"][0].split()) == set(scopes)
    # A missing scope field uses the bounded request; a supplied subset wins.
    store.complete_oauth("alice", qs["state"][0], "fake-code",
                         exchange=lambda *_: {"access_token": "new-value",
                                              "refresh_token": "new-refresh",
                                              "expires_in": 3600})
    assert set(store.status("alice")["scopes"]) == set(scopes)


def test_reconnect_preserves_write_scope_and_respects_actual_consent(setup):
    store, client = setup
    connect(store)
    begun = client.post("/api/connections/google/connect").json()
    assert connectors.GOOGLE_CALENDAR_WRITE_SCOPE in begun["scopes"]
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(begun["authorization_url"]).query)
    store.complete_oauth("alice", qs["state"][0], "fake-code",
                         exchange=lambda *_: {"access_token": "new-value",
                                              "refresh_token": "new-refresh",
                                              "expires_in": 3600,
                                              "scope": connectors.GOOGLE_CALENDAR_READ_SCOPE})
    assert store.status("alice")["scopes"] == [connectors.GOOGLE_CALENDAR_READ_SCOPE]


def test_initial_and_disconnected_reconnect_stay_read_only(setup):
    store, client = setup
    assert client.post("/api/connections/google/connect").json()["scopes"] == list(connectors.GOOGLE_READ_SCOPES)
    connect(store)
    store.disconnect("alice")
    assert client.post("/api/connections/google/connect").json()["scopes"] == list(connectors.GOOGLE_READ_SCOPES)


@pytest.mark.parametrize("kind", ["timeout", "rate_limit", "server", "invalid_client", "malformed"])
def test_temporary_refresh_failure_offers_retry_and_preserves_grant(setup, monkeypatch, kind):
    store, client = setup
    connect(store)
    before = store.path.read_bytes()
    def unavailable(*args, **kwargs):
        if kind == "timeout":
            raise TimeoutError("provider secret must not escape")
        code = {"rate_limit": 429, "server": 503, "invalid_client": 400, "malformed": 400}[kind]
        body = b"unparseable secret" if kind == "malformed" else b'{"error":"invalid_client","secret":"do-not-display"}'
        raise urllib.error.HTTPError("https://oauth2.googleapis.com/token", code,
                                     "provider-secret", {}, io.BytesIO(body))
    monkeypatch.setattr(connectors.urllib.request, "urlopen", unavailable)
    view = client.get("/api/connections").json()["connectors"][0]
    assert view["connected"] and not view["expired"] and not view["usable"]
    assert view["temporarily_unavailable"]
    assert view["has_gmail_scope"] and view["has_write_scope"]
    assert "secret" not in json.dumps(view)
    assert store.path.read_bytes() == before
    monkeypatch.setattr(store, "_default_refresh", lambda *_: {
        "access_token": "retry-value", "expires_in": 3600})
    recovered = client.get("/api/connections").json()["connectors"][0]
    assert recovered["usable"] and not recovered.get("temporarily_unavailable")


@pytest.mark.parametrize("refresh", ["", "revoked"])
def test_missing_or_revoked_refresh_requires_reconnect(setup, monkeypatch, refresh):
    store, client = setup
    connect(store, refresh=refresh)
    calls = []
    def revoked(*args, **kwargs):
        calls.append(1)
        raise urllib.error.HTTPError("https://oauth2.googleapis.com/token", 400,
                                     "no-echo", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
    monkeypatch.setattr(connectors.urllib.request, "urlopen", revoked)
    view = client.get("/api/connections").json()["connectors"][0]
    assert view["expired"] and not view["usable"]
    assert not view.get("temporarily_unavailable")
    assert len(calls) == bool(refresh)


def test_status_refresh_is_owner_scoped(setup, monkeypatch):
    store, client = setup
    connect(store, owner="bob")
    monkeypatch.setattr(store, "_default_refresh", lambda *_: pytest.fail("must not refresh Bob's grant"))
    view = client.get("/api/connections").json()["connectors"][0]
    assert not view["connected"]
    assert client.post("/api/connections/google/connect").json()["scopes"] == list(connectors.GOOGLE_READ_SCOPES)


def test_refresh_cannot_overwrite_new_oauth_grant(setup):
    store, _ = setup
    connect(store)
    def reauthorized(*_):
        begun = store.begin_oauth("alice")
        store.complete_oauth("alice", begun["state"], "fake-code", exchange=lambda *_: {
            "access_token": "replacement-value", "refresh_token": "replacement-refresh",
            "expires_in": 3600, "scope": connectors.GOOGLE_CALENDAR_READ_SCOPE})
        return {"access_token": "stale-refreshed-value", "expires_in": 3600}
    with pytest.raises(connectors.ConnectorRefreshTemporaryUnavailable, match="changed"):
        store.access_token("alice", refresh=reauthorized)
    assert store.access_token("alice") == "replacement-value"
    assert store.status("alice")["scopes"] == [connectors.GOOGLE_CALENDAR_READ_SCOPE]
