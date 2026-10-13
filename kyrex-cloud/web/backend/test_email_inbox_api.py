"""Owner authentication, bounded secrets and dedicated source controls."""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

import agent_inbox
import connections_api
import email_inbox_api as api

KEY = "email-api-test-credential-" + "x" * 32


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "inbox-api-test-encryption")

    def request(path, key, params=None):
        assert key == KEY
        return (
            {"messages": []}
            if path.endswith("/messages")
            else {"inbox_id": "inb_school", "email": "school@agentmail.to"}
        )

    store = agent_inbox.AgentInboxStore(tmp_path / "inbox.sqlite3", request)
    monkeypatch.setattr(agent_inbox, "default_store", lambda: store)
    monkeypatch.setattr(
        connections_api,
        "_require_user",
        lambda request: request.headers.get("x-test-owner", "alice"),
    )
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        yield client, store


def test_connect_source_disconnect_and_secrets_never_returned(setup):
    client, store = setup
    root = "/api/connections/email-inbox"
    assert client.get(root).json()["source"] == "gmail"
    assert (
        client.post(root + "/source", json={"source": "none"}).json()["source"]
        == "none"
    )
    response = client.post(
        root + "/connect", json={"inbox_id": "inb_school", "api_key": KEY}
    )
    assert response.status_code == 200
    assert KEY not in response.text and "sealed" not in response.text
    assert response.json()["active"]
    assert not client.get(root, headers={"x-test-owner": "bob"}).json()["connected"]
    assert client.post(root + "/disconnect", json={}).json()["source"] == "none"
    assert not store.view("alice")["connected"]
    assert client.post(root + "/source", json={"source": "gmail"}).json()[
        "personal_gmail_enabled"
    ]


def test_cross_origin_oversized_invalid_payloads_do_not_mutate(setup):
    client, store = setup
    root = "/api/connections/email-inbox"
    for path in ["/connect", "/source", "/disconnect"]:
        assert (
            client.post(
                root + path, json={}, headers={"origin": "https://other.example"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                root + path, json={}, headers={"sec-fetch-site": "cross-site"}
            ).status_code
            == 403
        )
    assert client.post(root + "/connect", content="x" * 4097).status_code == 413
    assert (
        client.post(
            root + "/connect", json={"api_key": KEY, "owner": "bob"}
        ).status_code
        == 400
    )
    assert (
        client.post(root + "/source", json={"source": "agentmail"}).status_code == 409
    )
    assert client.post(root + "/source", content="bad json").status_code == 400
    assert store.view("alice")["source"] == "gmail"


def test_every_endpoint_requires_auth_before_secret_or_store_access(setup, monkeypatch):
    client, _ = setup

    def denied(request):
        raise HTTPException(401, "Sign in")

    monkeypatch.setattr(connections_api, "_require_user", denied)
    monkeypatch.setattr(
        agent_inbox, "default_store", lambda: pytest.fail("Store accessed before auth")
    )
    root = "/api/connections/email-inbox"
    assert client.get(root).status_code == 401
    for path in ["/source", "/connect", "/disconnect"]:
        assert client.post(root + path, content="x" * 5000).status_code == 401


def test_zoho_connect_is_owner_bound_and_never_returns_password(setup):
    from test_zoho_inbox import Fake, EMAIL, PASSWORD

    client, store = setup
    fake = Fake()
    store.zoho_factory = fake.factory
    root = "/api/connections/email-inbox"
    response = client.post(
        root + "/connect",
        json={
            "provider": "zoho",
            "email": EMAIL,
            "password": PASSWORD,
            "host": "imappro.zoho.com",
        },
    )
    assert response.status_code == 200 and response.json()["active"]
    assert response.json()["source"] == response.json()["provider"] == "zoho"
    assert PASSWORD not in response.text and "sealed" not in response.text
    assert not client.get(root, headers={"x-test-owner": "bob"}).json()["connected"]
    assert (
        client.post(
            root + "/connect",
            json={
                "provider": "zoho",
                "email": EMAIL,
                "password": "",
                "host": "imappro.zoho.com",
            },
        ).status_code
        == 200
    )
    assert client.post(root + "/disconnect", json={}).json()["source"] == "none"
    assert not store.view("alice")["connected"]


def test_provider_fields_cannot_be_mixed_and_failed_zoho_connect_does_not_fallback(
    setup,
):
    from test_zoho_inbox import Fake, EMAIL, PASSWORD

    client, store = setup
    fake = Fake()
    store.zoho_factory = fake.factory
    root = "/api/connections/email-inbox"
    store.select("alice", "none")
    for body in [
        {"provider": "zoho", "email": EMAIL, "password": PASSWORD, "api_key": "other"},
        {"provider": "agentmail", "inbox_id": "inb_school", "password": PASSWORD},
        {"provider": "smtp", "email": EMAIL, "password": PASSWORD},
    ]:
        assert client.post(root + "/connect", json=body).status_code == 400
    assert not fake.calls
    fake.failure = True
    response = client.post(
        root + "/connect",
        json={"provider": "zoho", "email": EMAIL, "password": PASSWORD},
    )
    assert response.status_code == 409 and PASSWORD not in response.text
    assert store.view("alice")["source"] == "none"
