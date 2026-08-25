#!/usr/bin/env python3
"""
kyrex-cloud/web/backend/main.py — Kyrex Cloud Web trigger (Milestone 1).

FastAPI application providing:
  - "Sign in with GitHub" OAuth (single allowed username via env var)
  - POST /api/task — queue a plain-text task in the persistent CloudTaskStore
  - GET  /api/task/{task_id} — task status + result
  - GET  /api/tasks — list tasks (optionally filtered by bot/status)
  - POST /api/task/{task_id}/cancel — request cancellation
  - POST /api/task/{task_id}/respond — answer an approval (operator decision)
  - GET  /api/task/{task_id}/events — durable event stream (progress/approval)
  - WebSocket /ws/task — live progress + result for the session's latest task

Execution is delegated to the :class:`TaskWorker` (started by ``worker.py``),
which is the single execution path.  This backend only *submits* and *reads*
tasks from the shared ``CloudTaskStore`` (SQLite under ``DATA_DIR``), so it
survives restarts and is identical across the web and Telegram surfaces.

Reuses kyrex-cloud/git_workflow.py as a subprocess exactly the same way
kyrex-cloud/telegram_bot.py does — nothing in kyrex_engine/ is modified.
"""

import asyncio
import json
import os
import secrets
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

import serve
from task_store import CloudTaskStore, TERMINAL_STATUSES

# ── paths ──────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent          # web/backend/
WEB_DIR = SCRIPT_DIR.parent                            # web/
KYREX_CLOUD_DIR = WEB_DIR.parent                       # kyrex-cloud/

