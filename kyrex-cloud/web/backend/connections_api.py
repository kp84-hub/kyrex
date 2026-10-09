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

  POST /api/connections/google/upgrade-gmail
        start an OAuth round-trip that ADDS the Gmail READ-ONLY scope
        (``gmail.readonly``), unioned with the owner's existing scopes so
        Calendar access is preserved. Never a send/modify/delete scope.

  GET  /api/connections/google/gmail/search
        bounded, read-only mail search (safe ``{owner,id,thread_id}`` stubs).

  GET  /api/connections/google/gmail/message/{message_id}
        ONE message's safe projection (Subject/From/Date + snippet; metadata
        only). No send/delete/archive/label route exists anywhere.

  POST /api/connections/google/disconnect
        drop the owner's stored tokens. Idempotent.

Expired handling: GET /api/connections refreshes expired Google access tokens
before deriving ``expired``/``usable``. Temporary refresh failures offer retry;
missing or revoked refresh authorization requires reconnecting.
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
    return {
        "read_only": all(d["read_only"]
                         for d in core.CAPABILITY_DECLARATIONS.values()),
        "bots": bots,
    }


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
    store = _store()
    view = _expiry_fields(store.status(owner, provider))
    if view["expired"]:
        try:
            # Normal access-token expiry is not lost consent. Use the same
            # owner-scoped, encrypted refresh path as the Calendar/Gmail tools.
            store.access_token(owner, provider)
            view = _expiry_fields(store.status(owner, provider))
        except core.ConnectorRefreshTemporaryUnavailable:
            view["expired"] = False
            view["usable"] = False
            view["temporarily_unavailable"] = True
        except core.ConnectorError:
            # Missing/unreadable refresh material or invalid_grant requires
            # consent. Never expose the provider's error or token response.
            view["expired"] = True
            view["usable"] = False
    caps = _capabilities_summary()
    view["capabilities"] = caps
    view["configured"] = _configured()
    view["read_only"] = caps["read_only"]
    view["has_write_scope"] = core.GOOGLE_CALENDAR_WRITE_SCOPE in (
        view.get("scopes") or [])
    # Gmail read is a SEPARATE scope over the shared Google grant: the Gmail
    # card is only "connected" once THIS scope is present, so a Calendar-only
    # token never reads mail. Non-secret, derived -- never a token.
    view["has_gmail_scope"] = core.GOOGLE_GMAIL_READ_SCOPE in (
        view.get("scopes") or [])
    return view


# ── Routes ──────────────────────────────────────────────────────────────

@router.get("/api/connections")
def list_connections(request: Request):
    owner = _require_user(request)
    import device_messages
    return {"connectors": [_connection_view(owner), device_messages.connection_view(owner), _github().view(owner), _fitness().view(owner), _fitness().view(owner, 'samsung_health')], "read_only": True}


@router.get("/api/connections/google/account")
def google_account(request: Request):
    """The connected Google ACCOUNT view: email + destination calendar.

    Read-only and non-secret: the email comes from Google's tokeninfo with
    the owner's live token, and the destination calendar id is the owner's
    stored non-secret preference. Fail closed (409/503) when Google is not
    connected or the provider is unresponsive — the caller shows a
    Reconnect Google action, never a stale cache.
    """
    owner = _require_user(request)
    core = _connectors()
    try:
        return _store().account_view(owner)
    except core.ConnectorUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except core.ConnectorError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/api/connections/google/calendars")
def google_calendars(request: Request):
    """The owner's destination-calendar choices (calendarList, read-only)."""
    owner = _require_user(request)
    core = _connectors()
    try:
        return {"calendars": _store().list_calendars(owner),
                "preferred": _store().preferred_calendar(owner)}
    except core.ConnectorUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except core.ConnectorError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.put("/api/connections/google/calendar")
async def set_google_calendar(request: Request):
    """Set the owner's destination calendar (Change calendar control).

    Body: ``{"calendar_id": "..."}``. The id is validated non-secret
    (no whitespace, no scheme/userinfo, bounded length) and persisted on the
    owner's connected record — readers (calendar: …, level6: calendar) and
    the confirmation-gated writer target exactly it.
    """
    owner = _require_user(request)
    body = await request.json()
    calendar_id = str(body.get("calendar_id") or "").strip()
    core = _connectors()
    try:
        stored = _store().set_calendar(owner, calendar_id)
    except core.ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"calendar_id": stored,
            "preferred": _store().preferred_calendar(owner)}


