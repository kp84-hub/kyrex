#!/usr/bin/env python3
"""
telegram_bot.py — Kyrex Cloud Agent, Telegram adapter.

Adapter that provides Telegram-specific send/edit message implementations
and imports the host loop from serve.py. Exposes the same module-level
interface so existing tests pass unmodified.

See serve.py for the host loop, approval protocol, executor routing, etc.
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

# Import the host loop — all shared state, config, and logic lives here.
import serve as _serve

# Re-export shared names so tests and callers can access them via telegram_bot.
from serve import (
    busy_lock,
    pending_approvals,
    pending_docs,
    resolve_executor,
    resolve_repo,
    EXECUTORS,
    DEFAULT_EXECUTOR,
    EXECUTOR_PREFIX_RE,
    DEFAULT_REPO_URL,
    STATUS_LABELS,
    ATTACHMENT_TEMPLATE,
    build_task_with_attachments,
    format_result,
    handle_approval_reply,
    REPO_ALIASES,
    take_pending_docs,
    TASK_TIMEOUT,
    PENDING_DOC_TTL,
    APPROVAL_TIMEOUT,
    SCRIPT_DIR,
)

# ── Telegram configuration ────────────────────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
API = f"{API_BASE}/bot{BOT_TOKEN}"

STATE_FILE = _serve.SCRIPT_DIR / "bot_offset.txt"


# ── Telegram API helpers ──────────────────────────────────────────────

def api_call(method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read())


# ── Message callbacks (injected into serve) ───────────────────────────

def send_message(chat_id, text):
    """Never raises. A failed sendMessage (429, transient network) used to
    propagate out of run_task before its try/finally and strand busy_lock
    held forever, making the bot answer 'still working' to everything."""
    try:
        resp = api_call("sendMessage", chat_id=chat_id, text=text[:4000])
        return resp.get("result", {}).get("message_id")
    except Exception as e:
        print(f"[telegram_bot] sendMessage failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def edit_message(chat_id, message_id, text):
    try:
        api_call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4000])
    except urllib.error.HTTPError:
        pass


# Inject callbacks into serve so serve.run_task / serve.launch use them.
_serve.send_message = send_message
_serve.edit_message = edit_message


# ── Document download (Telegram-specific) ────────────────────────────

def download_file_content(file_id: str, file_name: str) -> tuple[str | None, str | None]:
    """Download a Telegram document by file_id and decode as UTF-8 text."""
    try:
        resp = api_call("getFile", file_id=file_id)
        result = resp.get("result", {})
        file_path = result.get("file_path")
        if not file_path:
            return None, "could not resolve file path from Telegram"
        file_url = f"{API_BASE}/file/bot{BOT_TOKEN}/{file_path}"
        filename = file_path.split("/")[-1] or file_name
        req = urllib.request.Request(file_url)
        with urllib.request.urlopen(req, timeout=30) as f:
            raw = f.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, (
                f"File '{filename}' does not appear to be valid UTF-8 text — "
                f"only text/code files are supported. Please paste the content "
                f"directly in your message instead."
            )
        return text, filename
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ── Offset persistence ────────────────────────────────────────────────

def load_offset():
    if STATE_FILE.exists():
        return int(STATE_FILE.read_text().strip())
    return None


def save_offset(offset):
    STATE_FILE.write_text(str(offset))


def catch_up_offset():
    """Discard any pending backlog on startup rather than replaying old
    commands after a restart. Returns None if the backlog could not be
    determined — returning 0 here meant 'send me everything', i.e. the exact
    replay this function exists to prevent."""
    for attempt in range(3):
        try:
            resp = api_call("getUpdates", timeout=1)
            updates = resp.get("result", [])
            if not updates:
                return 0
            return updates[-1]["update_id"] + 1
        except Exception as e:
            print(f"[telegram_bot] catch-up attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(2)
    return None


# ── Wrappers (inject callbacks into serve before delegating) ──────────

def run_task(chat_id, repo_url, task_text, executor_prefix="repo"):
    """Wrapper that injects current send/edit callbacks into serve, then
    delegates to serve.run_task. This ensures module-level monkey-patching
    (as done by tests) propagates to the host loop."""
    _serve.send_message = send_message
    _serve.edit_message = edit_message
    return _serve.run_task(chat_id, repo_url, task_text, executor_prefix)


def launch(chat_id, repo_url, task_text, executor_prefix="repo"):
    """Wrapper that injects current send/edit callbacks into serve, then
    delegates to serve.launch."""
    _serve.send_message = send_message
    _serve.edit_message = edit_message
    return _serve.launch(chat_id, repo_url, task_text, executor_prefix)


# ── Message routing handler ──────────────────────────────────────────

def handle_message(msg):
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    if chat_id != ALLOWED_CHAT_ID:
        return

    if _serve.handle_approval_reply(msg):
        return

    doc = msg.get("document")
    if doc:
        file_id = doc.get("file_id")
        file_name = doc.get("file_name", "unknown_file")
        caption = msg.get("caption", "")
        if not file_id:
            return
        content, err = download_file_content(file_id, file_name)
        if err:
            send_message(chat_id, err)
            return
        if caption:
            exec_prefix, rest_text, err_word = _serve.resolve_executor(caption)
            if err_word:
                valid = ", ".join(sorted(_serve.EXECUTORS.keys()))
                send_message(chat_id, f"Unknown executor prefix '{err_word}'. Valid prefixes: {valid}")
                return
            repo_url, clean_instruction = _serve.resolve_repo(rest_text)
            task_text = _serve.build_task_with_attachments(
                clean_instruction, [{"filename": file_name, "content": content}])
            launch(chat_id, repo_url, task_text, executor_prefix=exec_prefix)
        else:
            _serve.pending_docs.setdefault(chat_id, []).append(
                {"filename": file_name, "content": content, "ts": time.time()})
            filenames = ", ".join(d["filename"] for d in _serve.pending_docs[chat_id])
            send_message(chat_id,
                         f"📎 Got {len(_serve.pending_docs[chat_id])} file(s): {filenames}. "
                         f"Now send your instructions / task description.")
        return

    if not text:
        return

    stripped = text.strip()
    if stripped == "/status":
        send_message(chat_id, "Kyrex Cloud Agent is "
                     + ("busy on a task." if _serve.busy_lock.locked() else "idle."))
        return
    if stripped == "/repos":
        lines = [f"Default: {_serve.DEFAULT_REPO_URL}"]
        lines += [f"{alias}: {url}" for alias, url in _serve.REPO_ALIASES.items()]
        send_message(chat_id, "\n".join(lines))
        return

    exec_prefix, rest_text, err_word = _serve.resolve_executor(text)
    if err_word:
        valid = ", ".join(sorted(_serve.EXECUTORS.keys()))
        send_message(chat_id, f"Unknown executor prefix '{err_word}'. Valid prefixes: {valid}")
        return

    repo_url, task_text = _serve.resolve_repo(rest_text)
    if not task_text:
        return

    pending = _serve.take_pending_docs(chat_id)
    if pending:
        task_text = _serve.build_task_with_attachments(task_text, pending)

    launch(chat_id, repo_url, task_text, executor_prefix=exec_prefix)


# ── Poll loop ─────────────────────────────────────────────────────────

def main():
    offset = load_offset()
    discard_first_batch = False
    if offset is None:
        offset = catch_up_offset()
        if offset is None:
            offset = 0
            discard_first_batch = True
    save_offset(offset)
    print(f"[telegram_bot] listening, chat_id={ALLOWED_CHAT_ID}, "
          f"default_repo={_serve.DEFAULT_REPO_URL}, aliases={list(_serve.REPO_ALIASES.keys())}, "
          f"offset={offset}, task_timeout={_serve.TASK_TIMEOUT}s, "
          f"discard_first_batch={discard_first_batch}")

    while True:
        try:
            resp = api_call("getUpdates", offset=offset, timeout=30)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"[telegram_bot] poll error: {e}, retrying in 5s", file=sys.stderr)
            time.sleep(5)
            continue
        except Exception as e:
            print(f"[telegram_bot] unexpected poll error: {type(e).__name__}: {e}",
                  file=sys.stderr)
            time.sleep(5)
            continue

        updates = resp.get("result", [])
        if discard_first_batch:
            discard_first_batch = False
            if updates:
                offset = updates[-1]["update_id"] + 1
                save_offset(offset)
                print(f"[telegram_bot] discarded {len(updates)} stale update(s)")
                continue

        for update in updates:
            offset = update["update_id"] + 1
            save_offset(offset)
            msg = update.get("message")
            if msg:
                try:
                    handle_message(msg)
                except Exception as e:
                    print(f"[telegram_bot] handler error: {type(e).__name__}: {e}",
                          file=sys.stderr)


if __name__ == "__main__":
    main()