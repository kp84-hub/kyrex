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

# Hard ceiling on a single git_workflow.py run. Without this the bot blocks on
# proc.stdout forever and never releases busy_lock — a hung agent means a dead
# bot until manual restart.
TASK_TIMEOUT = int(os.environ.get("KYREX_TASK_TIMEOUT", "1800"))

# An uncaptioned document waits this long for a follow-up instruction before
# being dropped. Without expiry, a file sent and forgotten silently attaches
# itself to an unrelated message hours later.
PENDING_DOC_TTL = int(os.environ.get("KYREX_PENDING_DOC_TTL", "1800"))

# Maximum time the bot waits for an operator to reply to a KYREX_APPROVAL prompt
# before writing DENIED to the executor's stdin. Enforced host-side per the
# executor contract — an executor cannot be trusted to time out its own request.
APPROVAL_TIMEOUT = int(os.environ.get("KYREX_APPROVAL_TIMEOUT", "600"))

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

# Pending approval requests awaiting operator reply.
# Key: message_id of the approval prompt sent to the chat.
# Value: {"event": threading.Event(), "chat_id": int, "tier": int,
#         "token": str | None, "result": str | None}
# The run_task thread waits on event; handle_message signals it when a matching
# reply arrives. timeout writes DENIED and clears the entry.
pending_approvals: dict[int, dict] = {}


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


def handle_approval_reply(msg: dict) -> bool:
    """Route a Telegram reply to its pending approval, if any.

    Returns True if the message was consumed as an approval reply (matched a
    pending approval OR is a stale reply-to referencing a no-longer-pending
    approval), False otherwise so handle_message proceeds normally.

    Two matching modes:
    - reply_to_message present: match by message_id. If the referenced
      approval no longer exists, the reply is silently consumed (stale reply).
    - no reply_to_message: match by chat when *exactly one* approval is
      pending for this chat. 0 or >1 pending → return False so the message
      is treated as a normal task launch.

    Tier 1: reply matching /^y(es)?$/i → APPROVED, everything else → DENIED.
    Tier 2: reply matching *token* exactly (after strip) → APPROVED, else DENIED.
    Both deny on a host-enforced timeout (APPROVAL_TIMEOUT).
    """
    chat_id = msg.get("chat", {}).get("id")
    reply_to = msg.get("reply_to_message")

    if reply_to:
        reply_to_id = reply_to.get("message_id")
        pending = pending_approvals.get(reply_to_id)
        if not pending:
            # Stale reply-to referencing a no-longer-pending approval —
            # consume it silently rather than launching it as a new task.
            return True
        if chat_id != pending["chat_id"]:
            return True  # reply from wrong chat — consume silently
    else:
        # No reply-to: match by chat when exactly one approval is pending.
        matches = [e for e in pending_approvals.values() if e["chat_id"] == chat_id]
        if len(matches) != 1:
            return False  # 0 or >1 pending — proceed as normal message
        pending = matches[0]

    reply_text = (msg.get("text") or "").strip()
    tier = pending["tier"]
    approved = False

    if tier == 1:
        approved = reply_text.lower() in ("y", "yes")
    elif tier == 2:
        approved = reply_text == (pending["token"] or "")
    else:
        approved = False  # unknown tier — deny

    decision = "APPROVED" if approved else "DENIED"
    pending["result"] = decision
    pending["event"].set()
    return True


