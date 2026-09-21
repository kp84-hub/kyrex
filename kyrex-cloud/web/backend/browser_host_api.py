"""browser_host_api.py — Cloud HTTP + WebSocket surface for Browser Hosts.

This is the missing Cloud half of the Browser Host channel: the routing that
lets the VPS host (``browser-host/agent.py``) actually reach the Cloud. It is
mounted into the REAL production FastAPI app (``main.py``) alongside
``chat_api.router``.

Endpoints
---------
  GET    /api/browser-hosts                 owner-scoped host list (+ endpoint)
  POST   /api/browser-hosts                 enroll a host; mints the secret ONCE
  GET    /api/browser-hosts/endpoint        the WSS URL the host must dial
  GET    /api/browser-hosts/{host_id}       one host's status (owner-scoped)
  DELETE /api/browser-hosts/{host_id}       revoke a host (terminal)
  WS     /api/browser-hosts/ws              the outbound host channel (HMAC auth)

Security model
--------------
* **Host authentication is HMAC based and happens on the WS.** The endpoint
  itself is public (the host dials in); the ``hello`` frame proves possession
  of the enrollment secret via ``HMAC_SHA256(secret, host_id + "." + nonce)``.
  The secret never travels. A bad proof is rejected and the socket closed.
* **The enrollment secret is returned exactly once.** ``POST /api/browser-hosts``
  returns ``secret`` only on the call that MINTS it; every later call returns
  ``secret: null``. A client-supplied secret is never accepted (that would let
  a caller choose a known key). ``browser_hosts.public_view`` never contains it.
* **Owner scoping.** Every HTTP view is scoped to the authenticated user; a
  host owned by anyone else is a 404 (invisible, not just forbidden).
* **No CDP, no credentials.** Nothing here returns a CDP URL, a provider
  credential, or the sealed blob. Host views are redacted by
  ``browser_hosts.public_view``; the WSS URL is the Cloud's own origin.
* **Cloud stays authoritative.** This surface only authenticates hosts and
  exposes lifecycle; policy and approvals remain in ``browser_host_channel``
  (``decide_operation``) — unchanged.

Railway
-------
The deployment is an HTTPS origin (e.g.
``https://kyrex-production.up.railway.app``). The host must dial the TLS
WebSocket form of the SAME origin: ``wss://<railway-host>/api/browser-hosts/ws``.
``wss_url`` performs that https -> wss mapping, and ``GET /api/browser-hosts/endpoint``
hands the operator the exact URL to put in ``KYREX_HOST_CLOUD_URL``. There is
no public CDP port anywhere in this path.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

import browser_host_channel as channel_mod
import browser_hosts as hosts

router = APIRouter(prefix="/api/browser-hosts", tags=["browser-hosts"])

# The single WS path a host dials. Kept here so the docs and the route can
# never drift.
WS_PATH = "/api/browser-hosts/ws"
_TRIGGER_MAX_SKEW = 90
_trigger_lock = threading.Lock()
_trigger_nonces: dict[str, float] = {}


# ── helpers ───────────────────────────────────────────────────────────

def _require_user(request: Request) -> str:
    """Reuse the Cloud's own auth (session cookie OR bearer)."""
    import main
    return main.require_user(request)


def _host_view(host_id: str) -> dict | None:
    """Secret-free view of a host, or ``None`` if absent or revoked.

    A revoked host is deliberately INVISIBLE here (404), so the status/list
    views agree: revoke removes the host from every surface, exactly as the
    list already excludes it.
    """
    rec = hosts.get_host(host_id)
    if rec is None or rec.state == hosts.STATE_REVOKED:
        return None
    return hosts.public_view(rec)


def wss_url(base: str, path: str = WS_PATH) -> str:
    """Build the TLS WebSocket URL a host dials, from an HTTP(S)/WS(S) origin.

    The Cloud is always reached over TLS in production, so ``http``/``ws`` are
    coerced UP to ``wss`` — a host must never be told to dial plaintext.
    """
    raw = str(base or "").strip().rstrip("/")
    if raw.startswith("https://"):
        raw = "wss://" + raw[len("https://"):]
    elif raw.startswith("http://"):
        raw = "wss://" + raw[len("http://"):]
    elif raw.startswith("ws://"):
        raw = "wss://" + raw[len("ws://"):]
    elif raw and not raw.startswith("wss://"):
        raw = "wss://" + raw
    return raw + path


