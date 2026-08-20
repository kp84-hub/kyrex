#!/usr/bin/env python3
"""
serve.py — Kyrex Cloud Agent host loop, no Telegram dependencies.

Provides run_task, launch, busy_lock, pending_approvals, handle_approval_reply,
resolve_executor, and EXECUTORS. Takes callbacks for sending and editing
messages rather than importing Telegram helpers.

Usage (from an adapter, not directly):
    import serve
    serve.send_message = my_send_fn
    serve.edit_message = my_edit_fn
    serve.launch(chat_id, repo_url, task_text)
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Configuration (env-based, shared across adapters) ──────────────────
DEFAULT_REPO_URL = os.environ.get("KYREX_TARGET_REPO_URL",
                                  "https://github.com/kp84-hub/kyrex.git")
BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")

try:
    REPO_ALIASES = json.loads(os.environ.get("KYREX_REPO_ALIASES", "{}"))
except json.JSONDecodeError:
    REPO_ALIASES = {}

SCRIPT_DIR = Path(__file__).resolve().parent

# Hard ceiling on a single executor run.
TASK_TIMEOUT = int(os.environ.get("KYREX_TASK_TIMEOUT", "1800"))

# An uncaptioned document waits this long for a follow-up instruction.
PENDING_DOC_TTL = int(os.environ.get("KYREX_PENDING_DOC_TTL", "1800"))

# Maximum time the host waits for an operator to reply to a KYREX_APPROVAL
# prompt before writing DENIED.
APPROVAL_TIMEOUT = int(os.environ.get("KYREX_APPROVAL_TIMEOUT", "600"))

# ── Executor routing ───────────────────────────────────────────────────
EXECUTORS = {
    "repo": "git_workflow.py",
}
DEFAULT_EXECUTOR = "repo"

EXECUTOR_PREFIX_RE = re.compile(r"^(\w+):\s+(.*)")

# ── Shared state ───────────────────────────────────────────────────────
busy_lock = threading.Lock()

# In-memory store of pending file content awaiting a follow-up instruction.
# Key: chat_id, Value: list of {"filename": str, "content": str, "ts": float}.
pending_docs: dict[int, list[dict]] = {}

# Pending approval requests awaiting operator reply.
# Key: message_id of the approval prompt.
# Value: {"event": threading.Event(), "chat_id": int, "tier": int,
#         "token": str | None, "result": str | None}
pending_approvals: dict[int, dict] = {}

# ── Message callbacks (overridden by adapters) ─────────────────────────
def _not_configured(*args, **kwargs):
    raise RuntimeError("serve: send_message/edit_message not configured. "
                       "An adapter must set these callbacks.")

send_message = _not_configured
edit_message = _not_configured

# ── Labels ─────────────────────────────────────────────────────────────
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

ATTACHMENT_TEMPLATE = """\
Attached file (name: {filename}):
-----BEGIN ATTACHED FILE CONTENT-----
{content}
-----END ATTACHED FILE CONTENT-----
Use the attached content EXACTLY as given, verbatim, character for character — do not paraphrase, reformat, summarize, or 'improve' it."""


# ── Helpers ────────────────────────────────────────────────────────────

def take_pending_docs(chat_id: int) -> list[dict]:
    """Pop pending docs for a chat, discarding any older than PENDING_DOC_TTL."""
    docs = pending_docs.pop(chat_id, None) or []
    now = time.time()
    return [d for d in docs if now - d.get("ts", 0) <= PENDING_DOC_TTL]


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


def resolve_executor(text: str):
    """Parse a leading '<prefix>: ' from task text for executor routing.

    Returns (executor_prefix, task_text, error_word) where:
      - executor_prefix is a key in EXECUTORS, or DEFAULT_EXECUTOR on no match
      - task_text is the text with prefix stripped (or whole text on no match)
      - error_word is None unless an unknown prefix was detected, in which
        case it holds the unknown word and executor_prefix is None

    Known executor prefixes (EXECUTORS) are routed to their script.
    Repo aliases (REPO_ALIASES) are NOT consumed here — they fall through to
    DEFAULT_EXECUTOR so the alias prefix is preserved for resolve_repo inside
    the repo executor's command builder.
    Unknown prefixes that aren't aliases either are rejected.
    Text with no prefix match at all routes to DEFAULT_EXECUTOR."""
    m = EXECUTOR_PREFIX_RE.match(text)
    if m:
        prefix = m.group(1).lower()
        rest = m.group(2)
        if prefix in EXECUTORS:
            return prefix, rest, None
        # Repo aliases pass through to default executor with full text intact
        # so resolve_repo can strip the alias inside build_command.
        if prefix in REPO_ALIASES:
            return DEFAULT_EXECUTOR, text, None
        return None, None, prefix  # unknown prefix → rejection
    return DEFAULT_EXECUTOR, text, None


def build_task_with_attachments(instruction: str, docs: list[dict]) -> str:
    """Build the full task text by appending each pending document's content
    verbatim. Content is read at download time and stored as strings, so it
    travels inside the task text itself — no local path references that would
    be unreachable from the isolated per-task workspace in the executor."""
    parts = [instruction.strip()]
    for d in docs:
        parts.append(ATTACHMENT_TEMPLATE.format(filename=d["filename"], content=d["content"]))
    return "\n".join(parts)


def format_result(result: dict) -> str:
    status = result.get("status", "unknown")
    final_response = result.get("final_response", "").strip()

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


# ── Approval handler ──────────────────────────────────────────────────

def handle_approval_reply(msg: dict) -> bool:
    """Route a reply to its pending approval, if any.

    Returns True if the message was consumed as an approval reply, False
    otherwise so the caller proceeds normally.

    A message is consumed as an approval reply only if its text plausibly
    answers the pending approval:
      - Tier 1: exactly y, yes, n, or no (case-insensitive).
      - Tier 2: the exact token (case-sensitive, after strip).

    Matching strategies:
      1. reply_to_message matches a known pending approval AND text plausibly
         answers → consume.
      2. reply_to_message matches nothing in pending_approvals AND text looks
         like a tier-1 answer (y/yes/n/no) → stale reply, consume.
      3. bare message (no reply_to) with exactly one pending approval for this
         chat AND text plausibly answers → consume.
      Every other message → fall through (return False) so it can be handled
      as a normal task.
    """
    chat_id = msg.get("chat", {}).get("id")
    reply_to = msg.get("reply_to_message")
    reply_text = (msg.get("text") or "").strip()

    if reply_to:
        reply_to_id = reply_to.get("message_id")
        pending = pending_approvals.get(reply_to_id)
        if not pending:
            if reply_text.lower() in ("y", "yes", "n", "no"):
                return True
            return False
        if chat_id != pending["chat_id"]:
            return False
    else:
        pending_for_chat = {k: v for k, v in pending_approvals.items()
                           if v["chat_id"] == chat_id}
        if len(pending_for_chat) != 1:
            return False
        pending = next(iter(pending_for_chat.values()))

    tier = pending["tier"]

    if tier == 1:
        if reply_text.lower() not in ("y", "yes", "n", "no"):
            return False
    elif tier == 2:
        if reply_text != (pending["token"] or ""):
            return False

    approved = False
    if tier == 1:
        approved = reply_text.lower() in ("y", "yes")
    elif tier == 2:
        approved = reply_text == (pending["token"] or "")

    decision = "APPROVED" if approved else "DENIED"
    pending["result"] = decision
    pending["event"].set()
    return True


# ── Task execution ────────────────────────────────────────────────────

def run_task(chat_id, repo_url, task_text, executor_prefix="repo"):
    status_msg_id = None
    progress_lines = []
    last_edit = 0.0
    _send = send_message
    _edit = edit_message

    def maybe_edit():
        nonlocal last_edit
        now = time.monotonic()
        if status_msg_id and now - last_edit > 2.5:
            last_edit = now
            body = "\n".join(f"  → {p}" for p in progress_lines[-6:])
            _edit(chat_id, status_msg_id, f"⏳ Working: {task_text}\n{body}")

    try:
        status_msg_id = _send(chat_id, f"⏳ Starting: {task_text}")

        executor_script = EXECUTORS[executor_prefix]
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / executor_script),
             "--repo-url", repo_url,
             "--base", BASE_BRANCH,
             "--task", task_text],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

        stderr_buf = []

        def drain_stderr():
            for line in proc.stderr:
                stderr_buf.append(line)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

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

                    approval_msg_id = _send(chat_id, prompt)
                    if approval_msg_id is None:
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
                    watchdog.cancel()
                    got_reply = evt.wait(timeout=APPROVAL_TIMEOUT)
                    if not timed_out.is_set():
                        watchdog = threading.Timer(TASK_TIMEOUT, on_timeout)
                        watchdog.start()
                    entry = pending_approvals.pop(approval_msg_id, None)
                    decision = "APPROVED" if got_reply and entry and entry["result"] == "APPROVED" else "DENIED"

                    if not got_reply:
                        _edit(chat_id, approval_msg_id, prompt + "\n\n⏰ Timed out — denied.")
                    else:
                        _edit(chat_id, approval_msg_id,
                                     prompt + f"\n\n→ {decision}")

                    try:
                        proc.stdin.write(f"{decision}\n")
                        proc.stdin.flush()
                    except BrokenPipeError:
                        pass

                elif line.startswith("KYREX_RESULT_JSON:"):
                    try:
                        result_json = json.loads(line[len("KYREX_RESULT_JSON:"):])
                    except json.JSONDecodeError as e:
                        parse_errors += 1
                        print(f"[serve] result JSON undecodable: {e}\n{line[:800]}",
                              file=sys.stderr)
            proc.wait()
        finally:
            watchdog.cancel()

        stderr_thread.join(timeout=5)
        stderr_tail = "".join(stderr_buf).strip()[-600:]

        if timed_out.is_set():
            _send(chat_id,
                         f"⚠️ Task exceeded {TASK_TIMEOUT // 60} min and was killed."
                         + (f"\n\nstderr:\n{stderr_tail}" if stderr_tail else ""))
        elif result_json:
            _send(chat_id, format_result(result_json))
        else:
            detail = f" ({parse_errors} undecodable protocol line(s))" if parse_errors else ""
            _send(chat_id,
                         f"⚠️ Task finished with exit code {proc.returncode} but emitted no "
                         f"parseable result{detail}."
                         + (f"\n\nstderr:\n{stderr_tail}" if stderr_tail else ""))
    except Exception as e:
        print(f"[serve] task failed: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            _send(chat_id, f"⚠️ Bot error: {type(e).__name__}: {e}")
        except Exception:
            pass
    finally:
        busy_lock.release()


def launch(chat_id, repo_url, task_text, executor_prefix="repo"):
    """Acquire the busy lock and spawn a run_task thread. Returns True if
    launched, False if busy."""
    _send = send_message
    if not busy_lock.acquire(blocking=False):
        _send(chat_id, "Still working on the previous task — one at a time for now.")
        return False
    threading.Thread(target=run_task, args=(chat_id, repo_url, task_text, executor_prefix),
                     daemon=True).start()
    return True