"""Controlled Composio broker, real Fernet/SQLite/API/MCP grants; no live accounts."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
for directory in (ROOT / "kyrex-cloud", ROOT / "kyrex-cloud/web/backend"):
    sys.path.insert(0, str(directory))

import composio_connections as apps
import composio_api as api
import remote_mcp_connections as remote
from kyrex.tools.remote_mcp import RemoteMCPError

KEY = "offline-composio-project-fixture"
READ = {"name": "NOTION_SEARCH", "description": "Read notes",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False}}
WRITE = {**READ, "name": "NOTION_CREATE_PAGE", "annotations": {"readOnlyHint": False, "destructiveHint": False}}
META = {**READ, "name": "COMPOSIO_REMOTE_WORKBENCH"}


class Config:
    def __init__(self, data): self.data = data
    def model_dump(self): return dict(self.data)


class Broker:
    def __init__(self):
        self.created = []; self.sessions_by_id = {}; self.accounts = {}; self.deleted = []
        self.sessions = NS(create=self.create, use=self.use)
        self.connected_accounts = NS(get=lambda account_id: self.accounts[account_id])
    def create(self, **kwargs):
        self.created.append(kwargs)
        sid = "session-" + str(len(self.created))
        session = NS(session_id=sid, config=Config(kwargs),
            mcp=NS(url=apps.API_ORIGIN + "/sessions/" + sid + "/mcp", headers={"x-api-key": KEY}),
            authorize=lambda toolkit: NS(id="account-" + sid, redirect_url=apps.AUTH_ORIGIN + "/link/" + sid),
            delete=lambda: self.deleted.append(sid))
        self.sessions_by_id[sid] = session
        if "connected_accounts" not in kwargs:
            self.accounts["account-" + sid] = {"id": "account-" + sid,
                "user_id": kwargs["user_id"], "toolkit": {"slug": kwargs["toolkits"][0]}, "status": "INITIATED"}
        return session
    def use(self, session_id, **kwargs): return self.sessions_by_id[session_id]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "offline-app-encryption-fixture")
    monkeypatch.setenv("COMPOSIO_API_KEY", KEY)
    broker = Broker()
    monkeypatch.setattr(apps, "sdk", lambda key=None: broker)
    class Provider:
        tools_value = [READ, WRITE, META, {**READ, "name": "SLACK_SEARCH"}]
        calls = []
        before_list = None
        before_call = None
        def __init__(self, url, token, auth_header):
            assert url.startswith(apps.API_ORIGIN) and token == KEY and auth_header == "x-api-key"
        def tools(self):
            if type(self).before_list: type(self).before_list()
            return self.tools_value
        def call_read(self, name, arguments, expected, authorize):
            if type(self).before_call: type(self).before_call()
            authorize()
            self.calls.append(name)
            return {"content": [{"type": "text", "text": "Document"}]}
    store = remote.RemoteMCPConnections(tmp_path / "mcp.sqlite3", Provider)
    service = apps.ComposioConnections(store, lambda: broker)
    return NS(store=store, service=service, broker=broker, provider=Provider)


def ready(env, owner="alice"):
    attempt = env.service.start(owner, "notion")
    record = env.store._get(owner, attempt["id"])
    env.broker.accounts[record["account_id"]]["status"] = "ACTIVE"
    row = env.service.complete(owner, attempt["id"])
    return row["id"]


def test_auth_session_has_no_tools_and_identity_is_owner_derived(setup):
    a = setup.service.start("alice", "notion")
    b = setup.service.start("bob", "notion")
    configs = setup.broker.created
    assert configs[0]["user_id"] == apps.user_id("alice") != configs[1]["user_id"]
    for config in configs:
        assert config["toolkits"] == ["notion"]
        assert config["tools"] == {"notion": {"enable": []}}
        assert config["sandbox"] == {"enable": False} and config["proxy_execute"] is False
        assert config["manage_connections"] is False and config["instant"] is False
        assert config["multi_account"] == {"enable": False}
    assert setup.store.discover("alice")["tools"] == []
    assert a["id"] != b["id"]


def test_complete_pins_exact_account_and_requires_explicit_tool_selection(setup):
    cid = ready(setup)
    config = setup.broker.created[-1]
    record = setup.store._get("alice", cid)
    assert config["connected_accounts"] == {"notion": [record["account_id"]]}
    assert config["tags"]["enable"] == ["readOnlyHint"]
    assert config["tags"]["disable"] == ["createHint", "updateHint", "destructiveHint"]
    assert config["session_preset"] == "direct_tools"
    assert setup.store.discover("alice")["tools"] == []
    assert [t["name"] for t in setup.store.list("alice")[0]["tools"]] == ["NOTION_SEARCH", "NOTION_CREATE_PAGE"]
    for name in ("NOTION_CREATE_PAGE", "SLACK_SEARCH", "COMPOSIO_REMOTE_WORKBENCH"):
        with pytest.raises(remote.MCPConnectionError): setup.store.select("alice", cid, [name])
    setup.store.select("alice", cid, ["NOTION_SEARCH"])
    result = setup.store.call("alice", cid, "NOTION_SEARCH", {"query": "test"})
    assert result["tool"] == "NOTION_SEARCH" and setup.provider.calls == ["NOTION_SEARCH"]


@pytest.mark.parametrize("field,value", [("user_id", "other-user"), ("id", "other-account"),
    ("toolkit", {"slug": "slack"}), ("status", "EXPIRED")])
def test_complete_rejects_wrong_owner_account_app_or_expired_auth(setup, field, value):
    attempt = setup.service.start("alice", "notion")
    record = setup.store._get("alice", attempt["id"])
    setup.broker.accounts[record["account_id"]].update(status="ACTIVE")
    setup.broker.accounts[record["account_id"]][field] = value
    with pytest.raises(remote.MCPConnectionError): setup.service.complete("alice", attempt["id"])
    assert len(setup.broker.created) == 1 and setup.provider.calls == []


def test_owner_isolation_expiry_and_no_secrets_in_safe_views(setup):
    cid = ready(setup)
    with pytest.raises(remote.MCPConnectionError): setup.service.complete("bob", cid)
    with pytest.raises(remote.MCPConnectionError): setup.store.select("bob", cid, ["NOTION_SEARCH"])
    assert setup.service.listing("bob")["connections"] == []
    output = json.dumps([setup.service.listing("alice"), setup.store.discover("alice")])
    raw = setup.store.path.read_bytes()
    record = setup.store._get("alice", cid)
    for value in (KEY, record["url"], record["session_id"], record["account_id"], record["composio_user"]):
        assert value not in output and value.encode() not in raw
    pending = setup.service.start("alice", "notion")["id"]
    item = setup.store._get("alice", pending); item["expires_at"] = 0
    setup.store._put("alice", item)
    with pytest.raises(remote.MCPConnectionError, match="expired"): setup.service.complete("alice", pending)


def test_revocation_and_changed_account_block_existing_grant(setup):
    cid = ready(setup)
    setup.store.select("alice", cid, ["NOTION_SEARCH"])
    record = setup.store._get("alice", cid)
    setup.broker.accounts[record["account_id"]]["status"] = "EXPIRED"
    with pytest.raises(remote.MCPConnectionError): setup.store.call("alice", cid, "NOTION_SEARCH", {})
    setup.broker.accounts[record["account_id"]]["status"] = "ACTIVE"
    setup.broker.sessions_by_id[record["session_id"]].config.data["connected_accounts"] = {"notion": ["another"]}
    with pytest.raises(remote.MCPConnectionError): setup.store.call("alice", cid, "NOTION_SEARCH", {})
    assert setup.provider.calls == []


def test_account_revocation_is_rechecked_immediately_before_tool_call(setup):
    cid = ready(setup); setup.store.select("alice", cid, ["NOTION_SEARCH"])
    record = setup.store._get("alice", cid)
    setup.provider.before_call = lambda: setup.broker.accounts[record["account_id"]].update(status="EXPIRED")
    with pytest.raises(remote.MCPConnectionError): setup.store.call("alice", cid, "NOTION_SEARCH", {})
    assert setup.provider.calls == []


def test_retest_filters_other_apps_and_meta_tools(setup):
    cid = ready(setup); setup.store.test("alice", cid)
    assert [t["name"] for t in setup.store.list("alice")[0]["tools"]] == ["NOTION_SEARCH", "NOTION_CREATE_PAGE"]


def test_disconnect_during_completion_cannot_recreate_local_grant(setup):
    cid = setup.service.start("alice", "notion")["id"]
    record = setup.store._get("alice", cid)
    setup.broker.accounts[record["account_id"]]["status"] = "ACTIVE"
    setup.provider.before_list = lambda: setup.store.delete("alice", cid)
    with pytest.raises(remote.MCPConnectionError): setup.service.complete("alice", cid)
    assert setup.store.list("alice") == []


def test_delete_revokes_local_tools_even_if_broker_is_down(setup, monkeypatch):
    cid = ready(setup); setup.store.select("alice", cid, ["NOTION_SEARCH"])
    def unavailable(*args, **kwargs): raise RuntimeError("offline")
    monkeypatch.setattr(apps, "sdk", unavailable)
    setup.store.delete("alice", cid)
    assert setup.store.discover("alice")["tools"] == []


@pytest.mark.parametrize("destination", ["https://evil.example/link/test", "http://connect.composio.dev/link/test",
    "https://connect.composio.dev.evil.example/link/test", "https://connect.composio.dev/link/test?key=bad"])
def test_untrusted_connect_destination_rejected(destination):
    with pytest.raises(remote.MCPConnectionError): apps._url(destination, apps.AUTH_ORIGIN)


def test_missing_encryption_and_unknown_apps_make_no_broker_calls(setup, monkeypatch):
    with pytest.raises(remote.MCPConnectionError): setup.service.start("alice", "unknown")
    monkeypatch.delenv("WEB_SESSION_SECRET")
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    with pytest.raises(connectors.ConnectorError): setup.service.start("alice", "notion")
    assert setup.broker.created == []


# Imported late to keep fixtures independent of main's startup.
import connectors


@pytest.fixture
def http(setup, monkeypatch):
    def require_user(request):
        user = request.headers.get("authorization", "").removeprefix("Bearer ")
        if user not in ("alice", "bob"): raise HTTPException(401, "Sign in")
        return user
    monkeypatch.setitem(sys.modules, "main", NS(require_user=require_user))
    monkeypatch.setattr(api, "service", lambda: setup.service)
    app = FastAPI(); app.include_router(api.router)
    return TestClient(app)


def test_http_authentication_and_spoofed_identity_are_rejected(http, setup):
    for method, path, body in (("GET", "", None), ("POST", "/apps/notion", {}), ("POST", "/guess/complete", {})):
        assert http.request(method, "/api/connections/composio" + path, json=body).status_code == 401
    response = http.post("/api/connections/composio/apps/notion",
        headers={"authorization": "Bearer alice"}, json={"owner": "bob", "token": KEY})
    assert response.status_code == 422 and KEY not in response.text and not setup.broker.created


def test_http_sign_in_complete_then_separate_read_tool_grant(http, setup):
    auth = {"authorization": "Bearer alice"}
    start = http.post("/api/connections/composio/apps/notion", headers=auth, json={})
    assert start.status_code == 200 and start.json()["redirect_url"].startswith(apps.AUTH_ORIGIN)
    cid = start.json()["id"]; record = setup.store._get("alice", cid)
    assert http.post("/api/connections/composio/" + cid + "/complete",
        headers={"authorization": "Bearer bob"}, json={}).status_code == 400
    assert http.post("/api/connections/composio/" + cid + "/complete", headers=auth, json={}).status_code == 400
    setup.broker.accounts[record["account_id"]]["status"] = "ACTIVE"
    done = http.post("/api/connections/composio/" + cid + "/complete", headers=auth, json={})
    assert done.status_code == 200 and done.json()["setup_status"] == "ready"
    assert setup.store.discover("alice")["tools"] == []
    assert KEY not in done.text and "session-" not in done.text


def test_pinned_sdk_contract_imports_without_external_requests(monkeypatch):
    # Real installed package/types, not a fake module. Construction is offline.
    import inspect
    from composio import Composio
    from composio.core.models.tool_router import ToolRouter
    from composio.core.models.tool_router_session import ToolRouterSession
    monkeypatch.delenv("COMPOSIO_USER_API_KEY", raising=False)
    monkeypatch.delenv("COMPOSIO_ORG_API_KEY", raising=False)
    client = Composio(api_key=KEY, allow_tracking=False, base_url=apps.API_ORIGIN, max_retries=0)
    assert client.sessions is not None
    assert {"user_id", "toolkits", "connected_accounts", "mcp", "session_preset", "instant"} <= set(inspect.signature(ToolRouter.create).parameters)
    assert {"toolkit", "callback_url"} <= set(inspect.signature(ToolRouterSession.authorize).parameters)
    assert hasattr(client.connected_accounts, "get")


def test_sdk_factory_refuses_missing_or_ambient_broad_credentials(monkeypatch):
    # Factory is deliberately not patched by the setup fixture in this test.
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    with pytest.raises(remote.MCPConnectionError): apps.sdk()
    monkeypatch.setenv("COMPOSIO_API_KEY", KEY)
    for name in ("COMPOSIO_USER_API_KEY", "COMPOSIO_ORG_API_KEY", "COMPOSIO_CUSTOM_HEADERS"):
        monkeypatch.setenv(name, "offline-ambient-value")
        with pytest.raises(remote.MCPConnectionError): apps.sdk()
        monkeypatch.delenv(name)


def test_real_sdk_serializes_scoped_sessions_and_reads_exact_account(tmp_path, monkeypatch):
    import httpx
    from composio import Composio
    monkeypatch.setenv("WEB_SESSION_SECRET", "offline-real-sdk-fixture")
    monkeypatch.setenv("COMPOSIO_API_KEY", KEY)
    requests = []
    sessions = {}
    uid = apps.user_id("alice")

    def transport(request):
        assert request.url.host == "backend.composio.dev"
        assert request.headers["x-api-key"] == KEY
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.method == "POST" and "user_id" in body:
            sid = "sdk-session-" + str(len(sessions) + 1)
            response = {"session_id": sid, "config_version": 1,
                "config": {**body, "preload": {"tools": "all"}, "instant": False,
                    "search": {"enable": False}, "execute": {"enable_multi_execute": False}},
                "mcp": {"type": "http", "url": apps.API_ORIGIN + "/sessions/" + sid + "/mcp"},
                "experimental": None}
            sessions[sid] = response
            return httpx.Response(200, json=response)
        if request.method == "POST" and "toolkit" in body:
            return httpx.Response(200, json={"connected_account_id": "sdk-account",
                "redirect_url": apps.AUTH_ORIGIN + "/link/sdk-attempt"})
        if "connected_accounts" in request.url.path:
            return httpx.Response(200, json={"id": "sdk-account", "user_id": uid,
                "toolkit": {"slug": "notion"}, "status": "ACTIVE"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        if request.method == "GET":
            sid = next((sid for sid in sessions if sid in request.url.path), None)
            if sid: return httpx.Response(200, json=sessions[sid])
        raise AssertionError("Unexpected offline SDK request: " + request.method + " " + request.url.path)

    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        client = Composio(api_key=KEY, base_url=apps.API_ORIGIN,
            allow_tracking=False, max_retries=0, http_client=http)
        monkeypatch.setattr(apps, "sdk", lambda key=None: client)
        class Provider:
            def __init__(self, *args): pass
            def tools(self): return [READ]
            def call_read(self, name, arguments, expected, authorize):
                authorize()
                return {"content": [{"type": "text", "text": "Real SDK bridge"}]}
        store = remote.RemoteMCPConnections(tmp_path / "sdk.sqlite3", Provider)
        service = apps.ComposioConnections(store, lambda: client)
        attempt = service.start("alice", "notion")
        row = service.complete("alice", attempt["id"])
        store.select("alice", row["id"], ["NOTION_SEARCH"])
        assert store.call("alice", row["id"], "NOTION_SEARCH", {})["tool"] == "NOTION_SEARCH"
        creates = [body for method, path, body in requests if method == "POST" and "user_id" in body]
        assert creates[0]["tools"]["notion"]["enable"] == []
        assert creates[1]["connected_accounts"] == {"notion": ["sdk-account"]}
        assert creates[1]["toolkits"] == {"enable": ["notion"]}
        assert creates[1]["tags"]["enable"] == ["readOnlyHint"]
        assert creates[1]["proxy_execute"] == {"enable": False}