def _endpoint_payload(request: Request) -> dict:
    """The WSS endpoint the VPS host must be configured with.

    Prefers the deployment-configured public origin (``KYREX_PUBLIC_BASE_URL``,
    set on Railway to the service's public HTTPS host). Falls back to the
    request origin, coerced to https then wss.
    """
    import main
    base = getattr(main, "PUBLIC_BASE_URL", "") or str(request.base_url).rstrip("/")
    return {
        "wss_url": wss_url(base),
        "path": WS_PATH,
        "env_var": "KYREX_HOST_CLOUD_URL",
        "note": ("Set KYREX_HOST_CLOUD_URL on the host to this wss:// URL. "
                 "Chromium CDP stays on loopback and is never published."),
    }


def _live_view(view: dict) -> dict:
    """Augment a public host view with the LIVE channel connection state."""
    out = dict(view)
    channel = channel_mod.default_manager().channel_for(out.get("host_id"))
    out["connected"] = bool(channel is not None and channel.authenticated)
    return out


def _calendar_bot_for_host(owner: str, host_id: str) -> dict | None:
    """Return the one running unified Calendar Bot explicitly bound here."""
    import bots
    import serve
    matches = [
        bot for bot in bots.load_bots().values()
        if str(bot.get("owner") or "").strip() == owner
        and str(bot.get("status") or "").strip() == "running"
        and serve.calendar_bot_granted(bot)
        and hosts.binding_for(owner, bot.get("id")) == host_id
    ]
    return matches[0] if len(matches) == 1 else None