@router.post("/api/connections/google/connect")
async def connect_google(request: Request):
    owner = _require_user(request)
    core = _connectors()
    try:
        started = _store().begin_reconnect(owner)
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


@router.post("/api/connections/google/upgrade-write")
async def upgrade_google_calendar_write(request: Request):
    """Start the OAuth round-trip that ADDS the calendar event-write scope.

    Called ONLY when the owner explicitly enables the Calendar Writer. It
    requests the MINIMUM additional Google scope (``calendar.events``) unioned
    with the owner's currently granted scopes, so nothing already working is
    dropped and no arbitrary scope is requested. Read access stays read-only;
    the new write scope only permits creating events, each of which still
    requires the owner's explicit confirmation before the call.
    """
    owner = _require_user(request)
    core = _connectors()
    try:
        started = _store().begin_calendar_write_upgrade(
            owner, redirect_uri=_configured_redirect_uri())
    except core.ConnectorConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except core.ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "provider": "google",
        "authorization_url": started["authorization_url"],
        "expires_at": started["expires_at"],
        "scopes": started["scopes"],
        "write_scope": core.GOOGLE_CALENDAR_WRITE_SCOPE,
    }


@router.post("/api/connections/google/upgrade-gmail")
async def upgrade_google_gmail_read(request: Request):
    """Start the OAuth round-trip that ADDS the Gmail READ-ONLY scope.

    Called ONLY when the owner explicitly enables Gmail read. It requests the
    MINIMUM additional Google scope (``gmail.readonly``) unioned with the
    owner's currently granted scopes, so existing Calendar access is PRESERVED
    and no arbitrary scope is requested. Gmail stays STRICTLY read-only: this
    route never requests a send/modify/delete scope, and the connector core
    exposes no Gmail write surface at all.
    """
    owner = _require_user(request)
    core = _connectors()
    try:
        started = _store().begin_gmail_read_upgrade(
            owner, redirect_uri=_configured_redirect_uri())
    except core.ConnectorConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except core.ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "provider": "google",
        "authorization_url": started["authorization_url"],
        "expires_at": started["expires_at"],
        "scopes": started["scopes"],
        "read_scope": core.GOOGLE_GMAIL_READ_SCOPE,
    }


# ── Gmail READER surface (the smallest Chat-reader route) ──────────────
#
# Search mail and fetch ONE message's safe projection. Owner-authenticated,
# bounded, and fail closed -- and, like the rest of the Gmail slice, READ-ONLY:
# there is no send/delete/archive/label route here or anywhere.
#
# Deliberately NOT a Calendar surface: it never touches calendar scopes, so the
# existing separate Calendar behaviour is untouched.

@router.get("/api/connections/google/gmail/search")
def gmail_search(request: Request, q: str = "", max_results: int = 10,
                 page_token: str = ""):
    """Bounded Gmail search: ``{owner, id, thread_id}`` stubs only.

    Never a subject, snippet, body, or attachment. ``page_token`` continues
    the SAME query via the provider's opaque ``nextPageToken``; the response
    echoes the next page token (``""`` when exhausted). A missing token or
    scope fails closed (409); an unconfigured host fails closed (503).
    """
    owner = _require_user(request)
    core = _connectors()
    try:
        page = _store().gmail(owner).search(
            query=(q or None), max_results=max_results,
            page_token=(page_token or None))
    except core.ConnectorConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except core.ConnectorUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except core.ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"messages": page["messages"],
            "next_page_token": page.get("next_page_token") or "",
            "read_only": True}


@router.get("/api/connections/google/gmail/message/{message_id}")
def gmail_message(request: Request, message_id: str):
    """Fetch ONE message's safe projection (Subject/From/Date + snippet).

    Metadata only: bodies, attachments, and every other header never surface.
    A missing/malformed id fails closed (400) before any provider call.
    """
    owner = _require_user(request)
    core = _connectors()
    try:
        message = _store().gmail(owner).message(message_id)
    except core.ConnectorConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except core.ConnectorUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except core.ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"message": message, "read_only": True}


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


def _github():
    from github_connection import GitHubConnection
    return GitHubConnection()



def _github_flow():
    from github_app import GitHubAppFlow
    return GitHubAppFlow(connection=_github())


def _github_origin(request):
    # The callback follows the authenticated Chat origin, rather than the
    # separate desktop/login OAuth application's default Railway origin.
    import main
    from github_app import origin
    return origin(main.login_base_url(request))


