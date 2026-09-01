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
import json
import os
import secrets
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# IDE (desktop) client auth plumbing — sibling module, stdlib-only. The
# Kyrex IDE is a different origin with no shared cookie jar: it signs in
# via ?client=ide and authenticates every API call with header tokens.
from ide_auth import (
    CORS_ALLOWED_HEADERS,
    is_ide_state,
    make_ide_state,
    parse_cors_origins,
    session_token_from,
)

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

# ── globals ────────────────────────────────────────────────────────

# In-memory session store (simple; single-process, fine for Render).
sessions: dict[str, str] = {}  # session_token -> github_username

# Shared task store. This process only submits; the TaskWorker in the
# worker process claims and executes through serve.run_task, which
# applies tier derivation, policy, approvals, and audit.
store = CloudTaskStore()

app = FastAPI(title="Kyrex Cloud Web", version="1.0.0")

# ── CORS (desktop/IDE clients) ─────────────────────────────────────
# The browser UI is same-origin, so CORS only matters for the Kyrex IDE
# calling the API from its own origin (tauri://localhost, and the
# localhost scheme WebView2 uses on Windows). WEB_CORS_ORIGINS overrides
# the Tauri defaults; "*" allows any origin (acceptable: the API is
# single-operator by design).
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_cors_origins(os.environ.get("WEB_CORS_ORIGINS")),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=CORS_ALLOWED_HEADERS,
)

# ── helpers ────────────────────────────────────────────────────────

def get_session_user(request: Request) -> Optional[str]:
    """Return the GitHub username for this session, or None.

    Resolves the browser session cookie and, with equal standing, the
    header credentials the Kyrex IDE sends (X-Session-Token / Bearer) —
    see ide_auth.session_token_from for the precedence rules.
    """
    token = session_token_from(request.cookies.get("session"), request.headers)
    if token and token in sessions:
        return sessions[token]
    return None


def require_user(request: Request) -> str:
    user = get_session_user(request)
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

# Page served by /auth/callback when the flow was started by the Kyrex
# IDE (?client=ide on /auth/login). A Tauri app shares no cookie jar
# with this backend, so instead of redirecting to the web UI the page
# displays the session token for the operator to paste into the IDE.
# __USER__/__TOKEN__ are replaced with .replace() (never str.format —
# the CSS braces would fight it).
IDE_TOKEN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Kyrex IDE sign-in</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0d1117; color: #e6edf3; max-width: 560px;
         margin: 0 auto; padding: 48px 24px; line-height: 1.5; }
  h1 { font-size: 1.3rem; margin-bottom: 16px; }
  p { color: #8b949e; }
  pre { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
        padding: 16px; font-size: 0.9rem; word-break: break-all;
        white-space: pre-wrap; user-select: all; }
  .warn { color: #d29922; font-size: 0.85rem; }
</style>
</head>
<body>
<h1>Kyrex IDE — signed in as __USER__</h1>
<p>Copy this session token into the Kyrex IDE to finish signing in:</p>
<pre>__TOKEN__</pre>
<p class="warn">Treat this token like a password: it grants access to your
Kyrex Cloud session for 7 days.</p>
</body>
</html>"""


@app.get("/auth/login")
def login(request: Request, client: Optional[str] = None):
    redirect_uri = str(request.base_url) + "auth/callback"
    # ?client=ide marks the flow for the desktop app: the callback then
    # serves the session-token page instead of the cookie redirect.
    # Browser flows keep an unguessable random state.
    state = make_ide_state() if client == "ide" else secrets.token_hex(16)
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "read:user",
        "state": state,
    }
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{urlencode(params)}")


@app.get("/auth/callback")
def callback(code: str, request: Request, state: Optional[str] = None):
    redirect_uri = str(request.base_url) + "auth/callback"
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

    if is_ide_state(state):
        # IDE-initiated flow: show the token for pasting into the desktop
        # app. No cookie redirect — the IDE authenticates via headers.
        return HTMLResponse(
            IDE_TOKEN_PAGE.replace("__USER__", username)
                          .replace("__TOKEN__", session_token)
        )

    response = RedirectResponse(url="/")
    response.set_cookie(key="session", value=session_token, httponly=True, max_age=86400 * 7)
    return response


@app.get("/api/me")
def me(request: Request):
    user = get_session_user(request)
    if not user:
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
    # Telegram) and routes any approval reply back via store.respond().
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
    """Auth for the event stream: ?session= token, header credentials,
    or the session cookie.

    The IDE's EventSource cannot set headers, so it passes the token as
    ?session= (and reconnects resume via Last-Event-ID + the same param);
    same-origin browser tabs use the cookie; header credentials cover
    fetch-based consumers of the same endpoint.
    """
    token = session_param or session_token_from(
        request.cookies.get("session"), request.headers
    )
    if token and token in sessions:
        return sessions[token]
    return None


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
    """Route an approval reply (y/n/token) to a task's pending approval.

    Preserves the serve approval protocol: the decision lands in the same
    in-memory pending entry the executor is waiting on, and is persisted
    to the durable approval_requests table by the worker's callbacks.
    """
    user = require_user(request)
    task = store.get(task_id)
    if task is None or task.get("session_key") != user:
        raise HTTPException(status_code=404, detail="Task not found")
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    delivered = store.respond(task_id, text)
    return {"delivered": bool(delivered)}


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