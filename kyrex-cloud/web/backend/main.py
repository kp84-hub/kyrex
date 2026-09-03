#!/usr/bin/env python3
"""
kyrex-cloud/web/backend/main.py — Kyrex Cloud Web trigger.

FastAPI application providing:
  - "Sign in with GitHub" OAuth (single allowed username via env var)
  - POST /api/task — queue a task in the shared store (worker executes it)
  - GET /api/task/{id}/events — Flux: live task event stream (Server-Sent
    Events over the durable task_events table; cursor-resumable)
  - POST /api/task/{id}/respond — route an approval reply to a pending task
  - POST /api/task/{id}/cancel — request cancellation of a task
  - List of past results via /api/results

This app only *submits* tasks to the shared CloudTaskStore and streams
events from it (flux.py). The worker process is the single execution
path, so every task — Telegram or web — goes through the same
tier/approval/policy/audit gate in serve.py.
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ── paths ──────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent          # web/backend/
WEB_DIR = SCRIPT_DIR.parent                            # web/
KYREX_CLOUD_DIR = WEB_DIR.parent                       # kyrex-cloud/
GIT_WORKFLOW = KYREX_CLOUD_DIR / "git_workflow.py"
RESULTS_DIR = KYREX_CLOUD_DIR / "results"

# The web app is a *submitter*: it shares the CloudTaskStore (SQLite
# under DATA_DIR) with the worker process, which is the only thing that
# executes tasks. Import it here, after KYREX_CLOUD_DIR is known.
sys.path.insert(0, str(KYREX_CLOUD_DIR))
from task_store import CloudTaskStore  # noqa: E402
import flux  # noqa: E402  — durable, cursor-based task event streaming

# ── env ────────────────────────────────────────────────────────────
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]
ALLOWED_USERNAME = os.environ["WEB_ALLOWED_GITHUB_USERNAME"]
REPO_URL = os.environ.get("KYREX_TARGET_REPO_URL", "https://github.com/kp84-hub/kyrex.git")
BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")
SESSION_SECRET = os.environ.get("WEB_SESSION_SECRET", secrets.token_hex(32))
# Public base URL used to build GitHub-facing callback URLs. Deployment
# configuration: set KYREX_PUBLIC_BASE_URL (e.g. https://kyrex-production.up.railway.app)
# to the externally reachable HTTPS origin. When unset, the request base is
# used with the scheme coerced to https so callbacks never receive http://.
PUBLIC_BASE_URL = os.environ.get("KYREX_PUBLIC_BASE_URL", "").rstrip("/")

# ── globals ────────────────────────────────────────────────────────

# In-memory session store (simple; single-process, fine for Render).
sessions: dict[str, str] = {}  # session_token -> github_username

# Desktop OAuth uses a fixed custom-scheme redirect and short-lived, opaque
# handoff codes. These stores are process-local, matching browser sessions.
DESKTOP_REDIRECT_URI = "kyrex://auth/callback"
DESKTOP_REDIRECT_ALLOWLIST = frozenset(
    value.strip() for value in os.environ.get(
        "KYREX_DESKTOP_REDIRECT_ALLOWLIST", DESKTOP_REDIRECT_URI
    ).split(",") if value.strip()
)
DESKTOP_TX_TTL_SECONDS = int(os.environ.get("KYREX_DESKTOP_TX_TTL_SECONDS", "600"))
DESKTOP_CODE_TTL_SECONDS = int(os.environ.get("KYREX_DESKTOP_CODE_TTL_SECONDS", "120"))
desktop_transactions: dict[str, dict] = {}
desktop_handoffs: dict[str, dict] = {}
desktop_access_tokens: dict[str, dict] = {}
desktop_refresh_tokens: dict[str, dict] = {}
desktop_lock = threading.Lock()
DESKTOP_ACCESS_TTL_SECONDS = int(os.environ.get("KYREX_DESKTOP_ACCESS_TTL_SECONDS", "900"))
DESKTOP_REFRESH_TTL_SECONDS = int(os.environ.get("KYREX_DESKTOP_REFRESH_TTL_SECONDS", str(60 * 60 * 24 * 30)))

# Server-side OAuth login transactions.  State is deliberately not placed in
# the browser cookie: it is a one-time transaction credential, not a session.
# The lock makes consume_oauth_state atomic within this process, matching the
# existing process-local session architecture.
OAUTH_STATE_TTL_SECONDS = int(os.environ.get("WEB_OAUTH_STATE_TTL_SECONDS", "600"))
oauth_states: dict[str, dict[str, float | bool]] = {}
oauth_state_lock = threading.Lock()

# Shared task store. This process only submits; the TaskWorker in the
# worker process claims and executes through serve.run_task, which
# applies tier derivation, policy, approvals, and audit.
store = CloudTaskStore()

app = FastAPI(title="Kyrex Cloud Web", version="1.0.0")

# ── CORS ───────────────────────────────────────────────────────────
# The Tauri IDE WebView is a cross-origin client (dev: http://localhost:1420,
# packaged: tauri://localhost). Browser users are same-origin (frontend is
# mounted at "/"), so they are unaffected; this only opens the desktop-flow
# origins used by the IDE. Overridable via KYREX_IDE_ALLOWED_ORIGINS.
KYREX_IDE_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "KYREX_IDE_ALLOWED_ORIGINS", "http://localhost:1420,tauri://localhost"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=KYREX_IDE_ALLOWED_ORIGINS,
    allow_credentials=False,  # desktop flow uses bearer tokens, not cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# ── helpers ────────────────────────────────────────────────────────

def github_oauth_base_url(request: Request) -> str:
    """Return the origin used to build GitHub-facing OAuth callback URLs.

    Prefers the deployment-configured KYREX_PUBLIC_BASE_URL. When unset, the
    request base is used with the scheme coerced to https so callbacks are
    never assembled with an http:// origin (which GitHub rejects).
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    base = str(request.base_url).rstrip("/")
    return base.replace("http://", "https://", 1)


