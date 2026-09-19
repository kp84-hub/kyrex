"""connections_api.py — owner-authenticated Chat API routes for Connections.

The Google Calendar (read-only) connector slice: the HTTP surface ONLY. All
durable behaviour lives in ``kyrex-cloud/connectors.py`` (encrypted,
owner-scoped OAuth token storage + fail-closed boundaries); this module adds
no storage and no secrets of its own.

Endpoints:

  GET  /api/connections
        owner-scoped connection view (status connected|disconnected|expired,
        granted scopes, timestamps, capability declarations). NEVER contains a
        token, client secret, or authorization code.

  POST /api/connections/google/connect
        start an OAuth round-trip. Returns the provider ``authorization_url``
        (which carries the single-use, owner-bound, redirect-bound, TTL-bound
        ``state``) and nothing else. Unconfigured host -> 503.

  GET  /api/connections/google/callback
        the provider redirects the owner's browser here after consent. The
        ``state`` is validated (unknown / foreign / reused / expired / redirect
        mismatch fail closed), the code is exchanged SERVER-SIDE, and the
        tokens are sealed into the durable store. The browser gets a tiny
        static HTML page; the code never reaches, and is never echoed to, any
        UI surface.

  POST /api/connections/google/disconnect
        drop the owner's stored tokens. Idempotent.

Expired handling: GET /api/connections derives ``expired``/``usable`` from the
stored ``expires_at`` -- a connected-but-expired connector shows as EXPIRED
with a reconnect hint, never as silently broken.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

# web/backend/connections_api.py sits inside kyrex-cloud/web/backend -- resolve
# the Cloud package the same way chat_api.py / dev_bot.py do.
_SCRIPT_DIR = Path(__file__).resolve().parent        # web/backend/
_CLOUD_DIR = _SCRIPT_DIR.parent.parent              # kyrex-cloud/
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

try:  # pragma: no cover -- exercised via _connectors()
    import connectors as connectors_core  # noqa: E402
except Exception as _exc:  # pragma: no cover
    connectors_core = None
    _IMPORT_ERROR = _exc
else:
    _IMPORT_ERROR = None

router = APIRouter()

_DEFAULT_STORE = None
# Test hook: an injected ``callable(code, redirect_uri, client) -> dict``
# token exchange, so the callback endpoint is testable end-to-end without
# touching Google. Production leaves this None.
exchange_override = None


def _connectors():
    """The connector core module, or a fail-closed 503 when unavailable."""
    if connectors_core is None:
        raise HTTPException(
            status_code=503,
            detail="Connections are unavailable on this host "
                   f"({_IMPORT_ERROR})",
        )
    return connectors_core


def _store():
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = _connectors().default_store()
    return _DEFAULT_STORE


def _require_user(request: Request) -> str:
    """Reuse the Cloud's own auth resolution (session cookie OR bearer)."""
    import main
    return main.require_user(request)


# ── Status derivation (connected | disconnected | expired) ────────────

def _expiry_fields(view: dict, *, now=None) -> dict:
    now = time.time() if now is None else float(now)
    expires_at = view.get("expires_at")
    expired = bool(
        view.get("connected")
        and expires_at is not None
        and float(expires_at) <= now
    )
    view["expired"] = expired
    view["usable"] = bool(view.get("connected") and not expired)
    return view


def _capabilities_summary() -> dict:
    """Static capability declarations for the UI (read-only wording)."""
    core = _connectors()
    bots = {}
    for role, decl in core.CAPABILITY_DECLARATIONS.items():
        bots[role] = {
            "capabilities": list(decl["capabilities"]),
            "read_only": decl["read_only"],
            "unsupported": list(decl["unsupported"]),
        }
    return {"read_only": True, "bots": bots}


def _configured() -> bool:
    """Whether a Google OAuth client is configured (no secret surfaces)."""
    try:
        _store().client_config()
    except Exception:
        return False
    return True


def _configured_redirect_uri() -> str:
    """The exact callback redirect configured on this host (no secret).

    The callback asserts a state's bound redirect equals this value, so a
    state minted for a different callback origin can never complete here.
    """
    return str(_store().client_config()["redirect_uri"])