def _consume_trigger_nonce(nonce: str, *, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        issued = int(str(nonce).split(".", 1)[0])
    except (TypeError, ValueError):
        return False
    if abs(now - issued) > _TRIGGER_MAX_SKEW:
        return False
    with _trigger_lock:
        for key, seen in list(_trigger_nonces.items()):
            if now - seen > _TRIGGER_MAX_SKEW:
                _trigger_nonces.pop(key, None)
        if nonce in _trigger_nonces:
            return False
        _trigger_nonces[nonce] = now
    return True


# ── enrollment ────────────────────────────────────────────────────────

@router.get("")
def list_hosts(request: Request):
    """Owner-scoped list of enrolled hosts (redacted) plus the WSS endpoint."""
    user = _require_user(request)
    return {
        "hosts": [_live_view(v) for v in hosts.list_hosts(user)],
        "endpoint": _endpoint_payload(request),
    }


@router.post("")
async def enroll_host(request: Request):
    """Enroll a host for the authenticated owner, minting its secret ONCE.

    Body: ``{"host_id": "...", "name": "...", "allowlist": ["example.com"]}``.
    Returns ``{"host": <public view>, "secret": <string|None>, "endpoint": ...}``.

    ``secret`` is non-null ONLY when this call minted a new one — hand it to the
    host out of band and store it nowhere else. Re-enrolling the same host
    (e.g. to rename it or change its allowlist) PRESERVES the existing secret
    and returns ``secret: null``. A client-supplied ``secret`` is ignored.
    """
    user = _require_user(request)
    body = await request.json()
    host_id = str(body.get("host_id") or "").strip()
    name = str(body.get("name") or "").strip()
    # A client may NOT choose the secret: that would let a caller pick a known
    # key. Only ever mint server-side.
    try:
        result = hosts.enroll_host(user, host_id, name=name,
                                   allowlist=body.get("allowlist"))
    except hosts.HostError as exc:
        message = str(exc)
        status = 409 if "another owner" in message else 400
        raise HTTPException(status_code=status, detail=message)
    return {
        "host": _live_view(result["host"]),
        "secret": result["secret"],          # None unless just minted
        "endpoint": _endpoint_payload(request),
    }


@router.get("/endpoint")
def get_endpoint(request: Request):
    """The exact WSS URL the host dials (Railway docs / host configuration)."""
    _require_user(request)
    return _endpoint_payload(request)


@router.get("/ws")
def browser_host_ws_http_guard(request: Request):
    """HTTP guard for the WebSocket-only path ``/api/browser-hosts/ws``.

    The channel itself is served in the *websocket* scope by
    :func:`browser_host_ws`. A plain HTTP GET to the same path means the
    upgrade never happened (e.g. the Cloud runtime has no websocket
    implementation such as ``websockets``). Without this guard that request
    falls through to the session-guarded ``GET /{host_id}`` route and is
    answered ``401`` — misreporting a transport fault as an authentication
    failure. Registered BEFORE ``/{host_id}`` so it wins the match.
    """
    raise HTTPException(
        status_code=426,
        detail=("WebSocket upgrade required. Dial this path over the WebSocket "
                "protocol (wss://), not plain HTTP. If you are the host agent "
                "and expected an upgrade, the Cloud runtime is missing its "
                "'websockets' dependency."),
        headers={"Upgrade": "websocket"},
    )


@router.post("/google-messages-trigger")
async def google_messages_trigger(request: Request):
    """Queue the fixed Level 6 reply for an authenticated host trigger."""
    body = await request.json()
    host_id = str(body.get("host_id") or "").strip()
    nonce = str(body.get("nonce") or "").strip()
    proof = str(body.get("proof") or "").strip()
    trigger_id = str(body.get("trigger_id") or "").strip().lower()
    if (len(trigger_id) != 64
            or any(ch not in "0123456789abcdef" for ch in trigger_id)):
        raise HTTPException(status_code=400, detail="invalid trigger")
    if not hosts.verify_proof(host_id, nonce, proof):
        raise HTTPException(status_code=401, detail="authentication failed")
    if not _consume_trigger_nonce(nonce):
        raise HTTPException(status_code=409, detail="expired or repeated trigger")
    rec = hosts.get_host(host_id)
    owner = str(getattr(rec, "owner", "") or "")
    bot = _calendar_bot_for_host(owner, host_id)
    if bot is None:
        raise HTTPException(status_code=409,
                            detail="Calendar Bot binding unavailable")

    import main
    import serve
    from task_store import DuplicateTaskId
    task_id = "gm-" + hashlib.sha256(
        f"{host_id}:{trigger_id}".encode()).hexdigest()[:32]
    try:
        main.store.submit(
            session_key=bot["id"], task_text=serve.LEVEL6_MESSAGE_REQUEST,
            repo_url=None, executor_prefix="level6", bot_id=bot["id"],
            rift=str(bot.get("rift") or ""), chat_id=owner,
            task_id=task_id, resolve_bot=True)
        status = "queued"
    except DuplicateTaskId:
        status = "duplicate"
    return {"status": status, "task_id": task_id}


@router.get("/{host_id}")
def get_host(host_id: str, request: Request):
    """One host's status (owner-scoped). A foreign/unknown id is a 404."""
    user = _require_user(request)
    view = _host_view(host_id)
    if view is None or str(view.get("owner")) != user:
        raise HTTPException(status_code=404, detail="Browser host not found")
    view = dict(view)
    view["owner"] = user  # never echo an unexpected owner value
    return {"host": _live_view(view), "endpoint": _endpoint_payload(request)}


@router.delete("/{host_id}")
def revoke_host(host_id: str, request: Request):
    """Revoke an enrolled host (terminal; drops bindings and the secret)."""
    user = _require_user(request)
    view = _host_view(host_id)
    if view is None or str(view.get("owner")) != user:
        raise HTTPException(status_code=404, detail="Browser host not found")
    revoked = hosts.revoke_host(user, host_id)
    return {"revoked": bool(revoked), "host_id": host_id}


# ── the outbound host channel (WebSocket) ─────────────────────────────
#
# One connection == one host. Frames are newline-free JSON text; the host
# authenticates with its ``hello`` frame. The bridge runs the async socket and
# the thread-based HostChannel together: inbound frames are handled on the loop,
# outbound frames (which may originate on a worker thread during a task
# dispatch) hop back via ``call_soon_threadsafe``.

@router.websocket("/ws")
async def browser_host_ws(websocket: WebSocket):
    await websocket.accept()
    manager = channel_mod.default_manager()
    loop = asyncio.get_running_loop()
    out_q: "asyncio.Queue" = asyncio.Queue()
    _CLOSE = object()

    def send(frame: dict) -> None:
        try:
            loop.call_soon_threadsafe(out_q.put_nowait, frame)
        except RuntimeError:
            # The loop is gone (shutdown): nothing to deliver.
            pass

    channel = manager.attach(send)

    async def writer() -> None:
        while True:
            item = await out_q.get()
            if item is _CLOSE:
                return
            try:
                await websocket.send_text(channel_mod.encode(item))
            except Exception:
                return

    writer_task = asyncio.create_task(writer())
    try:
        while not channel.closed:
            raw = await websocket.receive_text()
            try:
                frame = channel_mod.decode(raw)
            except channel_mod.ProtocolError:
                break
            channel.handle(frame)
            if channel.closed:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.detach(channel)
        try:
            loop.call_soon_threadsafe(out_q.put_nowait, _CLOSE)
        except RuntimeError:
            pass
        try:
            await asyncio.wait_for(writer_task, timeout=2)
        except Exception:
            writer_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