def get_session_user(request: Request) -> Optional[str]:
    """Return the GitHub username for the browser session, if present."""
    token = request.cookies.get("session")
    return sessions.get(token) if token else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_desktop_tokens(user: str, family: Optional[str] = None) -> dict:
    now = time.time()
    family = family or secrets.token_urlsafe(24)
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(48)
    with desktop_lock:
        desktop_access_tokens[_token_hash(access)] = {"user": user, "expires_at": now + DESKTOP_ACCESS_TTL_SECONDS, "revoked": False, "family": family}
        desktop_refresh_tokens[_token_hash(refresh)] = {"user": user, "expires_at": now + DESKTOP_REFRESH_TTL_SECONDS, "revoked": False, "family": family, "used": False}
    return {"access_token": access, "refresh_token": refresh, "expires_in": DESKTOP_ACCESS_TTL_SECONDS, "username": user}


def _bearer_user(request: Request) -> Optional[str]:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None
    with desktop_lock:
        entry = desktop_access_tokens.get(_token_hash(token))
        if not entry or entry["revoked"] or entry["expires_at"] <= time.time():
            return None
        return entry["user"]


def require_user(request: Request) -> str:
    user = get_session_user(request) or _bearer_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def format_result_summary(result: dict) -> dict:
    """Build a short summary dict suitable for the past-results list."""
    status = result.get("status", "unknown")
    summary = {
        "task": result.get("task", ""),
        "status": status,
        "branch": result.get("branch", ""),
        "started_at": result.get("started_at", ""),
        "finished_at": result.get("finished_at", ""),
        "pr_url": None,
        "review": None,
        "final_response": (result.get("final_response") or "")[:500],
        "errors": (result.get("errors") or [])[-1:] if result.get("errors") else [],
    }
    pr = result.get("pull_request")
    if pr and pr.get("url"):
        summary["pr_url"] = pr["url"]
    review = result.get("review")
    if review and review.get("available"):
        summary["review"] = {
            "matches_task": review.get("matches_task"),
            "reasoning": review.get("reasoning", ""),
        }
    return summary


