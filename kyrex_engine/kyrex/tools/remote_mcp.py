"""Bounded Streamable HTTP MCP reads, shared by Kyrex's host adapters.

Local subprocess MCP remains in mcp.py. This transport never launches commands,
uses ambient credentials/proxies, follows redirects or retries tool calls.
Servers are explicitly trusted by the connecting owner; annotations describe a
server's claim, not a sandbox around its implementation.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit

import httpx

MAX_BYTES = 1_000_000
MAX_TOOLS = 200
VERSIONS = {"2025-06-18", "2025-11-25"}


class RemoteMCPError(ValueError):
    pass


def public_endpoint(url):
    """Validate and resolve once; the actual socket must use the returned IP."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) <= 32 for c in url):
        raise RemoteMCPError("Enter a public HTTPS MCP endpoint.")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or parsed.query
                or parsed.port not in (None, 443)):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii")
        addresses = sorted({entry[4][0] for entry in socket.getaddrinfo(
            host, 443, type=socket.SOCK_STREAM)})
        if not addresses:
            raise ValueError()
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global or ip.is_multicast or ip.is_reserved:
                raise ValueError()
    except (ValueError, OSError, UnicodeError):
        raise RemoteMCPError("Use a reachable public HTTPS server on port 443, without URL credentials or query parameters.") from None
    return host, addresses[0]


def fingerprint(tool):
    return hashlib.sha256(json.dumps(tool, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def is_read_tool(tool):
    annotations = tool.get("annotations") or {}
    return (isinstance(annotations, dict) and annotations.get("readOnlyHint") is True
            and annotations.get("destructiveHint") is False)


def validate_schema(schema):
    """No remote schema retrieval, including through an untrusted $ref."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise RemoteMCPError("This tool has an unsupported input schema.")
    stack = [schema]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("$ref", "$dynamicRef") and (not isinstance(item, str) or not item.startswith("#")):
                    raise RemoteMCPError("Remote schema references are not supported.")
                stack.append(item)
        elif isinstance(value, list):
            stack.extend(value)


def _sse_reply(raw, request_id):
    normalized = bytes(raw).replace(b"\r\n", b"\n")
    end = normalized.rfind(b"\n\n")
    if end < 0:
        return None
    for event in normalized[:end].decode("utf-8").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
        if data:
            item = json.loads(data)
            if isinstance(item, dict) and item.get("id") == request_id:
                return item
    return None


class RemoteMCPClient:
    def __init__(self, url, token="", auth_header="Authorization", *, transport=None):
        self.url = url
        self.token = token
        self.auth_header = auth_header
        self.transport = transport
        self.session_id = None
        self.version = "2025-06-18"
        self.counter = 0
        self.deadline = time.monotonic() + 60

    def _post(self, method, params, *, notification=False):
        if time.monotonic() >= self.deadline:
            raise RemoteMCPError("MCP operation timed out.")
        self.counter += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = self.counter
        host, address = public_endpoint(self.url)
        headers = {"Host": host, "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", "User-Agent": "kyrex-mcp/1.0",
                   "MCP-Protocol-Version": self.version}
        if self.token:
            if self.auth_header not in ("Authorization", "x-api-key") or any(ord(c) < 32 for c in self.token):
                raise RemoteMCPError("Invalid MCP credential settings.")
            headers[self.auth_header] = ("Bearer " if self.auth_header == "Authorization" else "") + self.token
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        # Pin the validated address while retaining the real Host and TLS SNI.
        target = httpx.URL(self.url).copy_with(host=address)
        try:
            with httpx.Client(timeout=15, trust_env=False, follow_redirects=False,
                              transport=self.transport) as client:
                with client.stream("POST", target, headers=headers, json=message,
                                   extensions={"sni_hostname": host}) as response:
                    if response.status_code in (401, 403):
                        raise RemoteMCPError("MCP authentication was rejected. Replace the key or check access.")
                    if not 200 <= response.status_code < 300:
                        raise RemoteMCPError(f"MCP server returned HTTP {response.status_code}. No redirect or automatic retry was performed.")
                    sid = response.headers.get("mcp-session-id")
                    if sid:
                        if len(sid) > 256 or any(not 33 <= ord(c) <= 126 for c in sid):
                            raise RemoteMCPError("MCP server returned an invalid session header.")
                        self.session_id = sid
                    raw = bytearray()
                    sse = "text/event-stream" in response.headers.get("content-type", "")
                    reply = None
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if time.monotonic() >= self.deadline:
                            raise RemoteMCPError("MCP operation timed out.")
                        if len(raw) > MAX_BYTES:
                            raise RemoteMCPError("MCP response is too large.")
                        if sse and not notification:
                            reply = _sse_reply(raw, message["id"])
                            if reply is not None:
                                break
                    if notification:
                        return {}
                    if not sse:
                        reply = json.loads(raw)
        except RemoteMCPError:
            raise
        except (httpx.HTTPError, OSError, ValueError, UnicodeError, RecursionError):
            raise RemoteMCPError("MCP server could not be reached or returned an invalid response.") from None
        if not isinstance(reply, dict) or reply.get("id") != message["id"] or reply.get("jsonrpc") != "2.0":
            raise RemoteMCPError("MCP server returned an unmatched response.")
        if "error" in reply:
            raise RemoteMCPError("MCP server rejected this operation. Check the connection and tool arguments.")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise RemoteMCPError("MCP server returned an invalid result.")
        return result

    def initialize(self):
        result = self._post("initialize", {"protocolVersion": self.version, "capabilities": {},
                            "clientInfo": {"name": "kyrex", "version": "1.0"}})
        if result.get("protocolVersion") not in VERSIONS:
            raise RemoteMCPError("This server does not support Kyrex's MCP protocol versions yet.")
        self.version = result["protocolVersion"]
        self._post("notifications/initialized", {}, notification=True)

    def tools(self):
        self.initialize()
        tools, seen = [], set()
        cursor = None
        for _ in range(20):
            result = self._post("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list):
                raise RemoteMCPError("MCP server returned an invalid tool list.")
            for tool in page:
                if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                        or not 1 <= len(tool["name"]) <= 128 or tool["name"] in seen):
                    raise RemoteMCPError("MCP server returned invalid or duplicate tools.")
                seen.add(tool["name"])
                tools.append(tool)
            if len(tools) > MAX_TOOLS:
                raise RemoteMCPError("This server lists too many tools for this connection slice.")
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or len(cursor) > 2048:
                break
        raise RemoteMCPError("MCP tool discovery exceeded its page limit.")

    def call_read(self, name, arguments, expected_fingerprint, *, authorize=lambda: None):
        # Re-list before every call: a stale grant cannot authorize changed tools.
        tools = self.tools()
        tool = next((item for item in tools if item["name"] == name), None)
        if not tool or not is_read_tool(tool) or fingerprint(tool) != expected_fingerprint:
            raise RemoteMCPError("This tool changed or is no longer read-only. Test the connection and select its tools again.")
        schema = tool.get("inputSchema")
        validate_schema(schema)
        try:
            from jsonschema import Draft202012Validator
            from referencing import Registry
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema, registry=Registry()).validate(arguments)
        except Exception:
            raise RemoteMCPError("Tool arguments do not match the selected input schema.") from None
        authorize()
        result = self._post("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RemoteMCPError("The MCP tool reported a failure.")
        return result