# ── env ────────────────────────────────────────────────────────────
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]
ALLOWED_USERNAME = os.environ["WEB_ALLOWED_GITHUB_USERNAME"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO_URL = os.environ.get("KYREX_TARGET_REPO_URL", "https://github.com/kp84-hub/kyrex.git")
BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")
SESSION_SECRET = os.environ.get("WEB_SESSION_SECRET", secrets.token_hex(32))

# ── store (shared with the worker via DATA_DIR) ───────────────────
_store = None


def get_store() -> CloudTaskStore:
    """Return the process-wide CloudTaskStore, creating it on first use."""
    global _store
    if _store is None:
        _store = CloudTaskStore()
    return _store


# In-memory session store (simple; single-process, fine for Render).
sessions: dict[str, str] = {}  # session_token -> github_username

# Latest task_id per web session, so the WebSocket can stream it.
latest_task_per_session: dict[str, str] = {}

app = FastAPI(title="Kyrex Cloud Web", version="1.0.0")


# ── helpers ────────────────────────────────────────────────────────

def get_session_token(request: Request) -> Optional[str]:
    return request.cookies.get("session")


def get_session_user(request: Request) -> Optional[str]:
    """Return the GitHub username for this session, or None."""
    token = get_session_token(request)
    if token and token in sessions:
        return sessions[token]
    return None


def require_user(request: Request) -> str:
    user = get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def serialize_task(task: dict) -> dict:
    """Return a safe public view of a stored task."""
    return {
        "task_id": task.get("task_id"),
        "session_key": task.get("session_key"),
        "bot_id": task.get("bot_id"),
        "status": task.get("status"),
        "executor_prefix": task.get("executor_prefix"),
        "task_text": task.get("task_text"),
        "result": task.get("result"),
        "error": task.get("error"),
        "created_at": task.get("created_at"),
        "started_at": task.get("started_at"),
        "finished_at": task.get("finished_at"),
    }


def format_result_summary(task: dict) -> dict:
    """Build a short summary dict suitable for the past-results list."""
    result = task.get("result") or {}
    status = task.get("status") or "unknown"
    summary = {
        "task": task.get("task_text", ""),
        "status": status,
        "branch": result.get("branch", ""),
        "started_at": task.get("started_at", ""),
        "finished_at": task.get("finished_at", ""),
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
    token = get_session_token(request)
    if token and token in sessions:
        del sessions[token]
    response = RedirectResponse(url="/")
    response.set_cookie(key="session", value="", httponly=True, max_age=0)
    return response


# ── task routes (CloudTaskStore-backed) ────────────────────────────

@app.post("/api/task")
async def accept_task(request: Request):
    """Queue a plain-text task. The worker is the only thing that runs it."""
    user = require_user(request)
    body = await request.json()
    task_text = (body.get("task") or "").strip()
    if not task_text:
        raise HTTPException(status_code=400, detail="Task text is required")
    if len(task_text) > 2000:
        raise HTTPException(status_code=400, detail="Task text too long (max 2000 chars)")

    exec_prefix, rest, err_word = serve.resolve_executor(task_text)
    if err_word:
        valid = ", ".join(sorted(serve.EXECUTORS.keys()))
        raise HTTPException(
            status_code=400,
            detail=f"Unknown executor prefix '{err_word}'. Valid prefixes: {valid}",
        )

    store = get_store()
    try:
        task_id = store.submit(
            session_key=user,
            task_text=rest,
            repo_url=REPO_URL,
            executor_prefix=exec_prefix,
            bot_id=user,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not queue task: {exc}")

    latest_task_per_session[get_session_token(request)] = task_id
    return {"status": "queued", "task_id": task_id, "task": task_text}


@app.get("/api/task/{task_id}")
def get_task(task_id: str, request: Request):
    require_user(request)
    store = get_store()
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return serialize_task(task)


@app.get("/api/tasks")
def list_tasks(request: Request, bot: Optional[str] = None, status: Optional[str] = None):
    """List tasks, optionally filtered by bot (bot_id) or status."""
    require_user(request)
    store = get_store()
    tasks = store.list_tasks(status=status, bot_id=bot, limit=50)
    return {"tasks": [serialize_task(t) for t in tasks]}


@app.post("/api/task/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    require_user(request)
    store = get_store()
    ok = store.cancel(task_id)
    return {"cancelled": ok}


@app.post("/api/task/{task_id}/respond")
async def respond_task(task_id: str, request: Request):
    """Answer an operator approval for *task_id* (e.g. "y", "n", or a T2 token)."""
    require_user(request)
    body = await request.json()
    text = body.get("decision") or body.get("text") or ""
    store = get_store()
    ok = store.respond(task_id, text)
    return {"responded": ok}


@app.get("/api/task/{task_id}/events")
def task_events(task_id: str, request: Request, after: int = 0):
    """Return the durable event stream for a task (progress/approval/result)."""
    require_user(request)
    store = get_store()
    if store.get(task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"events": store.get_events(task_id, after_event_id=after)}


@app.get("/api/results")
def list_results(request: Request):
    """Return a list of past task results (newest first)."""
    require_user(request)
    store = get_store()
    tasks = store.list_tasks(limit=50)
    return {"results": [format_result_summary(t) for t in tasks]}


# ── WebSocket (store-backed live progress) ────────────────────────

@app.websocket("/ws/task")
async def task_ws(websocket: WebSocket):
    """WebSocket that streams live progress/result for the session's latest task.

    Auth via query param ?session= since JS WebSocket can't set cookies.
    The backend polls the shared CloudTaskStore and relays events to the
    browser; when the task reaches a terminal state the final result is sent.
    """
    await websocket.accept()

    token = websocket.query_params.get("session")
    user = sessions.get(token) if token else None
    if not user:
        await websocket.send_json({"type": "error", "message": "Not authenticated"})
        await websocket.close()
        return

    last_id = 0
    sent_result = False
    try:
        while True:
            task_id = latest_task_per_session.get(token)
            if task_id:
                store = get_store()
                events = store.get_events(task_id, after_event_id=last_id)
                progress_lines = []
                for ev in events:
                    last_id = ev["event_id"]
                    if ev["type"] == "progress" and ev.get("payload"):
                        progress_lines.append(ev["payload"])
                if progress_lines:
                    await websocket.send_json({
                        "type": "progress",
                        "note": progress_lines[-1],
                        "lines": progress_lines[-10:],
                    })
                task = store.get(task_id)
                if task and task["status"] in TERMINAL_STATUSES and not sent_result:
                    await websocket.send_json({
                        "type": "result",
                        "data": format_result_summary(task),
                        "raw": task.get("result") or {},
                    })
                    sent_result = True
                    break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ── static frontend ────────────────────────────────────────────────

FRONTEND_DIR = WEB_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


# ── entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