# ── OAuth routes ───────────────────────────────────────────────────


def create_oauth_state() -> str:
    """Create a short-lived, single-use server-side OAuth transaction."""
    state = secrets.token_urlsafe(32)
    with oauth_state_lock:
        now = time.time()
        # Opportunistically discard expired transactions while holding the
        # same lock used by callback consumption.
        for key, transaction in list(oauth_states.items()):
            if transaction["expires_at"] <= now:
                del oauth_states[key]
        oauth_states[state] = {
            "created_at": now,
            "expires_at": now + OAUTH_STATE_TTL_SECONDS,
            "consumed": False,
        }
    return state


def consume_oauth_state(state: str) -> None:
    """Atomically validate and consume an OAuth state value."""
    with oauth_state_lock:
        transaction = oauth_states.get(state)
        if transaction is None:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")
        if transaction["consumed"]:
            raise HTTPException(status_code=400, detail="OAuth state already consumed")
        if transaction["expires_at"] <= time.time():
            del oauth_states[state]
            raise HTTPException(status_code=400, detail="OAuth state expired")
        transaction["consumed"] = True


def _validate_desktop_redirect(redirect_uri: str) -> None:
    if not redirect_uri or redirect_uri not in DESKTOP_REDIRECT_ALLOWLIST:
        raise HTTPException(status_code=400, detail="Invalid desktop redirect URI")


def _consume_desktop_transaction(cloud_state: str) -> dict:
    with desktop_lock:
        tx = desktop_transactions.get(cloud_state)
        if tx is None:
            raise HTTPException(status_code=400, detail="Invalid desktop OAuth state")
        if tx["consumed"]:
            raise HTTPException(status_code=400, detail="Desktop OAuth state already consumed")
        if tx["expires_at"] <= time.time():
            del desktop_transactions[cloud_state]
            raise HTTPException(status_code=400, detail="Desktop OAuth state expired")
        tx["consumed"] = True
        return dict(tx)


def _github_user(code: str, redirect_uri: str, verifier: Optional[str] = None) -> str:
    values = {"client_id": GITHUB_CLIENT_ID, "client_secret": GITHUB_CLIENT_SECRET,
              "code": code, "redirect_uri": redirect_uri}
    if verifier:
        values["code_verifier"] = verifier
    token_req = urllib.request.Request(
        "https://github.com/login/oauth/access_token", data=urlencode(values).encode(),
        headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(token_req, timeout=15) as resp:
            token_resp = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise HTTPException(status_code=502, detail=f"GitHub token exchange failed: {e} — {body}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub token exchange failed: {e}")
    access_token = token_resp.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="GitHub did not return an access token")
    user_req = urllib.request.Request("https://api.github.com/user",
                                      headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(user_req, timeout=15) as resp:
            username = json.loads(resp.read()).get("login")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub user fetch failed: {e}")
    if username != ALLOWED_USERNAME:
        raise HTTPException(status_code=403, detail=f"Access denied: {username} is not authorized")
    return username


@app.get("/auth/desktop/start")
def desktop_start(state: str, redirect_uri: str, code_challenge: str, code_challenge_method: str = "S256", code_verifier: str = "", request: Request = None):
    _validate_desktop_redirect(redirect_uri)
    if not state or not code_challenge or not code_verifier or code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="PKCE state, challenge, and verifier are required")
    expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    if not secrets.compare_digest(expected_challenge, code_challenge):
        raise HTTPException(status_code=400, detail="PKCE verifier does not match challenge")
    cloud_state = create_oauth_state()
    now = time.time()
    with desktop_lock:
        desktop_transactions[cloud_state] = {"ide_state": state, "redirect_uri": redirect_uri,
            "code_challenge": code_challenge, "code_verifier": code_verifier, "created_at": now,
            "expires_at": now + DESKTOP_TX_TTL_SECONDS, "consumed": False}
    params = {"client_id": GITHUB_CLIENT_ID, "redirect_uri": github_oauth_base_url(request) + "/auth/desktop/callback",
              "scope": "read:user", "state": cloud_state, "code_challenge": code_challenge,
              "code_challenge_method": "S256"}
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{urlencode(params)}")


