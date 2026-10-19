"""Offline wire tests: no remote server, model or account credentials."""
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from kyrex.tools import remote_mcp as mcp

READ = {"name": "search_notes", "description": "Search notes", "inputSchema": {
    "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False}}


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


class Wire:
    def __init__(self, tools=None, sse=False):
        self.tools = [READ] if tools is None else tools
        self.requests = []
        self.sse = sse
        self.error = None

    def respond(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        if self.error:
            return self.error(request, body)
        method = body["method"]
        if method == "notifications/initialized":
            assert "id" not in body
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "offline", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "Found the note"}]}
        else:
            raise AssertionError(method)
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.sse:
            return httpx.Response(200, headers={"content-type": "text/event-stream", "mcp-session-id": "offline-session"},
                text='event: message\r\ndata: ' + json.dumps(reply) + '\r\n\r\n')
        return httpx.Response(200, headers={"mcp-session-id": "offline-session"}, json=reply)

    def client(self, token="test-credential-value"):
        return mcp.RemoteMCPClient("https://notes.example/mcp", token, transport=httpx.MockTransport(self.respond))


@pytest.mark.parametrize("sse", [False, True])
def test_handshake_listing_read_and_pinned_tls_headers(public_dns, sse):
    wire = Wire(sse=sse)
    result = wire.client().call_read("search_notes", {"query": "schedule"}, mcp.fingerprint(READ))
    assert result["content"][0]["text"] == "Found the note"
    assert [body["method"] for _, body in wire.requests] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    for request, body in wire.requests:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "notes.example"
        assert request.extensions["sni_hostname"] == "notes.example"
        assert request.headers["authorization"] == "Bearer test-credential-value"
        assert "test-credential-value" not in str(request.url) + request.content.decode()
        if body["method"] != "initialize":
            assert request.headers["mcp-session-id"] == "offline-session"


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_private_and_metadata_addresses_never_dial(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(0, 1, 0, "", (address, 443))])
    wire = Wire()
    with pytest.raises(mcp.RemoteMCPError):
        wire.client().tools()
    assert wire.requests == []


@pytest.mark.parametrize("url", ["http://notes.example/mcp", "https://a:b@notes.example/mcp", "https://notes.example/mcp?key=secret", "https://notes.example:444/mcp", "file:///tmp/mcp"])
def test_invalid_endpoint_refused_before_resolving(monkeypatch, url):
    def forbidden(*a, **kw):
        raise AssertionError("DNS should not run")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    with pytest.raises(mcp.RemoteMCPError):
        mcp.public_endpoint(url)


