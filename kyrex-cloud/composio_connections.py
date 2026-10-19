"""Managed app authentication bridged into the existing owner-scoped MCP store.

No agent SDK/provider, sandbox, proxy, write tools or ambient account selection.
The project key is deployment configuration; app consent belongs to each owner.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from urllib.parse import urlsplit

import connectors
from remote_mcp_connections import MCPConnectionError, RemoteMCPConnections

CATALOG = (
    {"slug": "notion", "name": "Notion", "description": "Read pages shared with your integration."},
    {"slug": "slack", "name": "Slack", "description": "Search and read the workspace you authorize."},
    {"slug": "googledrive", "name": "Google Drive", "description": "Search and read files your account can access."},
)
APPS = {app["slug"]: app for app in CATALOG}
API_ORIGIN = "https://backend.composio.dev"
AUTH_ORIGIN = "https://connect.composio.dev"
TTL = 15 * 60


def configured():
    return bool(os.environ.get("COMPOSIO_API_KEY", "").strip())


def user_id(owner):
    # Stable opaque application identity. Use a separate Composio project per deployment.
    return "kyrex_" + RemoteMCPConnections._owner(owner)


def sdk(key=None):
    key = key or os.environ.get("COMPOSIO_API_KEY", "").strip()
    if not key:
        raise MCPConnectionError("Apps via Composio is not configured on this server.")
    if any(os.environ.get(name) for name in ("COMPOSIO_USER_API_KEY", "COMPOSIO_ORG_API_KEY", "COMPOSIO_CUSTOM_HEADERS")):
        raise MCPConnectionError("Configure a dedicated Composio project key without user or organization key fallback.")
    from composio import Composio
    # An explicit key prevents SDK login/keyring fallback. No model provider is installed.
    return Composio(api_key=key, base_url=API_ORIGIN, allow_tracking=False, timeout=15, max_retries=0)


def _url(value, origin):
    parsed = urlsplit(str(value or ""))
    if (parsed.scheme + "://" + parsed.netloc != origin or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or any(ord(c) < 33 for c in str(value))):
        raise MCPConnectionError("Composio returned an unsupported connection destination.")
    return str(value)


def _dump(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def _account(client, record):
    account = _dump(client.connected_accounts.get(record["account_id"]))
    if (not isinstance(account, dict) or account.get("id") != record["account_id"]
            or account.get("user_id") != record["composio_user"]
            or (account.get("toolkit") or {}).get("slug") != record["toolkit"]):
        raise MCPConnectionError("The app account does not match this connection. Reconnect it.")
    if account.get("status") != "ACTIVE":
        raise MCPConnectionError("App sign-in is incomplete or expired. Finish connecting or reconnect.")
    return account


def validate_record(record):
    if record.get("setup_status") != "ready":
        raise MCPConnectionError("Finish app sign-in in Connections before enabling tools.")
    if record.get("composio_user") != "kyrex_" + record["owner"]:
        raise MCPConnectionError("App connection ownership could not be verified.")
    client = sdk(record["token"])
    _account(client, record)
    session = client.sessions.use(record["session_id"], mcp=True)
    config = _dump(session.config)
    if (not isinstance(config, dict) or config.get("user_id") != record["composio_user"]
            or config.get("connected_accounts") != {record["toolkit"]: [record["account_id"]]}
            or _url(session.mcp.url, API_ORIGIN) != record["url"]):
        raise MCPConnectionError("App session account changed. Reconnect it.")
    return client


def validated_client(record, client_factory):
    validate_record(record)
    return client_factory(record["url"], record["token"], "x-api-key")


def revoke_session(record):
    # Local grant is already deleted. Never delete a provider account that another
    # session might use. A provider outage must not retain local access.
    try:
        sdk(record.get("token") or None).sessions.use(record["session_id"]).delete()
    except Exception:
        pass


class ComposioConnections:
    def __init__(self, store=None, client_factory=sdk):
        self.store = store or RemoteMCPConnections()
        self.client_factory = client_factory

    def listing(self, owner):
        rows = [row for row in self.store.list(owner) if row.get("source") == "composio"]
        return {"configured": configured(), "apps": list(CATALOG), "connections": rows}

    def start(self, owner, toolkit):
        if toolkit not in APPS:
            raise MCPConnectionError("Choose an app from the Composio catalog.")
        connectors._box()  # fail before any broker request
        if len(self.store.list(owner)) >= 10:
            raise MCPConnectionError("Remove a connection before adding another (limit 10).")
        client = self.client_factory()
        # Empty tool allowlist during authentication; no execution/meta tools.
        session = client.sessions.create(user_id=user_id(owner), toolkits=[toolkit],
            tools={toolkit: {"enable": []}}, session_preset="direct_tools",
            sandbox={"enable": False}, multi_account={"enable": False}, proxy_execute=False, manage_connections=False,
            instant=False, mcp=True)
        request = session.authorize(toolkit)
        redirect = _url(request.redirect_url, AUTH_ORIGIN)
        record = {"owner": self.store._owner(owner), "id": uuid.uuid4().hex,
            "label": APPS[toolkit]["name"] + " via Composio", "url": "", "token": "",
            "auth_header": "x-api-key", "tools": [], "selected": {}, "tested_at": time.time(),
            "revision": uuid.uuid4().hex, "source": "composio", "toolkit": toolkit,
            "setup_status": "awaiting_sign_in", "expires_at": time.time() + TTL,
            "composio_user": user_id(owner), "account_id": request.id,
            "session_id": session.session_id}
        self.store._put(owner, record)
        return {"id": record["id"], "redirect_url": redirect}

    def complete(self, owner, connection_id):
        record = self.store._get(owner, connection_id)
        if record.get("source") != "composio":
            raise MCPConnectionError("This is not a Composio app connection.")
        if record.get("setup_status") == "ready":
            return self.store._view(record)
        if record.get("expires_at", 0) < time.time():
            raise MCPConnectionError("App setup expired. Remove this connection and connect again.")
        revision = record["revision"]
        client = self.client_factory()
        _account(client, record)  # exact stored attempt ID, not callback parameters
        toolkit = record["toolkit"]
        session = client.sessions.create(user_id=record["composio_user"],
            toolkits=[toolkit], connected_accounts={toolkit: [record["account_id"]]},
            tags={"enable": ["readOnlyHint"], "disable": ["createHint", "updateHint", "destructiveHint"]},
            session_preset="direct_tools", sandbox={"enable": False},
            proxy_execute=False, multi_account={"enable": False}, manage_connections=False, instant=False, mcp=True)
        headers = session.mcp.headers or {}
        if set(headers) != {"x-api-key"} or not isinstance(headers["x-api-key"], str) or not headers["x-api-key"]:
            raise MCPConnectionError("Composio must use a project API key for this connection.")
        url = _url(session.mcp.url, API_ORIGIN)
        candidate = {**record, "url": url, "token": headers["x-api-key"],
            "session_id": session.session_id, "setup_status": "ready",
            "revision": uuid.uuid4().hex, "tested_at": time.time(), "selected": {}}
        try:
            tools = self.store.client_factory(url, candidate["token"], "x-api-key").tools()
            # Ignore all meta tools and tools from any other app, regardless of annotations.
            candidate["tools"] = [tool for tool in tools if tool["name"].startswith(toolkit.upper() + "_")]
            self.store._put(owner, candidate, revision)
        except Exception:
            # A disconnect/change while checking can never recreate a local grant.
            try: session.delete()
            except Exception: pass
            raise
        try: client.sessions.use(record["session_id"]).delete()
        except Exception: pass
        return self.store._view(candidate)