def _github_page(message):
    import html
    return HTMLResponse("<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head>"
        "<body><h1>GitHub connection</h1><p>" + html.escape(message) + "</p>"
        "<a href='/'>Return to Kyrex</a></body></html>", headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'none'; frame-ancestors 'none'"})


@router.post("/api/connections/github/connect")
async def connect_github(request: Request):
    owner = _require_user(request)
    from github_connection import GitHubError
    from starlette.concurrency import run_in_threadpool
    try:
        return await run_in_threadpool(_github_flow().begin, owner, _github_origin(request))
    except (GitHubError, _connectors().ConnectorError):
        raise HTTPException(status_code=503, detail="GitHub sign-in is unavailable. Please try again.") from None


@router.post("/api/connections/github/manage")
async def manage_github(request: Request):
    owner = _require_user(request)
    from starlette.concurrency import run_in_threadpool
    try:
        return await run_in_threadpool(_github_flow().begin, owner, _github_origin(request), True)
    except Exception:
        raise HTTPException(status_code=503, detail="GitHub repository settings are unavailable.") from None


@router.get("/api/connections/github/setup")
def setup_github(request: Request, state: str = ""):
    owner = _require_user(request)
    import html
    import json
    import secrets
    try:
        manifest = _github_flow().manifest(owner, state, _github_origin(request))
        nonce = secrets.token_urlsafe(24)
        action = "https://github.com/settings/apps/new?state=" + state
        body = ("<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'></head><body>"
            "<h1>Connect GitHub</h1><p>For this first connection, confirm the Kyrex reader app on GitHub. "
            "Then choose the repositories you want to share.</p><form id='github' method='post' action='" + html.escape(action, quote=True) + "'>"
            "<input type='hidden' name='manifest' value='" + html.escape(json.dumps(manifest), quote=True) + "'>"
            "<button type='submit'>Continue on GitHub</button></form><script nonce='" + nonce + "'>"
            "document.getElementById('github').submit();</script></body></html>")
        return HTMLResponse(body, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; script-src 'nonce-" + nonce + "'; form-action https://github.com; frame-ancestors 'none'"})
    except Exception:
        return _github_page("Sign-in expired. Return to Kyrex and tap Connect again.")


@router.get("/api/connections/github/registered")
@router.get("/api/connections/github/installed")
@router.get("/api/connections/github/callback")
def finish_github(request: Request, state: str = "", code: str = "", installation_id: str = ""):
    owner = _require_user(request)
    from fastapi.responses import RedirectResponse
    from github_connection import GitHubError
    flow = _github_flow()
    base = _github_origin(request)
    try:
        if request.url.path.endswith('/registered'):
            url = flow.registered(owner, state, base, code)
        elif request.url.path.endswith('/installed'):
            url = flow.installed(owner, state, base, installation_id)
        else:
            result = flow.callback(owner, state, base, code)
            url = result.get('next_url')
            if not url:
                return _github_page("GitHub connected. You can close this tab and return to Kyrex.")
        return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    except GitHubError as exc:
        return _github_page(str(exc))
    except Exception:
        return _github_page("GitHub sign-in could not be completed. Return to Kyrex and tap Connect again.")


@router.post("/api/connections/github/disconnect")
def disconnect_github(request: Request):
    owner = _require_user(request)
    _github_flow().cancel(owner)
    return _github().disconnect(owner)

# Fitness connectors share the existing owner-authenticated Connections surface.
def _fitness():
    from fitness_connections import FitnessConnections
    return FitnessConnections()


@router.post('/api/connections/oura/connect')
def connect_oura(request: Request):
    from fitness_connections import FitnessError
    try:
        return _fitness().begin(_require_user(request))
    except (FitnessError, _connectors().ConnectorError) as exc:
        raise HTTPException(503, detail=str(exc)) from None


@router.get('/api/connections/oura/callback')
def finish_oura(request: Request, state: str = '', code: str = '', scope: str = '', error: str = '', iss: str = ''):
    from fitness_connections import FitnessError
    message = 'Oura connected. Return to Kyrex Chat.'
    try:
        if error:
            raise FitnessError('Oura authorization was not completed. Start again in Connections.')
        _fitness().complete(state, code, scope, issuer=iss)
    except FitnessError as exc:
        # Only implementation-defined messages; never arbitrary OAuth descriptions or secrets.
        safe_reasons = {
            'Oura needs host OAuth configuration before it can connect.',
            'Oura authorization was not completed. Start again in Connections.',
            'Connection link expired. Start again in Connections.',
            'Connection link expired or was already used. Start again.',
            'Connection changed. Start again.', 'Oura redirect changed. Start again.',
            'Oura authorization was not completed.',
            'No supported Oura permissions were granted. Connect again.',
            'Oura returned invalid credentials. Reconnect.',
            'Oura rejected the token exchange. Check the client ID, client secret and exact redirect URI, then connect again.',
            'Oura returned an unsupported authorization issuer. Start again in Connections.',
            'Oura could not be reached or returned invalid data.',
            'Oura rate limit reached. Try again later.',
            'Oura request failed. Try again later.'}
        message = str(exc) if str(exc) in safe_reasons else 'Oura connection was not completed. Return to Connections and try again.'
    except _connectors().ConnectorError:
        message = 'Oura credential storage is unavailable. Check the server connector encryption configuration.'
    import html
    return HTMLResponse('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><h1>Oura connection</h1><p>' + html.escape(message) + '</p><a href="/">Return to Kyrex</a></body></html>', headers={
        'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer',
        'Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"})


@router.post('/api/connections/fitness/{provider}/disconnect')
def disconnect_fitness(request: Request, provider: str):
    from fitness_connections import FitnessError
    owner = _require_user(request)
    try:
        return _fitness().disconnect(owner, provider)
    except FitnessError as exc:
        raise HTTPException(400, detail=str(exc)) from None


@router.post('/api/connections/samsung_health/pair')
def start_health_pair(request: Request):
    owner = _require_user(request)
    return _fitness().begin(owner, 'samsung_health')


async def _fitness_body(request: Request, limit=256_000):
    # Bound chunks even if a client omits or lies about Content-Length.
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit: raise HTTPException(413, detail='Health request is too large.')
        chunks.append(chunk)
    import json
    try:
        body = json.loads(b''.join(chunks))
        if not isinstance(body, dict): raise ValueError()
        return body
    except (ValueError, TypeError):
        raise HTTPException(400, detail='Invalid health request.') from None


@router.get('/api/connections/fitness/profile')
def get_fitness_profile(request: Request):
    owner = _require_user(request)
    from fastapi.responses import JSONResponse
    from connectors import ConnectorError
    try:
        return JSONResponse(_fitness().profile(owner), headers={'Cache-Control':'no-store'})
    except ConnectorError:
        raise HTTPException(503, detail='Fitness profile storage is unavailable.') from None


@router.put('/api/connections/fitness/profile')
async def save_fitness_profile(request: Request):
    owner = _require_user(request)
    body = await _fitness_body(request, limit=4096)
    from fastapi.responses import JSONResponse
    from fitness_connections import FitnessError
    from connectors import ConnectorError
    try:
        return JSONResponse(_fitness().save_profile(owner, body), headers={'Cache-Control':'no-store'})
    except FitnessError as exc:
        raise HTTPException(400, detail=str(exc)) from None
    except ConnectorError:
        raise HTTPException(503, detail='Fitness profile storage is unavailable.') from None


@router.delete('/api/connections/fitness/profile')
def clear_fitness_profile(request: Request):
    owner = _require_user(request)
    from fastapi.responses import JSONResponse
    return JSONResponse(_fitness().clear_profile(owner), headers={'Cache-Control':'no-store'})


@router.post('/api/connections/samsung_health/exchange')
async def exchange_health_pair(request: Request):
    # One-use high-entropy code grants ONLY device ingestion, never owner reads.
    from fitness_connections import FitnessError
    body = await _fitness_body(request)
    try:
        result = _fitness().pair(body.get('pairing_code', ''))
        from fastapi.responses import JSONResponse
        return JSONResponse(result, headers={'Cache-Control':'no-store'})
    except FitnessError as exc:
        raise HTTPException(400, detail=str(exc)) from None


@router.post('/api/connections/samsung_health/sync')
async def sync_health(request: Request):
    from fitness_connections import FitnessError
    header = request.headers.get('authorization', '')
    if not header.startswith('Bearer '): raise HTTPException(401, detail='Pair the health companion first.')
    body = await _fitness_body(request)
    try:
        return _fitness().upload(header[7:], body.get('records'), complete=body.get('complete') is True, skipped_records=body.get('skipped_records', 0))
    except FitnessError as exc:
        raise HTTPException(400, detail=str(exc)) from None


@router.get('/api/connections/fitness/read')
def read_fitness(request: Request, provider: str = 'all', start: str = '', end: str = '', collection: str = 'summary'):
    from fitness_connections import FitnessError
    owner = _require_user(request)
    try:
        from fastapi.responses import JSONResponse
        return JSONResponse(_fitness().read(owner,provider,start,end,collection), headers={'Cache-Control':'no-store'})
    except FitnessError as exc:
        raise HTTPException(400, detail=str(exc)) from None