def run_task(chat_id, repo_url, task_text):
    status_msg_id = None
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
        status_msg_id = send_message(chat_id, f"⏳ Starting: {task_text}")

        # stderr gets its own pipe. Merging it into stdout let an unbuffered
        # stderr write land mid-line and corrupt the KYREX_RESULT_JSON line —
        # same rule as the engine: nothing but protocol on a protocol channel.
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "git_workflow.py"),
             "--repo-url", repo_url,
             "--base", BASE_BRANCH,
             "--task", task_text],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

        # Drained in a thread so a chatty child can't fill the stderr pipe
        # buffer and deadlock against our stdout read.
        stderr_buf = []

        def drain_stderr():
            for line in proc.stderr:
                stderr_buf.append(line)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        # Real deadline. The old proc.wait(timeout=30) only ran *after* stdout
        # hit EOF, so it could never fire — a hung agent hung the bot forever.
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            proc.kill()

        watchdog = threading.Timer(TASK_TIMEOUT, on_timeout)
        watchdog.start()

        result_json = None
        parse_errors = 0
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith("KYREX_PROGRESS:"):
                    try:
                        note = json.loads(line[len("KYREX_PROGRESS:"):])
                        progress_lines.append(", ".join(f"{k}: {v}" for k, v in note.items()))
                        maybe_edit()
                    except json.JSONDecodeError:
                        parse_errors += 1
                elif line.startswith("KYREX_APPROVAL:"):
                    try:
                        approval = json.loads(line[len("KYREX_APPROVAL:"):])
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue
                    tier = approval.get("tier", 1)
                    summary = approval.get("summary", "")
                    token = approval.get("token", "")
                    detail = approval.get("detail", "")

                    if tier == 2:
                        prompt = (
                            f"⚠️  T2: {summary}"
                            + (f"\n{detail}" if detail else "")
                            + f"\n\nReply exactly:  {token}"
                            f"\n(timeout: {APPROVAL_TIMEOUT // 60} min)"
                        )
                    else:
                        prompt = (
                            f"⚠️  T1: {summary}"
                            + (f"\n{detail}" if detail else "")
                            + "\n\nReply with y (approve) or n (deny)"
                            f"\n(timeout: {APPROVAL_TIMEOUT // 60} min)"
                        )

                    approval_msg_id = send_message(chat_id, prompt)
                    if approval_msg_id is None:
                        # Can't reach the operator — deny the operation so the
                        # executor doesn't hang forever waiting on stdin.
                        try:
                            proc.stdin.write("DENIED\n")
                            proc.stdin.flush()
                        except BrokenPipeError:
                            pass
                        continue

                    evt = threading.Event()
                    pending_approvals[approval_msg_id] = {
                        "event": evt,
                        "chat_id": chat_id,
                        "tier": tier,
                        "token": token,
                        "result": None,
                    }
                    # Pause the task watchdog during approval wait so operator
                    # think-time doesn't count against TASK_TIMEOUT.
                    watchdog.cancel()
                    try:
                        got_reply = evt.wait(timeout=APPROVAL_TIMEOUT)
                    finally:
                        entry = pending_approvals.pop(approval_msg_id, None)
                        # Restart the watchdog with a fresh budget after the
                        # approval wait finishes, regardless of outcome.
                        watchdog = threading.Timer(TASK_TIMEOUT, on_timeout)
                        watchdog.daemon = True
                        watchdog.start()
                    decision = "APPROVED" if got_reply and entry and entry["result"] == "APPROVED" else "DENIED"

                    if not got_reply:
                        # Update the approval message to show it timed out
                        edit_message(chat_id, approval_msg_id, prompt + "\n\n⏰ Timed out — denied.")
                    else:
                        edit_message(chat_id, approval_msg_id,
                                     prompt + f"\n\n→ {decision}")

                    try:
                        proc.stdin.write(f"{decision}\n")
                        proc.stdin.flush()
                    except BrokenPipeError:
                        # Executor already exited (e.g. watchdog killed it
                        # before we could respond) — nothing to write to.
                        pass

                elif line.startswith("KYREX_RESULT_JSON:"):
                    try:
                        result_json = json.loads(line[len("KYREX_RESULT_JSON:"):])
                    except json.JSONDecodeError as e:
                        parse_errors += 1
                        print(f"[telegram_bot] result JSON undecodable: {e}\n{line[:800]}",
                              file=sys.stderr)
            proc.wait()
        finally:
            watchdog.cancel()

        stderr_thread.join(timeout=5)
        stderr_tail = "".join(stderr_buf).strip()[-600:]

        if timed_out.is_set():
            send_message(chat_id,
                         f"⚠️ Task exceeded {TASK_TIMEOUT // 60} min and was killed."
                         + (f"\n\nstderr:\n{stderr_tail}" if stderr_tail else ""))
        elif result_json:
            send_message(chat_id, format_result(result_json))
        else:
            detail = f" ({parse_errors} undecodable protocol line(s))" if parse_errors else ""
            send_message(chat_id,
                         f"⚠️ Task finished with exit code {proc.returncode} but emitted no "
                         f"parseable result{detail}."
                         + (f"\n\nstderr:\n{stderr_tail}" if stderr_tail else ""))
    except Exception as e:
        print(f"[telegram_bot] task failed: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            send_message(chat_id, f"⚠️ Bot error: {type(e).__name__}: {e}")
        except Exception:
            pass  # the notifier must never be the thing that kills the task
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
            repo_url, clean_instruction = resolve_repo(caption)
            task_text = build_task_with_attachments(clean_instruction, [{"filename": file_name, "content": content}])
            launch(chat_id, repo_url, task_text)
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
    pending = take_pending_docs(chat_id)
    if pending:
        task_text = build_task_with_attachments(task_text, pending)

    launch(chat_id, repo_url, task_text)


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
