"""Real encrypted store, FastAPI routes and engine/host exchange; providers mocked."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
for directory in (ROOT / "kyrex-cloud", ROOT / "kyrex-cloud/web/backend"):
    sys.path.insert(0, str(directory))
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "alice")
os.environ.setdefault("WEB_SESSION_SECRET", "remote-mcp-offline-only")

import remote_mcp_connections as connections
import remote_mcp_api as api
from kyrex.tools.remote_mcp import RemoteMCPError

READ = {"name": "read_notes", "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False}}
WRITE = {**READ, "name": "send_note", "annotations": {"readOnlyHint": False, "destructiveHint": False}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "remote-mcp-offline-only")
    class Provider:
        calls = []
        tools_value = [READ, WRITE]
        before_call = None
        def __init__(self, url, token, auth_header):
            self.url, self.token = url, token
        def tools(self):
            self.calls.append(("list", self.token))
            return self.tools_value
        def call_read(self, name, arguments, expected, authorize):
            if type(self).before_call: type(self).before_call()
            authorize()
            self.calls.append((name, self.token))
            return {"content": [{"type": "text", "text": "My note " + self.token + " " + self.url}]}
    value = connections.RemoteMCPConnections(tmp_path / "mcp.sqlite3", Provider)
    value.provider = Provider
    return value


def connect(store, owner="alice"):
    row = store.connect(owner, label="Notes", url="https://notes.example/mcp-session", token="offline-personal-credential-" + owner)
    return row["id"]


def test_encrypted_owner_isolation_and_no_secret_in_views_or_results(store):
    a, b = connect(store), connect(store, "bob")
    assert [a] == [row["id"] for row in store.list("alice")]
    for fn in (lambda: store.test("bob", a), lambda: store.select("bob", a, ["read_notes"]), lambda: store.delete("bob", a)):
        with pytest.raises(connections.MCPConnectionError): fn()
    store.select("alice", a, ["read_notes"])
    result = store.call("alice", a, "read_notes", {})
    serialized = json.dumps([store.list("alice"), store.discover("alice"), result])
    assert "offline-personal-credential" not in serialized
    assert "https://notes.example/mcp-session" not in serialized
    assert b not in serialized
    raw = store.path.read_bytes()
    assert b"offline-personal-credential" not in raw and b"notes.example" not in raw
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_connect_tests_only_then_selection_limits_execution(store):
    a = connect(store)
    assert store.provider.calls == [("list", "offline-personal-credential-alice")]
    assert store.discover("alice")["tools"] == []
    for name in ("read_notes", "send_note", "invented"):
        with pytest.raises(connections.MCPConnectionError): store.call("alice", a, name, {})
    for names in (["send_note"], ["invented"]):
        with pytest.raises(connections.MCPConnectionError): store.select("alice", a, names)
    store.select("alice", a, ["read_notes"])
    assert store.discover("alice")["tools"][0]["inputSchema"] == READ["inputSchema"]


def test_retest_removes_changed_grants_and_delete_stops_stale_calls(store):
    a = connect(store)
    store.select("alice", a, ["read_notes"])
    store.provider.tools_value = [{**READ, "description": "Changed"}]
    row = store.test("alice", a)
    assert row["status"] == "tested" and not row["tools"][0]["selected"]
    store.delete("alice", a)
    with pytest.raises(connections.MCPConnectionError): store.call("alice", a, "read_notes", {})


def test_disconnect_during_discovery_is_rechecked_before_call(store):
    a = connect(store)
    store.select("alice", a, ["read_notes"])
    store.provider.before_call = lambda: store.delete("alice", a)
    with pytest.raises(connections.MCPConnectionError): store.call("alice", a, "read_notes", {})
    assert all(name == "list" for name, _ in store.provider.calls)


def test_missing_encryption_makes_zero_provider_calls(store, monkeypatch):
    monkeypatch.delenv("WEB_SESSION_SECRET")
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    with pytest.raises(Exception): connect(store)
    assert store.provider.calls == []



@pytest.fixture
def client(store, monkeypatch):
    def require_user(request):
        user = request.headers.get("authorization", "").removeprefix("Bearer ")
        if user not in ("alice", "bob"): raise HTTPException(401, "Sign in")
        return user
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(require_user=require_user))
    monkeypatch.setattr(api, "store", lambda: store)
    app = FastAPI(); app.include_router(api.router)
    return TestClient(app)


def test_settings_http_requires_auth_and_rejects_owner_spoofing(client, store):
    body = {"label": "Notes", "url": "https://notes.example/mcp", "token": "offline-personal-key"}
    for method, path, payload in (("GET", "", None), ("POST", "", body), ("POST", "/guessed/test", None),
                                  ("PUT", "/guessed/tools", {"tools": []}), ("DELETE", "/guessed", None)):
        assert client.request(method, "/api/connections/mcp" + path, json=payload).status_code == 401
    assert store.provider.calls == []
    assert client.post("/api/connections/mcp", headers={"authorization": "Bearer alice"}, json={**body, "owner": "bob"}).status_code == 422
    assert store.provider.calls == []


def test_invalid_secret_fields_are_not_echoed_by_fastapi(client):
    secret = "private-test-value-" * 300
    response = client.post("/api/connections/mcp", headers={"authorization": "Bearer alice"}, json={
        "label": "Notes", "url": "https://notes.example/mcp", "token": secret})
    assert response.status_code == 422
    assert secret not in response.text and "private-test-value" not in response.text


def test_http_connect_select_retest_remove_and_cross_owner(client, store):
    auth = {"authorization": "Bearer alice"}
    response = client.post("/api/connections/mcp", headers=auth, json={"label": "Notes", "url": "https://notes.example/mcp", "token": "offline-personal-key"})
    assert response.status_code == 200, response.text
    cid = response.json()["id"]
    assert "offline-personal-key" not in response.text
    assert client.get("/api/connections/mcp", headers={"authorization": "Bearer bob"}).json() == {"connections": []}
    assert client.put(f"/api/connections/mcp/{cid}/tools", headers={"authorization": "Bearer bob"}, json={"tools": ["read_notes"]}).status_code == 400
    assert client.put(f"/api/connections/mcp/{cid}/tools", headers=auth, json={"tools": ["send_note"]}).status_code == 400
    assert client.put(f"/api/connections/mcp/{cid}/tools", headers=auth, json={"tools": ["read_notes"]}).json()["status"] == "enabled"
    assert client.post(f"/api/connections/mcp/{cid}/test", headers=auth).status_code == 200
    assert client.delete(f"/api/connections/mcp/{cid}", headers=auth).status_code == 200
    assert client.get("/api/connections/mcp", headers=auth).json() == {"connections": []}


def test_actual_engine_host_exchange_and_live_policy_restrictions(store, monkeypatch):
    import chat_service
    import bots
    import kyrex.toolbox as toolbox
    a = connect(store)
    store.select("alice", a, ["read_notes"])
    monkeypatch.setattr(connections, "RemoteMCPConnections", lambda: store)
    live = {"owner": "alice", "status": "running", "policy": {}}
    monkeypatch.setattr(bots, "get_bot", lambda _: live)
    session = object.__new__(chat_service.EngineSession)
    session.mcp_owner = "alice"; session.bot_id = "notes-bot"; session.allowed_tools = {"mcp_read"}
    frames = []
    class Host:
        def write(self, text):
            frame = json.loads(text); frames.append(frame)
            approved, payload = session._handle_mcp_read(frame)
            toolbox._confirmation_results[frame["id"]] = approved
            toolbox._confirmation_payloads[frame["id"]] = payload
            toolbox._pending_confirmations[frame["id"]].set()
        def flush(self): pass
    monkeypatch.setenv("KYREX_SURFACE", "Kyrex Chat")
    monkeypatch.setattr(toolbox.sys, "stdout", Host())
    tool = object.__new__(toolbox.ToolBox)
    assert tool.mcp_read()["tools"][0]["name"] == "read_notes"
    result = tool.mcp_read("call", a, "read_notes", {})
    assert result["untrusted"] is True and "offline-personal-credential" not in json.dumps(result)
    for rules in ({"mcp:read": "deny"}, {"mcp:*": "deny"}, {"*": "deny"}, {"mcp:read": 1}):
        live["policy"] = rules
        assert "error" in tool.mcp_read("call", a, "read_notes", {})
    live.update(owner="bob", policy={})
    assert "error" in tool.mcp_read()
    live.update(owner="alice")
    session._fitness_read_only = True
    assert "error" in tool.mcp_read()
    assert all("token" not in frame and "owner" not in frame for frame in frames)


def test_full_engine_loop_reaches_real_remote_transport_via_host(tmp_path, monkeypatch):
    import asyncio
    import socket
    import httpx
    import chat_service
    import kyrex.core as core
    import kyrex.toolbox as toolbox
    from kyrex.tools.remote_mcp import RemoteMCPClient
    requests = []
    def wire(request):
        frame = json.loads(request.content); requests.append(frame)
        method = frame["method"]
        if method == "notifications/initialized": return httpx.Response(202)
        result = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {}},
                  "tools/list": {"tools": [READ]},
                  "tools/call": {"content": [{"type": "text", "text": "Meeting at 4 PM"}]}}[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": frame["id"], "result": result})
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(0, 1, 0, "", ("93.184.216.34", 443))])
    real_store = connections.RemoteMCPConnections(tmp_path / "wire.sqlite3", lambda url, token, header:
        RemoteMCPClient(url, token, header, transport=httpx.MockTransport(wire)))
    cid = connect(real_store); real_store.select("alice", cid, ["read_notes"])
    monkeypatch.setattr(connections, "RemoteMCPConnections", lambda: real_store)
    host = object.__new__(chat_service.EngineSession)
    host.mcp_owner = "alice"; host.bot_id = None; host.allowed_tools = {"mcp_read"}
    emitted = []
    class Channel:
        def write(self, text):
            try: frame = json.loads(text)
            except ValueError: return
            if frame.get("type") != "confirm_request": return
            emitted.append(frame)
            approved, result = host._handle_mcp_read(frame)
            toolbox._confirmation_results[frame["id"]] = approved
            toolbox._confirmation_payloads[frame["id"]] = result
            toolbox._pending_confirmations[frame["id"]].set()
        def flush(self): pass
    class Model:
        name = "offline"
        count = 0
        async def chat(self, **kwargs):
            step = [("mcp_read", {"action": "list"}),
                    ("mcp_read", {"action": "call", "connection_id": cid, "tool": "read_notes", "arguments": {}}),
                    ("task_complete", {"summary": "Read the note", "answer": "Meeting at 4 PM"})][self.count]
            self.count += 1
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "call-" + str(self.count), "type": "function",
                "function": {"name": step[0], "arguments": json.dumps(step[1])}}]}
    model = Model()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KYREX_SURFACE", "Kyrex Chat")
    monkeypatch.setenv("KYREX_ALLOWED_TOOLS", "mcp_read,task_complete")
    monkeypatch.setenv("KYREX_READ_ONLY_REPO", "1")
    monkeypatch.setattr(core, "get_provider", lambda *a, **kw: model)
    monkeypatch.setattr(core, "MCPManager", lambda: SimpleNamespace(get_tool_schemas=lambda: [],
        call_tool=lambda *a, **kw: pytest.fail("Host tool was routed into local MCP")))
    monkeypatch.setattr(core.PlaneExecute, "_load_initial_state", lambda _: None)
    monkeypatch.setattr(core, "JevShadowObserver", SimpleNamespace(from_env=lambda: SimpleNamespace(observe=lambda *a: None)))
    config = SimpleNamespace(get=lambda _: None, get_headers=lambda: {}, get_provider=lambda: "offline", get_api_key=lambda: "offline")
    engine = core.PlaneExecute(config=config)
    engine.skills = SimpleNamespace(match=lambda _: None)
    engine.session.save = lambda *a, **kw: None
    monkeypatch.setattr(toolbox.sys, "stdout", Channel())
    answer, _ = asyncio.run(engine.chat("Read my notes through MCP"))
    assert model.count == 3 and "Meeting at 4 PM" in answer
    assert [frame["action"] for frame in emitted] == ["list", "call"]
    assert len([frame for frame in requests if frame["method"] == "tools/call"]) == 1
    assert "offline-personal-credential" not in json.dumps(engine.session.history)


def test_session_grants_refresh_on_select_and_disconnect(store, monkeypatch, tmp_path):
    from collections import OrderedDict
    import chat_service
    monkeypatch.setattr(connections, "RemoteMCPConnections", lambda: store)
    monkeypatch.setattr(chat_service, "_engine_sessions", OrderedDict())
    class Session:
        def __init__(self, workspace, provider, config):
            self.workspace = workspace; self.bot_id = None; self._closed = False
            self.allowed_tools = chat_service._effective_caps(config)
            self._proc = SimpleNamespace(poll=lambda: None)
        def close(self): self._closed = True
    monkeypatch.setattr(chat_service, "EngineSession", Session)
    cfg = {"provider": "offline", "model": "offline"}
    cid = connect(store)
    first = chat_service._get_engine_session("alice", "conversation", tmp_path, provider_cfg=cfg)
    assert "mcp_read" not in first.allowed_tools
    store.select("alice", cid, ["read_notes"])
    second = chat_service._get_engine_session("alice", "conversation", tmp_path, provider_cfg=cfg)
    assert first._closed and "mcp_read" in second.allowed_tools
    assert second.mcp_owner == "alice"
    store.delete("alice", cid)
    third = chat_service._get_engine_session("alice", "conversation", tmp_path, provider_cfg=cfg)
    assert second._closed and "mcp_read" not in third.allowed_tools


def test_unreadable_bot_registry_denies_connected_access(monkeypatch):
    import bots
    def broken(_): raise bots.RegistryError("Registry cannot be read")
    monkeypatch.setattr(bots, "get_bot", broken)
    assert connections.bot_allowed("alice", "notes-bot") is False
