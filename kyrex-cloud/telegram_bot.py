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
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
DEFAULT_REPO_URL = os.environ.get("KYREX_TARGET_REPO_URL", "https://github.com/kp84-hub/kyrex.git")
BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")
API_BASE = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
API = f"{API_BASE}/bot{BOT_TOKEN}"

try:
    REPO_ALIASES = json.loads(os.environ.get("KYREX_REPO_ALIASES", "{}"))
except json.JSONDecodeError:
    REPO_ALIASES = {}

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "bot_offset.txt"

busy_lock = threading.Lock()

STATUS_LABELS = {
    "pr_opened": "✅ PR opened",
    "pushed_pr_skipped": "✅ Pushed (no PR — see reason below)",
    "pushed_no_pr": "✅ Pushed (PR skipped by request)",
    "no_changes": "ℹ️ No changes were needed",
    "review_flagged": "⚠️ Self-review flagged a mismatch — branch pushed, PR held back",
    "agent_failed": "❌ Agent did not complete",
    "git_failed": "❌ Git operation failed",
    "error": "❌ Unexpected error",
}

# In-memory store of pending file content (not paths) awaiting a follow-up
# instruction. Key: chat_id, Value: list of {"filename": str, "content": str}.
# Content is read as text at download time so it travels inside the task
# string, not as a local path that would be unreachable from per-task
# workspaces created by git_workflow.py.
pending_docs: dict[int, list[dict]] = {}


def api_call(method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read())


def send_message(chat_id, text):
    resp = api_call("sendMessage", chat_id=chat_id, text=text[:4000])
    return resp.get("result", {}).get("message_id")


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
    commands after a restart."""
    try:
        resp = api_call("getUpdates", timeout=1)
        updates = resp.get("result", [])
        if updates:
            return updates[-1]["update_id"] + 1
    except Exception:
        pass
    return 0


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


def format_result(result: dict) -> str:
    status = result.get("status", "unknown")
    final_response = result.get("final_response", "").strip()

    # Nothing changed on disk means there's no git/review/PR outcome to
    # report at all — this was just a question. Read like a normal chatbot
    # answer, not a task-status label with nothing behind it.
    if status == "no_changes":
        return final_response[-3500:] if final_response else "(no response)"

    lines = [STATUS_LABELS.get(status, f"Status: {status}")]

    review = result.get("review")
    if review and review.get("available"):
        verdict = "matches task" if review.get("matches_task") else "possible mismatch"
        lines.append(f"🔍 Self-review: {verdict} — {review.get('reasoning', '')}")

    pr = result.get("pull_request")
    if pr and pr.get("url"):
        lines.append(f"🔗 {pr['url']}")
    elif pr and pr.get("skipped"):
        lines.append(f"(PR not opened: {pr.get('reason', 'unknown reason')})")

    final_response = result.get("final_response", "").strip()
    if final_response:
        lines.append("")
        lines.append(final_response[-800:])

    errors = result.get("errors") or []
    if errors and status in ("agent_failed", "git_failed", "error"):
        lines.append("")
        lines.append(f"Error: {errors[-1][:500]}")

    return "\n".join(lines)


def run_task(chat_id, repo_url, task_text):
    status_msg_id = send_message(chat_id, f"⏳ Starting: {task_text}")
    progress_lines = []
    last_edit = 0.0

    def maybe_edit():
        nonlocal last_edit
        now = time.monotonic()
        if status_msg_id and now - last_edit > 2.5:  # throttle: Telegram edit rate limits
            last_edit = now
            body = "\n".join(f"  → {p}" for p in progress_lines[-6:])
            edit_message(chat_id, status_msg_id, f"⏳ Working: {task_text}\n{body}")

    try:
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "git_workflow.py"),
             "--repo-url", repo_url,
             "--base", BASE_BRANCH,
             "--task", task_text],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        result_json = None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("KYREX_PROGRESS:"):
                try:
                    note = json.loads(line[len("KYREX_PROGRESS:"):])
                    progress_lines.append(", ".join(f"{k}: {v}" for k, v in note.items()))
                    maybe_edit()
                except json.JSONDecodeError:
                    pass
            elif line.startswith("KYREX_RESULT_JSON:"):
                try:
                    result_json = json.loads(line[len("KYREX_RESULT_JSON:"):])
                except json.JSONDecodeError:
                    pass
        proc.wait(timeout=30)

        if result_json:
            send_message(chat_id, format_result(result_json))
        else:
            send_message(chat_id, "⚠️ Task finished but no result could be parsed — check Railway logs.")
    except subprocess.TimeoutExpired:
        send_message(chat_id, "⚠️ Task timed out.")
    except Exception as e:
        send_message(chat_id, f"⚠️ Bot error: {type(e).__name__}: {e}")
    finally:
        busy_lock.release()


def launch(chat_id, repo_url, task_text):
    """Acquire the busy lock and spawn a run_task thread. Returns True if
    launched, False if busy."""
    if not busy_lock.acquire(blocking=False):
        send_message(chat_id, "Still working on the previous task — one at a time for now.")
        return False
    threading.Thread(target=run_task, args=(chat_id, repo_url, task_text), daemon=True).start()
    return True


def handle_message(msg):
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    if chat_id != ALLOWED_CHAT_ID:
        return  # silently ignore anyone else — no reply, no acknowledgment

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
            repo_url, clean_instruction = resolve_repo(caption)
            task_text = build_task_with_attachments(clean_instruction, [{"filename": file_name, "content": content}])
            launch(chat_id, repo_url, task_text)
        else:
            # Document without caption = store pending, wait for instruction.
            pending_docs.setdefault(chat_id, []).append({"filename": file_name, "content": content})
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
        send_message(chat_id, "Kyrex Cloud Agent is " + ("busy on a task." if busy_lock.locked() else "idle."))
        return
    if stripped == "/repos":
        lines = [f"Default: {DEFAULT_REPO_URL}"]
        lines += [f"{alias}: {url}" for alias, url in REPO_ALIASES.items()]
        send_message(chat_id, "\n".join(lines))
        return

    repo_url, task_text = resolve_repo(text)
    if not task_text:
        return

    # Check for pending file content to embed in the task
    pending = pending_docs.pop(chat_id, None)
    if pending:
        task_text = build_task_with_attachments(task_text, pending)

    launch(chat_id, repo_url, task_text)


def main():
    offset = load_offset()
    if offset is None:
        offset = catch_up_offset()
    save_offset(offset)
    print(f"[telegram_bot] listening, chat_id={ALLOWED_CHAT_ID}, "
          f"default_repo={DEFAULT_REPO_URL}, aliases={list(REPO_ALIASES.keys())}, offset={offset}")

    while True:
        try:
            resp = api_call("getUpdates", offset=offset, timeout=30)
        except (urllib.error.URLError, TimeoutError) as e:
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