@app.get("/auth/desktop/callback")
def desktop_callback(code: str, state: str, request: Request):
    # Validate the independent Cloud/GitHub state before any token exchange.
    consume_oauth_state(state)
    tx = _consume_desktop_transaction(state)
    github_redirect_uri = github_oauth_base_url(request) + "/auth/desktop/callback"
    username = _github_user(code, github_redirect_uri, tx["code_verifier"])
    handoff = secrets.token_urlsafe(32)
    now = time.time()
    with desktop_lock:
        desktop_handoffs[hashlib.sha256(handoff.encode()).hexdigest()] = {
            "user": username, "transaction": state, "redirect_uri": tx["redirect_uri"],
            "code_challenge": tx["code_challenge"], "created_at": now,
            "expires_at": now + DESKTOP_CODE_TTL_SECONDS, "consumed": False}
    target = tx["redirect_uri"] + "?" + urlencode({"code": handoff, "state": tx["ide_state"]})
    return RedirectResponse(target)


@app.post("/auth/desktop/exchange")
async def desktop_exchange(request: Request):
    body = await request.json()
    code, redirect_uri, verifier = body.get("code"), body.get("redirect_uri"), body.get("code_verifier")
    _validate_desktop_redirect(redirect_uri)
    if not code or not verifier:
        raise HTTPException(status_code=400, detail="code, redirect_uri, and code_verifier are required")
    digest = hashlib.sha256(code.encode()).hexdigest()
    with desktop_lock:
        handoff = desktop_handoffs.get(digest)
        if handoff is None or handoff["consumed"]:
            raise HTTPException(status_code=400, detail="Invalid or consumed handoff code")
        if handoff["expires_at"] <= time.time():
            del desktop_handoffs[digest]
            raise HTTPException(status_code=400, detail="Handoff code expired")
        if handoff["redirect_uri"] != redirect_uri:
            raise HTTPException(status_code=400, detail="Redirect URI mismatch")
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not secrets.compare_digest(expected, handoff["code_challenge"]):
            raise HTTPException(status_code=400, detail="PKCE verifier mismatch")
        handoff["consumed"] = True
        user = handoff["user"]
    return _new_desktop_tokens(user)


@app.post("/auth/desktop/refresh")
async def desktop_refresh(request: Request):
    body = await request.json()
    refresh = body.get("refresh_token")
    if not refresh:
        raise HTTPException(status_code=400, detail="refresh_token is required")
    with desktop_lock:
        entry = desktop_refresh_tokens.get(_token_hash(refresh))
        if not entry or entry["revoked"] or entry["used"] or entry["expires_at"] <= time.time():
            if entry:
                entry["revoked"] = True
            raise HTTPException(status_code=401, detail="Invalid refresh credential")
        entry["used"] = True
        family = entry["family"]
        user = entry["user"]
    return _new_desktop_tokens(user, family)


@app.post("/auth/desktop/logout")
def desktop_logout(request: Request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = header[7:].strip()
    with desktop_lock:
        access = desktop_access_tokens.get(_token_hash(token))
        if not access or access["revoked"]:
            raise HTTPException(status_code=401, detail="Not authenticated")
        family = access["family"]
        for entry in list(desktop_access_tokens.values()) + list(desktop_refresh_tokens.values()):
            if entry["family"] == family:
                entry["revoked"] = True
    return {"revoked": True}


@app.get("/auth/login")
def login(request: Request):
    redirect_uri = github_oauth_base_url(request) + "/auth/callback"
    state = create_oauth_state()
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "read:user",
        "state": state,
    }
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{urlencode(params)}")