def _connection_view(owner: str, provider: str = "google") -> dict:
    core = _connectors()
    view = _expiry_fields(_store().status(owner, provider))
    view["capabilities"] = _capabilities_summary()
    view["configured"] = _configured()
    view["read_only"] = True
    return view


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("/api/connections")
def list_connections(request: Request):
    owner = _require_user(request)
    return {"connectors": [_connection_view(owner)], "read_only": True}


@router.post("/api/connections/google/connect")
async def connect_google(request: Request):
    owner = _require_user(request)
    core = _connectors()
    try:
        started = _store().begin_oauth(owner)
    except core.ConnectorConfigError as exc:
        # Host-side misconfiguration -- the user cannot fix it here.
        raise HTTPException(status_code=503, detail=str(exc))
    except core.ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # The response deliberately contains ONLY the authorization URL (whose
    # ``state`` must reach the provider's redirect) plus metadata. No client
    # secret, no stored token, no authorization code.
    return {
        "provider": "google",
        "authorization_url": started["authorization_url"],
        "expires_at": started["expires_at"],
        "scopes": started["scopes"],
    }


def _redirect_page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>" + title + "</title>"
        "<style>body{font-family:system-ui,sans-serif;display:grid;"
        "place-items:center;height:100vh;margin:0;background:#111;color:#eee}"
        "main{text-align:center}</style></head><body><main>"
        "<h1>" + title + "</h1><p>" + body + "</p>"
        "<p>You can close this window and return to Kyrex Chat.</p>"
        "</main></body></html>"
    )


@router.get("/api/connections/google/callback")
def google_callback(request: Request):
    """Complete the OAuth round-trip server-side; render a static page.

    The provider's redirect arrives in the owner's BROWSER, so it can carry the
    single-use ``state`` but NOT a bearer token or the SPA's session
    credential. The state -- minted by the authenticated ``/connect`` call and
    bound to the owner and the exact configured redirect -- is therefore the
    ONLY credential this endpoint trusts: it is validated and atomically
    consumed server-side BEFORE any authorization code is exchanged. On success
    the response is a tiny page confirming the connection; on ANY failure it is
    a generic error page. Neither page ever echoes the ``code``, ``state``, or
    owner.
    """
    # No _require_user(): the browser callback authenticates solely by
    # validating and consuming the state (owner=None means "derive the owner
    # from the state"), so it works even without a session/bearer credential.
    core = _connectors()
    params = request.query_params
    error = str(params.get("error") or "").strip()
    state = str(params.get("state") or "").strip()
    code = str(params.get("code") or "").strip()

    if error:
        return HTMLResponse(_redirect_page(
            "Connection not completed",
            "The provider reported the authorization was not completed. "
            "Return to Kyrex Chat and try Connect again.",
        ), status_code=200)

    try:
        # owner=None: the state authenticates the owner (see consume_state).
        # The configured redirect is asserted exactly, so a state bound to a
        # different callback origin can never complete here.
        _store().complete_oauth(
            None, state, code,
            redirect_uri=_configured_redirect_uri(),
            exchange=exchange_override if exchange_override else None,
        )
    except core.OAuthStateError:
        return HTMLResponse(_redirect_page(
            "Connection not completed",
            "This authorization link is no longer valid -- it may have "
            "expired or already been used. Return to Kyrex Chat and start "
            "the connection again.",
        ), status_code=200)
    except core.ConnectorConfigError:
        return HTMLResponse(_redirect_page(
            "Connection not completed",
            "Google OAuth is not configured on this host.",
        ), status_code=200)
    except (core.ConnectorError, core.ConnectorUnavailable):
        # Includes a failed token exchange; the underlying raise path never
        # embeds the code, so a generic page keeps it that way.
        return HTMLResponse(_redirect_page(
            "Connection not completed",
            "The authorization could not be completed. Return to Kyrex "
            "Chat and try Connect again.",
        ), status_code=200)

    return HTMLResponse(_redirect_page(
        "Google connected",
        "Kyrex can now read your Google Calendar (read-only).",
    ), status_code=200)


@router.post("/api/connections/google/disconnect")
async def disconnect_google(request: Request):
    owner = _require_user(request)
    core = _connectors()  # fail closed (503) when the connector module is missing
    try:
        disconnected = _store().disconnect(owner)
    except core.ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    view = _expiry_fields(_store().status(owner))
    view["capabilities"] = _capabilities_summary()
    view["configured"] = _configured()
    view["read_only"] = True
    return {"disconnected": bool(disconnected), "connection": view}
