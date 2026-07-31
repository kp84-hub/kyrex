#!/usr/bin/env python3
"""
telegram_bot.py — Kyrex Cloud Agent, Phase 3 trigger.

Long-polls the Telegram Bot API for messages from one allowed chat, treats
each message as a task, and runs it through git_workflow.py (fresh clone
mode — no local checkout exists on a cloud host) against a target repo.
Replies on Telegram with an ack, then the final status + PR link (or error)
when it's done.

Security model:
  - Only TELEGRAM_ALLOWED_CHAT_ID is ever acted on. Every other chat is
    silently ignored (no reply at all) so the bot can't be discovered and
    abused as an open remote-code-execution trigger by anyone who finds it.
  - One task at a time — a second message while one is running gets a
    "still busy" reply instead of a second concurrent git_workflow.py run
    against the same repo.
  - On every startup, any backlog of pending Telegram updates is discarded
    rather than replayed. This matters most if the host's filesystem is
    ephemeral (offset file doesn't survive a redeploy/restart) — without
    this, old test messages or crashed-mid-task commands could silently
    re-fire after every restart.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
REPO_URL = os.environ.get("KYREX_TARGET_REPO_URL", "https://github.com/kp84-hub/kyrex.git")
BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
API = f"{API_BASE}/bot{BOT_TOKEN}"

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "bot_offset.txt"

busy_lock = threading.Lock()


def api_call(method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read())


def send_message(chat_id, text):
    api_call("sendMessage", chat_id=chat_id, text=text[:4000])  # Telegram's message cap


def load_offset():
    if STATE_FILE.exists():
        return int(STATE_FILE.read_text().strip())
    return None


def save_offset(offset):
    STATE_FILE.write_text(str(offset))


def catch_up_offset():
    """Discard any pending backlog on startup rather than replaying old
    commands after a restart."""
    try:
        resp = api_call("getUpdates", timeout=1)
        updates = resp.get("result", [])
        if updates:
            return updates[-1]["update_id"] + 1
    except Exception:
        pass
    return 0


def run_task(chat_id, task_text):
    send_message(chat_id, f"⏳ Starting: {task_text}")
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "git_workflow.py"),
             "--repo-url", REPO_URL,
             "--base", BASE_BRANCH,
             "--task", task_text],
            capture_output=True, text=True, timeout=1800,
        )
        output = (proc.stdout + proc.stderr).strip()
        send_message(chat_id, f"Done.\n\n{output[-3500:]}")
    except subprocess.TimeoutExpired:
        send_message(chat_id, "⚠️ Task timed out after 30 minutes.")
    except Exception as e:
        send_message(chat_id, f"⚠️ Bot error: {type(e).__name__}: {e}")
    finally:
        busy_lock.release()


def handle_message(msg):
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    if chat_id != ALLOWED_CHAT_ID:
        return  # silently ignore anyone else — no reply, no acknowledgment
    if not text:
        return
    if text.strip() == "/status":
        send_message(chat_id, "Kyrex Cloud Agent is " + ("busy on a task." if busy_lock.locked() else "idle."))
        return
    if not busy_lock.acquire(blocking=False):
        send_message(chat_id, "Still working on the previous task — one at a time for now.")
        return
    threading.Thread(target=run_task, args=(chat_id, text), daemon=True).start()


def main():
    offset = load_offset()
    if offset is None:
        offset = catch_up_offset()
    save_offset(offset)
    print(f"[telegram_bot] listening, chat_id={ALLOWED_CHAT_ID}, repo={REPO_URL}, offset={offset}")

    while True:
        try:
            resp = api_call("getUpdates", offset=offset, timeout=30)
        except (urllib.error.URLError, TimeoutError, TimeoutError) as e:
            print(f"[telegram_bot] poll error: {e}, retrying in 5s")
            time.sleep(5)
            continue
        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            save_offset(offset)
            msg = update.get("message")
            if msg:
                handle_message(msg)


if __name__ == "__main__":
    main()