@app.get("/auth/callback")
def callback(code: str, state: str, request: Request):
    consume_oauth_state(state)
    redirect_uri = github_oauth_base_url(request) + "/auth/callback"
    # Exchange code for access token
    token_data = urlencode({
        "client_id": GITHUB_CLIENT_ID,
        "client_secret": GITHUB_CLIENT_SECRET,
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()
    token_req = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=token_data,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(token_req, timeout=15) as resp:
            token_resp = json.loads(resp.read())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub token exchange failed: {e}")

    access_token = token_resp.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="GitHub did not return an access token")

    # Fetch the user's profile
    user_req = urllib.request.Request(
        "https://api.github.com/user",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(user_req, timeout=15) as resp:
            user_data = json.loads(resp.read())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GitHub user fetch failed: {e}")

    username = user_data.get("login")
    if username != ALLOWED_USERNAME:
        raise HTTPException(status_code=403, detail=f"Access denied: {username} is not authorized")

    # Create session
    session_token = secrets.token_hex(32)
    sessions[session_token] = username

    response = RedirectResponse(url="/")
    response.set_cookie(key="session", value=session_token, httponly=True, max_age=86400 * 7)
    return response


@app.get("/api/me")
def me(request: Request):
    user = get_session_user(request) or _bearer_user(request)
    if not user:
        if request.headers.get("authorization"):
            raise HTTPException(status_code=401, detail="Invalid or expired bearer credential")
        return {"authenticated": False}
    return {"authenticated": True, "username": user}


@app.get("/auth/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token and token in sessions:
        del sessions[token]
    response = RedirectResponse(url="/")
    response.set_cookie(key="session", value="", httponly=True, max_age=0)
    return response


# ── task routes ────────────────────────────────────────────────────

@app.post("/api/task")
async def accept_task(request: Request):
    """Queue a task in the shared store. The worker executes it, gated.

    This endpoint never runs the agent itself — doing so would bypass the
    tier/approval/policy/audit gate in serve.py. It only enqueues; the
    TaskWorker (separate process) is the single execution path.
    """
    user = require_user(request)
    body = await request.json()
    task_text = (body.get("task") or "").strip()
    if not task_text:
        raise HTTPException(status_code=400, detail="Task text is required")
    if len(task_text) > 2000:
        raise HTTPException(status_code=400, detail="Task text too long (max 2000 chars)")

    # session_key and chat_id are the operator username; the worker's
    # notifier treats a non-numeric chat_id as a web session (not
    # Telegram). Web approval replies are durably handed to the worker.
    # resolve_bot=False: the session_key is a GitHub username, not a Bot
    # binding.  A registered Bot whose id happens to equal this username
    # must never be resolved for a web task (no Rift/policy/identity
    # inheritance) — Telegram bot tasks are submitted through the bot
    # path with an explicit Bot session and keep the default behaviour.
    task_id = store.submit(
        session_key=user,
        task_text=task_text,
        repo_url=REPO_URL,
        executor_prefix="repo",
        chat_id=user,
        resolve_bot=False,
    )
    return {"status": "queued", "task_id": task_id, "task": task_text}


@app.get("/api/task/{task_id}")
def get_task(task_id: str, request: Request):
    """Return one task's state from the shared store."""
    user = require_user(request)
    task = store.get(task_id)
    if task is None or task.get("session_key") != user:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "task": task.get("task_text", ""),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
        "result": task.get("result"),
        "error": task.get("error"),
    }


@app.get("/api/results")
def list_results(request: Request):
    """Return recent task state from the shared store, scoped to the
    authenticated user only — a task (including its task text and final
    response) must never be visible to another user."""
    user = require_user(request)
    results_raw = []
    for task in store.list_tasks(session_key=user, limit=50):
        result = task.get("result") or {}
        if not isinstance(result, dict):
            result = {}
        # Prefer the executor's own status when the run finished;
        # otherwise surface the lifecycle status (queued/running/...).
        merged = {
            "task_id": task.get("task_id", ""),
            "task": task.get("task_text", ""),
            "status": result.get("status") or task.get("status", "unknown"),
            "branch": result.get("branch", ""),
            "started_at": task.get("started_at", ""),
            "finished_at": task.get("finished_at", ""),
            "final_response": result.get("final_response", ""),
            "errors": result.get("errors", []),
            "pull_request": result.get("pull_request"),
            "review": result.get("review"),
        }
        results_raw.append(format_result_summary(merged))
    return {"results": results_raw}


# ── Flux: task event stream (SSE) ──────────────────────────────────

# Hard lifetime for one event stream, so a walked-away-from browser tab
# cannot pin a polling thread forever. Tasks themselves have watchdogs and
# the store recovers orphans; this is only the consumer-side bound.
FLUX_STREAM_MAX_SECONDS = float(os.environ.get("KYREX_FLUX_STREAM_MAX_SECONDS", "3600"))
# SSE comment ping cadence — keeps proxies/CDNs from closing an idle
# connection while a long task is quiet.
FLUX_PING_SECONDS = 15.0


def _stream_user(request: Request, session_param: Optional[str]) -> Optional[str]:
    """Auth for the event stream: session cookie OR ?session= token.

    A same-origin EventSource sends cookies, so the cookie is the primary
    path; the query param exists for parity with the old WebSocket and for
    non-browser clients.
    """
    token = session_param or request.cookies.get("session")
    if token and token in sessions:
        return sessions[token]
    return _bearer_user(request)


@app.get("/api/task/{task_id}/events")
async def task_events(
    task_id: str,
    request: Request,
    after: Optional[int] = None,
    session: Optional[str] = None,
):
    """Stream one task's events as Server-Sent Events (flux.py).

    Replays history from the cursor, then tails live until the task is
    terminal. Cursor resolution order: ?after= query param, else the
    Last-Event-ID header (EventSource auto-reconnect), else 0 (full
    replay). The final SSE frame is an ``end`` event carrying the task's
    final status.
    """
    user = _stream_user(request, session)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    task = store.get(task_id)
    if task is None or task.get("session_key") != user:
        raise HTTPException(status_code=404, detail="Task not found")

    if after is not None:
        cursor = int(after)
    else:
        last_id = request.headers.get("last-event-id")
        try:
            cursor = int(last_id) if last_id else 0
        except ValueError:
            cursor = 0

    return StreamingResponse(
        _sse_response(task_id, cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _sse_response(task_id: str, cursor: int):
    """Bridge the blocking flux generator into an async SSE body.

    The pump thread walks flux.stream_events — store polling must never
    run on the event loop — and hands events to the loop via a
    call_soon_threadsafe hop. Idle periods emit SSE comment pings.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    def put(item) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def pump() -> None:
        try:
            for event in flux.stream_events(
                store, task_id,
                after_event_id=cursor,
                max_seconds=FLUX_STREAM_MAX_SECONDS,
            ):
                put(event)
        except Exception as exc:  # never leave the stream silently dead
            put({"event_id": None, "type": "error",
                 "payload": {"error": f"stream failure: {exc}"},
                 "created_at": None})
        finally:
            put(sentinel)

    threading.Thread(target=pump, daemon=True,
                     name=f"flux-{task_id[:16]}").start()
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=FLUX_PING_SECONDS)
        except asyncio.TimeoutError:
            yield ": ping\n\n"
            continue
        if item is sentinel:
            break
        yield flux.format_sse(item)


@app.post("/api/task/{task_id}/respond")
async def respond_task(task_id: str, request: Request):
    """Durably record an operator reply for the worker-side bridge."""
    user = require_user(request)
    task = store.get(task_id)
    if task is None or task.get("session_key") != user:
        raise HTTPException(status_code=404, detail="Task not found")
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    recorded = store.record_operator_reply(task_id, text)
    return {"recorded": bool(recorded)}


@app.post("/api/task/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    """Request cancellation: immediate for queued tasks, flagged for a
    running task (applied by the worker at the next approval gate or
    finalisation)."""
    user = require_user(request)
    task = store.get(task_id)
    if task is None or task.get("session_key") != user:
        raise HTTPException(status_code=404, detail="Task not found")
    requested = store.request_cancel(task_id)
    return {"requested": bool(requested), "status": store.status(task_id)}


# ── static frontend ────────────────────────────────────────────────

FRONTEND_DIR = WEB_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


# ── entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)