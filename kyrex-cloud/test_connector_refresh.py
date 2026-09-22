import os

import pytest

import connectors


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "refresh-test-secret")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://example.test/callback")


def _connected(store, owner, *, access="old-access", refresh="refresh-secret"):
    data = store._read()
    data.setdefault("owners", {})[store._owner_key(owner)] = {
        "owner": owner,
        "providers": {
            "google": {
                "provider": "google",
                "owner": owner,
                "status": "connected",
                "scopes": [connectors.GOOGLE_CALENDAR_READ_SCOPE],
                "sealed": connectors.seal_tokens({
                    "access_token": access,
                    "refresh_token": refresh,
                    "scope": connectors.GOOGLE_CALENDAR_READ_SCOPE,
                    "token_type": "Bearer",
                }),
                "expires_at": 99,
                "updated_at": 1,
            }
        },
    }
    store._write(data)


def test_expired_token_refreshes_and_persists_encrypted(tmp_path):
    store = connectors.ConnectorStore(tmp_path / "connectors.json")
    _connected(store, "alice")
    calls = []

    token = store.access_token(
        "alice", now=100,
        refresh=lambda value, client: calls.append((value, client)) or {
            "access_token": "new-access", "expires_in": 3600,
        },
    )

    assert token == "new-access"
    assert calls[0][0] == "refresh-secret"
    assert store.status("alice")["expires_at"] == 3700
    raw = store.path.read_text()
    assert "new-access" not in raw
    assert "refresh-secret" not in raw
    sealed = connectors.unseal_tokens(store._record("alice", "google")["sealed"])
    assert sealed["refresh_token"] == "refresh-secret"


def test_refresh_is_owner_scoped(tmp_path):
    store = connectors.ConnectorStore(tmp_path / "connectors.json")
    _connected(store, "alice", access="alice-old", refresh="alice-refresh")
    _connected(store, "bob", access="bob-old", refresh="bob-refresh")
    bob_before = store._record("bob", "google")["sealed"]

    assert store.access_token(
        "alice", now=100,
        refresh=lambda *_: {"access_token": "alice-new", "expires_in": 60},
    ) == "alice-new"
    assert store._record("bob", "google")["sealed"] == bob_before


@pytest.mark.parametrize("response", [None, {}, {"access_token": "x"}])
def test_bad_refresh_fails_closed_without_mutation(tmp_path, response):
    store = connectors.ConnectorStore(tmp_path / "connectors.json")
    _connected(store, "alice")
    before = store.path.read_bytes()

    with pytest.raises(connectors.ConnectorUnavailable, match="refresh failed"):
        store.access_token("alice", now=100, refresh=lambda *_: response)
    assert store.path.read_bytes() == before


def test_expired_token_without_refresh_token_requires_reconnect(tmp_path):
    store = connectors.ConnectorStore(tmp_path / "connectors.json")
    _connected(store, "alice", refresh="")
    with pytest.raises(connectors.ConnectorUnavailable, match="reconnect"):
        store.access_token("alice", now=100, refresh=lambda *_: {})
