#!/usr/bin/env python3
"""
kyrex-cloud/web/backend/main.py — Kyrex Cloud Web trigger.

FastAPI application providing:
  - "Sign in with GitHub" OAuth (single allowed username via env var)
  - POST /api/task — queue a task in the shared store (worker executes it)
  - WebSocket /ws/task — live-stream KYREX_PROGRESS lines, then final result
  - One task at a time (busy lock, same principle as telegram_bot.py)
  - List of past results via /api/results

This app only *submits* tasks to the shared CloudTaskStore. The worker
process is the single execution path, so every task — Telegram or web —
goes through the same tier/approval/policy/audit gate in serve.py.
"""

import asyncio
import json
import os
import secrets
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
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

# ── env ────────────────────────────────────────────────────────────
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]
ALLOWED_USERNAME = os.environ["WEB_ALLOWED_GITHUB_USERNAME"]
REPO_URL = os.environ.get("KYREX_TARGET_REPO_URL", "https://github.com/kp84-hub/kyrex.git")
BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")
SESSION_SECRET = os.environ.get("WEB_SESSION_SECRET", secrets.token_hex(32))

# ── globals ────────────────────────────────────────────────────────
busy_lock = threading.Lock()
active_task: dict = {"task": None, "ws": None}  # single active WebSocket

# In-memory session store (simple; single-process, fine for Render).
sessions: dict[str, str] = {}  # session_token -> github_username

# Shared task store. This process only submits; the TaskWorker in the
# worker process claims and executes through serve.run_task, which
# applies tier derivation, policy, approvals, and audit.
store = CloudTaskStore()

app = FastAPI(title="Kyrex Cloud Web", version="1.0.0")

# ── helpers ────────────────────────────────────────────────────────

def get_session_user(request: Request) -> Optional[str]:
    """Return the GitHub username for this session, or None."""
    token = request.cookies.get("session")
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

@app.get("/auth/login")
def login(request: Request):
    redirect_uri = str(request.base_url) + "auth/callback"
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "read:user",
        "state": secrets.token_hex(16),
    }
    return RedirectResponse(f"https://github.com/login/oauth/authorize?{urlencode(params)}")


@app.get("/auth/callback")
def callback(code: str, request: Request):
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
    task_id = store.submit(
        session_key=user,
        task_text=task_text,
        repo_url=REPO_URL,
        executor_prefix="repo",
        chat_id=user,
    )
    return {"status": "queued", "task_id": task_id, "task": task_text}


@app.get("/api/results")
def list_results(request: Request):
    """Return recent task state from the shared store."""
    require_user(request)
    results_raw = []
    for task in store.list_tasks(limit=50):
        result = task.get("result") or {}
        if not isinstance(result, dict):
            result = {}
        # Prefer the executor's own status when the run finished;
        # otherwise surface the lifecycle status (queued/running/...).
        merged = {
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


# ── WebSocket ──────────────────────────────────────────────────────

@app.websocket("/ws/task")
async def task_ws(websocket: WebSocket):
    """WebSocket that streams live progress for the currently running task.
    Auth via query param ?session= since JS WebSocket can't set cookies."""
    await websocket.accept()

    token = websocket.query_params.get("session")
    user = sessions.get(token) if token else None
    if not user:
        await websocket.send_json({"type": "error", "message": "Not authenticated"})
        await websocket.close()
        return

    # Register this connection as the active WS for progress streaming
    old = active_task.get("ws")
    active_task["ws"] = websocket
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        if active_task.get("ws") is websocket:
            active_task["ws"] = None


# The task runner that used to live here (send_progress / send_result /
# run_task) executed git_workflow.py directly, bypassing the gate. It has
# been removed: execution now happens only in the worker process via
# serve.run_task. Live progress streaming from task_events is a follow-up.


# ── static frontend ────────────────────────────────────────────────

FRONTEND_DIR = WEB_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


# ── entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)