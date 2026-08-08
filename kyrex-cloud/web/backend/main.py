#!/usr/bin/env python3
"""
kyrex-cloud/web/backend/main.py — Kyrex Cloud Web trigger.

FastAPI application providing:
  - "Sign in with GitHub" OAuth (single allowed username via env var)
  - POST /api/task — accept a plain-text task and run git_workflow.py
  - WebSocket /ws/task — live-stream KYREX_PROGRESS lines, then final result
  - One task at a time (busy lock, same principle as telegram_bot.py)
  - List of past results via /api/results

Reuses kyrex-cloud/git_workflow.py as a subprocess exactly the same way
kyrex-cloud/telegram_bot.py does (parsing KYREX_PROGRESS / KYREX_RESULT_JSON
lines from stdout).  Nothing in kyrex-cloud/ or kyrex_engine/ is modified.
"""

import asyncio
import json
import os
import secrets
import subprocess
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

# ── env ────────────────────────────────────────────────────────────
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]
ALLOWED_USERNAME = os.environ["WEB_ALLOWED_GITHUB_USERNAME"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO_URL = os.environ.get("KYREX_TARGET_REPO_URL", "https://github.com/kp84-hub/kyrex.git")
BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")
SESSION_SECRET = os.environ.get("WEB_SESSION_SECRET", secrets.token_hex(32))

# ── globals ────────────────────────────────────────────────────────
busy_lock = threading.Lock()
active_task: dict = {"task": None, "ws": None}  # single active WebSocket

# In-memory session store (simple; single-process, fine for Render).
sessions: dict[str, str] = {}  # session_token -> github_username

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
    """Accept a plain-text task description and launch git_workflow.py."""
    require_user(request)
    body = await request.json()
    task_text = (body.get("task") or "").strip()
    if not task_text:
        raise HTTPException(status_code=400, detail="Task text is required")
    if len(task_text) > 2000:
        raise HTTPException(status_code=400, detail="Task text too long (max 2000 chars)")

    if not busy_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="A task is already running. Wait for it to complete.")

    # Launch in a background thread
    threading.Thread(
        target=run_task,
        args=(task_text,),
        daemon=True,
    ).start()

    return {"status": "started", "task": task_text}


@app.get("/api/results")
def list_results(request: Request):
    """Return a list of past task results."""
    require_user(request)
    results_raw = []
    if RESULTS_DIR.exists():
        for f in sorted(RESULTS_DIR.glob("*.json"), reverse=True)[:50]:
            try:
                data = json.loads(f.read_text())
                results_raw.append(format_result_summary(data))
            except Exception:
                pass
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


# ── task runner ────────────────────────────────────────────────────

async def send_progress(msg: dict):
    """Send a progress JSON message to the active WebSocket, if any."""
    ws = active_task.get("ws")
    if ws:
        try:
            await ws.send_json({"type": "progress", **msg})
        except Exception:
            pass


async def send_result(result: dict):
    """Send the final result to the active WebSocket, if any."""
    ws = active_task.get("ws")
    if ws:
        try:
            await ws.send_json({"type": "result", "data": format_result_summary(result), "raw": result})
        except Exception:
            pass


def run_task(task_text: str):
    """Run git_workflow.py as a subprocess (same pattern as telegram_bot.py),
    streaming progress to the active WebSocket and saving the result.

    Runs synchronously in a background thread.  Calls asyncio.run() to
    dispatch WebSocket sends from this thread.
    """
    active_task["task"] = task_text
    progress_lines = []

    try:
        proc = subprocess.Popen(
            [sys.executable, str(GIT_WORKFLOW),
             "--repo-url", REPO_URL,
             "--base", BASE_BRANCH,
             "--task", task_text,
             "--token", GITHUB_TOKEN],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        result_json = None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("KYREX_PROGRESS:"):
                try:
                    note = json.loads(line[len("KYREX_PROGRESS:"):])
                    progress_lines.append(note)
                    asyncio.run(send_progress({"note": note, "lines": progress_lines[-10:]}))
                except json.JSONDecodeError:
                    pass
            elif line.startswith("KYREX_RESULT_JSON:"):
                try:
                    result_json = json.loads(line[len("KYREX_RESULT_JSON:"):])
                except json.JSONDecodeError:
                    pass
        proc.wait(timeout=300)

        if result_json:
            asyncio.run(send_result(result_json))
        else:
            asyncio.run(send_progress({"error": "Task finished but no result could be parsed."}))

    except subprocess.TimeoutExpired:
        asyncio.run(send_progress({"error": "Task timed out."}))
    except Exception as e:
        asyncio.run(send_progress({"error": f"Task error: {type(e).__name__}: {e}"}))
    finally:
        busy_lock.release()
        active_task["task"] = None
        # Give the WebSocket a moment to read the final result, then close
        time.sleep(1)
        ws = active_task.get("ws")
        if ws:
            try:
                asyncio.run(ws.close())
            except Exception:
                pass
            active_task["ws"] = None


# ── static frontend ────────────────────────────────────────────────

FRONTEND_DIR = WEB_DIR / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


# ── entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)