#!/usr/bin/env python3
"""
telegram_bot.py — Kyrex Cloud Agent, Phase 3 trigger + Phase 4 polish.

Long-polls the Telegram Bot API for messages from one allowed chat, treats
each message as a task, and runs it through git_workflow.py (fresh clone
mode — no local checkout exists on a cloud host) against a target repo.

Phase 4 additions over the original Phase 3 version:
  - Repo aliasing: a message can start with "alias: task text" to target a
    different repo than the default (KYREX_REPO_ALIASES env var, JSON map of
    alias -> repo URL). Plain messages with no alias prefix behave exactly
    as before — this is additive, not a breaking change.
  - Live progress: instead of silence between "Starting" and "Done", the
    initial message is edited in place as git_workflow.py streams
    KYREX_PROGRESS lines (throttled to avoid hammering Telegram's edit rate).
  - Clean final replies: git_workflow.py now emits one KYREX_RESULT_JSON line
    at the end; the bot parses that specifically and formats a human-readable
    summary (status, self-review verdict, PR link) instead of dumping raw
    stdout.

Security model (unchanged from Phase 3):
  - Only TELEGRAM_ALLOWED_CHAT_ID is ever acted on. Every other chat is
    silently ignored — no reply, no acknowledgment.
  - One task at a time — a second message while one is running gets a
    "still busy" reply instead of a second concurrent git_workflow.py run.
  - On every startup, any backlog of pending Telegram updates is discarded
    rather than replayed, since the offset file may not survive a redeploy.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

from serve import (
    APPROVAL_TIMEOUT,
    STATUS_LABELS,
    TASK_TIMEOUT,
    busy_lock,
    format_result,
    pending_approvals,
    DEFAULT_EXECUTOR,
    EXECUTOR_PREFIX_RE,
    EXECUTORS,
    REPO_ALIASES,
    resolve_executor,
)
import serve
import urllib.error
from pathlib import Path

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
DEFAULT_REPO_URL = os.environ.get("KYREX_TARGET_REPO_URL", "https://github.com/kp84-hub/kyrex.git")
BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
API = f"{API_BASE}/bot{BOT_TOKEN}"


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "bot_offset.txt"

# Hard ceiling on a single git_workflow.py run. Without this the bot blocks on
# proc.stdout forever and never releases busy_lock — a hung agent means a dead
# bot until manual restart.


# An uncaptioned document waits this long for a follow-up instruction before
# being dropped. Without expiry, a file sent and forgotten silently attaches
# itself to an unrelated message hours later.
PENDING_DOC_TTL = int(os.environ.get("KYREX_PENDING_DOC_TTL", "1800"))

# Maximum time the bot waits for an operator to reply to a KYREX_APPROVAL prompt
# before writing DENIED to the executor's stdin. Enforced host-side per the
# executor contract — an executor cannot be trusted to time out its own request.






# In-memory store of pending file content (not paths) awaiting a follow-up
# instruction. Key: chat_id, Value: list of {"filename": str, "content": str}.
# Content is read as text at download time so it travels inside the task
# string, not as a local path that would be unreachable from per-task
# workspaces created by git_workflow.py.
pending_docs: dict[int, list[dict]] = {}

# Pending approval requests awaiting operator reply.
# Key: message_id of the approval prompt sent to the chat.
# Value: {"event": threading.Event(), "chat_id": int, "tier": int,
#         "token": str | None, "result": str | None}
# The run_task thread waits on event; handle_message signals it when a matching
# reply arrives. timeout writes DENIED and clears the entry.



def take_pending_docs(chat_id: int) -> list[dict]:
    """Pop pending docs for a chat, discarding any older than PENDING_DOC_TTL."""
    docs = pending_docs.pop(chat_id, None) or []
    now = time.time()
    return [d for d in docs if now - d.get("ts", 0) <= PENDING_DOC_TTL]


def api_call(method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read())


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
        pass  # e.g. edited-too-fast or identical-content — not worth surfacing to the user


def download_file_content(file_id: str, file_name: str) -> tuple[str | None, str | None]:
    """Download a Telegram document by file_id and decode as UTF-8 text.
    Returns (content, filename) on success, (None, error_message) on failure.
    Non-UTF-8 binaries get a descriptive error — not a crash."""
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
                return 0  # nothing pending; 0 is genuinely safe here
            return updates[-1]["update_id"] + 1
        except Exception as e:
            print(f"[telegram_bot] catch-up attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(2)
    return None


def resolve_repo(text: str):
    """'alias: task text' -> (repo_url, task_text). No recognized alias
    prefix -> (default repo, whole text unchanged) — fully backward compatible
    with plain messages from before repo aliasing existed."""
    if ":" in text:
        prefix, rest = text.split(":", 1)
        alias = prefix.strip()
        if alias in REPO_ALIASES:
            return REPO_ALIASES[alias], rest.strip()
    return DEFAULT_REPO_URL, text.strip()


ATTACHMENT_TEMPLATE = """\
Attached file (name: {filename}):
-----BEGIN ATTACHED FILE CONTENT-----
{content}
-----END ATTACHED FILE CONTENT-----
Use the attached content EXACTLY as given, verbatim, character for character — do not paraphrase, reformat, summarize, or 'improve' it."""


def build_task_with_attachments(instruction: str, docs: list[dict]) -> str:
    """Build the full task text by appending each pending document's content
    verbatim. Content is read at download time and stored as strings, so it
    travels inside the task text itself — no local path references that would
    be unreachable from the isolated per-task workspace in git_workflow.py."""
    parts = [instruction.strip()]
    for d in docs:
        parts.append(ATTACHMENT_TEMPLATE.format(filename=d["filename"], content=d["content"]))
    return "\n".join(parts)










# --- Telegram adapters over the transport-neutral host loop in serve.py ----
# These keep the original signatures so callers (and tests that patch
# send_message/edit_message on this module) are unaffected by the extraction.


def handle_approval_reply(msg: dict) -> bool:
    reply_to = msg.get("reply_to_message") or {}
    chat_id = msg.get("chat", {}).get("id")
    return serve.handle_approval_reply(
        chat_id,
        msg.get("text") or "",
        reply_to.get("message_id"),
        session_key=str(chat_id) if chat_id is not None else None,
    )


def run_task(chat_id, repo_url, task_text, executor_prefix="repo"):
    return serve.run_task(chat_id, repo_url, task_text, executor_prefix,
                          session_key=str(chat_id),
                          send=lambda c, t: send_message(c, t),
                          edit=lambda c, m, t: edit_message(c, m, t))


def launch(chat_id, repo_url, task_text, executor_prefix="repo"):
    return serve.launch(chat_id, repo_url, task_text, executor_prefix,
                        session_key=str(chat_id),
                        send=lambda c, t: send_message(c, t),
                        edit=lambda c, m, t: edit_message(c, m, t))


def handle_message(msg):
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    if chat_id != ALLOWED_CHAT_ID:
        return  # silently ignore anyone else — no reply, no acknowledgment

    # --- Reply to a pending approval? ---
    # Check this before the document logic so approval replies work even if
    # the operator attaches something via a reply (unusual but correct).
    if handle_approval_reply(msg):
        return

    # --- Document received ---
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
            # Document with a caption = caption IS the instruction. Run
            # immediately with the file content embedded in the task text.
            exec_prefix, rest_text, err_word = resolve_executor(caption)
            if err_word:
                valid = ", ".join(sorted(EXECUTORS.keys()))
                send_message(chat_id, f"Unknown executor prefix '{err_word}'. Valid prefixes: {valid}")
                return
            repo_url, clean_instruction = resolve_repo(rest_text)
            task_text = build_task_with_attachments(clean_instruction, [{"filename": file_name, "content": content}])
            launch(chat_id, repo_url, task_text, executor_prefix=exec_prefix)
        else:
            # Document without caption = store pending, wait for instruction.
            pending_docs.setdefault(chat_id, []).append(
                {"filename": file_name, "content": content, "ts": time.time()})
            filenames = ", ".join(d["filename"] for d in pending_docs[chat_id])
            send_message(chat_id,
                         f"📎 Got {len(pending_docs[chat_id])} file(s): {filenames}. "
                         f"Now send your instructions / task description.")
        return

    # --- No document, no text -> silently ignore ---
    if not text:
        return

    stripped = text.strip()
    if stripped == "/status":
        send_message(chat_id, "Kyrex Cloud Agent is " + ("busy on a task." if serve._get_session_lock(str(chat_id)).locked() else "idle."))
        return
    if stripped == "/repos":
        lines = [f"Default: {DEFAULT_REPO_URL}"]
        lines += [f"{alias}: {url}" for alias, url in REPO_ALIASES.items()]
        send_message(chat_id, "\n".join(lines))
        return

    exec_prefix, rest_text, err_word = resolve_executor(text)
    if err_word:
        valid = ", ".join(sorted(EXECUTORS.keys()))
        send_message(chat_id, f"Unknown executor prefix '{err_word}'. Valid prefixes: {valid}")
        return

    repo_url, task_text = resolve_repo(rest_text)
    if not task_text:
        return

    # Check for pending file content to embed in the task
    pending = take_pending_docs(chat_id)
    if pending:
        task_text = build_task_with_attachments(task_text, pending)

    launch(chat_id, repo_url, task_text, executor_prefix=exec_prefix)


def main():
    offset = load_offset()
    discard_first_batch = False
    if offset is None:
        offset = catch_up_offset()
        if offset is None:
            # Couldn't reach Telegram to measure the backlog. Poll from 0 but
            # throw the first batch away rather than executing stale commands.
            offset = 0
            discard_first_batch = True
    save_offset(offset)
    print(f"[telegram_bot] listening, chat_id={ALLOWED_CHAT_ID}, "
          f"default_repo={DEFAULT_REPO_URL}, aliases={list(REPO_ALIASES.keys())}, "
          f"offset={offset}, task_timeout={TASK_TIMEOUT}s, "
          f"discard_first_batch={discard_first_batch}")

    while True:
        try:
            resp = api_call("getUpdates", offset=offset, timeout=30)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"[telegram_bot] poll error: {e}, retrying in 5s", file=sys.stderr)
            time.sleep(5)
            continue
        except Exception as e:
            # Never let an unexpected error kill the poll loop — the process
            # would stay alive and simply stop responding.
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