def test_rebinding_is_checked_before_every_actual_request(monkeypatch):
    calls = iter(["93.184.216.34", "127.0.0.1"])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(0, 1, 0, "", (next(calls), 443))])
    wire = Wire()
    with pytest.raises(mcp.RemoteMCPError):
        wire.client().tools()
    assert len(wire.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
def test_redirects_and_errors_never_retry_or_echo_credentials(public_dns, status):
    wire = Wire()
    wire.error = lambda request, body: httpx.Response(status, headers={"location": "https://other.example"}, text="test-credential-value")
    with pytest.raises(mcp.RemoteMCPError) as caught:
        wire.client().tools()
    assert "test-credential-value" not in str(caught.value)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("tool", [
    {**READ, "annotations": {}}, {**READ, "annotations": {"readOnlyHint": True}},
    {**READ, "annotations": {"readOnlyHint": True, "destructiveHint": True}},
    {**READ, "description": "Changed meaning"},
])
def test_unknown_write_or_changed_tool_never_calls_provider(public_dns, tool):
    wire = Wire(tools=[tool])
    with pytest.raises(mcp.RemoteMCPError):
        wire.client().call_read("search_notes", {"query": "hello"}, mcp.fingerprint(READ))
    assert all(body["method"] != "tools/call" for _, body in wire.requests)


def test_schema_validation_and_remote_ref_never_fetch_or_call(public_dns):
    wire = Wire()
    with pytest.raises(mcp.RemoteMCPError):
        wire.client().call_read("search_notes", {"query": 42}, mcp.fingerprint(READ))
    bad = {**READ, "inputSchema": {"type": "object", "$ref": "http://169.254.169.254/metadata"}}
    wire.tools = [bad]
    with pytest.raises(mcp.RemoteMCPError):
        wire.client().call_read("search_notes", {}, mcp.fingerprint(bad))
    assert all(body["method"] != "tools/call" for _, body in wire.requests)


def test_revocation_callback_runs_after_listing_and_before_call(public_dns):
    wire = Wire()
    def revoked():
        raise mcp.RemoteMCPError("Disconnected")
    with pytest.raises(mcp.RemoteMCPError):
        wire.client().call_read("search_notes", {"query": "hello"}, mcp.fingerprint(READ), authorize=revoked)
    assert wire.requests[-1][1]["method"] == "tools/list"


def test_wrong_rpc_id_and_oversized_result_fail(public_dns):
    for response in (httpx.Response(200, json={"jsonrpc": "2.0", "id": -1, "result": {}}),
                     httpx.Response(200, text="x" * (mcp.MAX_BYTES + 1))):
        wire = Wire()
        wire.error = lambda request, body: response
        with pytest.raises(mcp.RemoteMCPError):
            wire.client().tools()


def test_pagination_collects_all_tools(public_dns):
    wire = Wire()
    original = wire.respond
    def reply(request):
        body = json.loads(request.content)
        if body["method"] != "tools/list": return original(request)
        wire.requests.append((request, body))
        result = {"tools": [{**READ, "name": "second"}]} if body["params"] else {"tools": [READ], "nextCursor": "next"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
    client = mcp.RemoteMCPClient("https://notes.example/mcp", transport=httpx.MockTransport(reply))
    assert [tool["name"] for tool in client.tools()] == ["search_notes", "second"]


def test_sse_stops_after_matching_reply_without_waiting_for_stream_end(public_dns):
    class OpenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}\n\n'
            raise AssertionError("Client waited after its reply")
    wire = Wire()
    wire.error = lambda request, body: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=OpenStream())
    client = wire.client()
    assert client._post("initialize", {})["protocolVersion"] == "2025-06-18"


def test_mcp_host_schema_is_chat_only_and_permission_masked(monkeypatch):
    from kyrex.core import PlaneExecute
    engine = object.__new__(PlaneExecute)
    engine.mcp = SimpleNamespace(get_tool_schemas=lambda: [])
    monkeypatch.setenv("KYREX_ALLOWED_TOOLS", "mcp_read,task_complete")
    monkeypatch.delenv("KYREX_SURFACE", raising=False)
    assert "mcp_read" not in {s["function"]["name"] for s in engine._get_all_tools_schema()}
    monkeypatch.setenv("KYREX_SURFACE", "Kyrex Chat")
    assert "mcp_read" in {s["function"]["name"] for s in engine._get_all_tools_schema()}
    monkeypatch.setenv("KYREX_ALLOWED_TOOLS", "task_complete")
    assert "mcp_read" not in {s["function"]["name"] for s in engine._get_all_tools_schema()}


@pytest.mark.parametrize("surface,starts", [("Kyrex Chat", 0), ("terminal", 1)])
def test_chat_does_not_launch_deployment_local_mcp_but_terminal_keeps_it(monkeypatch, surface, starts):
    from kyrex.core import PlaneExecute
    import threading
    engine = object.__new__(PlaneExecute)
    engine.session = SimpleNamespace(load=lambda _: True)
    engine._bootstrap_context = lambda **kw: None
    engine.skills = SimpleNamespace(discover=lambda: None)
    engine.mcp = SimpleNamespace(start_all=lambda: None)
    launches = []
    monkeypatch.setattr(threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: launches.append(kw)))
    monkeypatch.setenv("KYREX_SURFACE", surface)
    engine._load_initial_state()
    assert len(launches) == starts


def test_chat_cannot_install_or_start_a_local_mcp_process(monkeypatch):
    from kyrex.core import PlaneExecute
    monkeypatch.setenv("KYREX_SURFACE", "Kyrex Chat")
    engine = object.__new__(PlaneExecute)
    for command in ("/mcp add unsafe npx arbitrary-server", "/mcp test unsafe", "/mcp-browse"):
        text, _ = engine.handle_command(command)
        assert "Connections" in text
