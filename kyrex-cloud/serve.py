#!/usr/bin/env python3
"""
serve.py — Core executor routing and repo alias resolution for Kyrex Cloud.

Constants and pure functions extracted from telegram_bot.py so they can be
imported by other modules (e.g., a future HTTP server) without pulling in
Telegram API dependencies.

This module has no Telegram imports and no dependency on telegram_bot.py.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

# Executor routing — maps a message prefix to a script path relative to SCRIPT_DIR.
# The default executor handles messages with no recognized prefix.
EXECUTORS = {
    "repo": "git_workflow.py",
}
DEFAULT_EXECUTOR = "repo"

# Matches a single-word prefix at the very start of a message followed by ": ".
EXECUTOR_PREFIX_RE = re.compile(r"^(\w+):\s+(.*)")

try:
    REPO_ALIASES = json.loads(os.environ.get("KYREX_REPO_ALIASES", "{}"))
except json.JSONDecodeError:
    REPO_ALIASES = {}


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


# ---------------------------------------------------------------------------
# Host loop — moved from telegram_bot.py. Transport-neutral: nothing here
# imports or knows about Telegram. The adapter injects send/edit callables.
# ---------------------------------------------------------------------------

BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")
SCRIPT_DIR = Path(__file__).resolve().parent

TASK_TIMEOUT = int(os.environ.get("KYREX_TASK_TIMEOUT", "1800"))

APPROVAL_TIMEOUT = int(os.environ.get("KYREX_APPROVAL_TIMEOUT", "600"))

busy_lock = threading.Lock()

# Per-session locking infrastructure.  _get_session_lock returns the
# threading.Lock for a given opaque session key, creating it on first access.
# The transport chooses the key — for telegram_bot.py it's str(chat_id), but
# the module does not hardcode that assumption.
_session_locks: dict[str, threading.Lock] = {}
_session_locks_lock = threading.Lock()


def _get_session_lock(session_key: str) -> threading.Lock:
    with _session_locks_lock:
        if session_key not in _session_locks:
            _session_locks[session_key] = threading.Lock()
        return _session_locks[session_key]


class SessionPendingApprovals(dict):
    """dict keyed by (session_key, message_id).  Supports int-key lookups and
    stores by scanning for backward compatibility with tests that set int keys
    directly on the module-level dict."""

    def get(self, key, default=None):
        # For int keys, scan for the matching tuple key first so the
        # super-class lookup uses the actual dict key.
        if isinstance(key, int):
            for k, v in self.items():
                if isinstance(k, tuple) and k[1] == key:
                    return v
        if key in self:
            return super().get(key)
        return default

    def __contains__(self, key):
        # Scan for int-key aliases first, then try exact match.
        if isinstance(key, int):
            return any(isinstance(k, tuple) and k[1] == key for k in self.keys())
        return super().__contains__(key)

    def pop(self, key, *args):
        # For int keys, scan for the matching tuple key first so the
        # super-class pop uses the actual dict key, not the int alias.
        if isinstance(key, int):
            for k in list(self.keys()):
                if isinstance(k, tuple) and k[1] == key:
                    return super().pop(k)
        if key in self:
            return super().pop(key)
        if args:
            return args[0]
        raise KeyError(key)

    def __setitem__(self, key, value):
        # Normalize int keys to (session_key, message_id) when the value has
        # chat_id, so approvals from different sessions can't collide even if
        # they happen to share the same message_id integer.
        if isinstance(key, int) and isinstance(value, dict) and "chat_id" in value:
            key = (str(value["chat_id"]), key)
        super().__setitem__(key, value)


pending_approvals: SessionPendingApprovals = SessionPendingApprovals()

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


def handle_approval_reply(chat_id, reply_text, reply_to_id=None,
                         session_key=None) -> bool:
    """Route a reply to its pending approval, if any.  session_key is the
    opaque session identifier — for telegram_bot.py it's str(chat_id).  The
    module does not hardcode that assumption: callers inject the key.

    Returns True if the message was consumed as an approval reply, False
    otherwise so the caller may handle it as a normal task.

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
         session AND text plausibly answers → consume.
      Every other message → fall through (return False) so it can be handled
      as a normal task, including messages carrying a reply_to_message.
    """
    if session_key is None:
        session_key = str(chat_id) if chat_id else ""
    reply_to = reply_to_id is not None
    reply_text = (reply_text or "").strip()

    if reply_to:
        pending = pending_approvals.get(reply_to_id)
        if not pending:
            # reply_to doesn't match any pending approval.
            # If the text looks like a tier-1 answer (y/yes/n/no), treat as
            # stale reply and consume.  Anything else falls through so it
            # can be launched as a normal task.
            if reply_text.lower() in ("y", "yes", "n", "no"):
                return True
            return False
        # Verify the pending approval belongs to this session, not a
        # different one whose message_id happened to collide.
        p_session = pending.get("session_key", str(pending.get("chat_id", "")))
        if session_key != p_session:
            return False
        if chat_id != pending.get("chat_id") and chat_id != pending.get("chat_id"):
            return False  # reply from wrong chat — ignore (keep old guard too)
    else:
        # Bare message, no reply_to.  Accept it as an approval reply only if
        # exactly one approval is pending for this session.
        pending_for_session = {
            k: v for k, v in pending_approvals.items()
            if v.get("session_key", str(v.get("chat_id", ""))) == session_key
        }
        if len(pending_for_session) != 1:
            return False
        pending = next(iter(pending_for_session.values()))

    tier = pending["tier"]

    # Plausibility gate: the text must plausibly answer this pending approval.
    if tier == 1:
        if reply_text.lower() not in ("y", "yes", "n", "no"):
            return False  # not a plausible approval answer — fall through
    elif tier == 2:
        if reply_text != (pending["token"] or ""):
            return False  # not a plausible approval answer — fall through

    approved = False
    if tier == 1:
        approved = reply_text.lower() in ("y", "yes")
    elif tier == 2:
        approved = reply_text == (pending["token"] or "")

    decision = "APPROVED" if approved else "DENIED"
    pending["result"] = decision
    pending["event"].set()
    return True


def run_task(chat_id, repo_url, task_text, executor_prefix="repo",
             session_key=None, send=None, edit=None):
    """Host-side task runner. `send(chat_id, text) -> message_id | None` and
    `edit(chat_id, message_id, text)` are injected by the transport, so this
    module stays free of any Telegram dependency.

    session_key is an opaque session identifier acquired from the transport.
    When None (legacy callers), defaults to str(chat_id)."""
    if session_key is None:
        session_key = str(chat_id)
    status_msg_id = None
    progress_lines = []
    last_edit = 0.0

    def maybe_edit():
        nonlocal last_edit
        now = time.monotonic()
        if status_msg_id and now - last_edit > 2.5:  # throttle: Telegram edit rate limits
            last_edit = now
            body = "\n".join(f"  → {p}" for p in progress_lines[-6:])
            edit(chat_id, status_msg_id, f"⏳ Working: {task_text}\n{body}")

    try:
        status_msg_id = send(chat_id, f"⏳ Starting: {task_text}")

        executor_script = EXECUTORS[executor_prefix]
        # stderr gets its own pipe. Merging it into stdout let an unbuffered
        # stderr write land mid-line and corrupt the KYREX_RESULT_JSON line —
        # same rule as the engine: nothing but protocol on a protocol channel.
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / executor_script),
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

                    approval_msg_id = send(chat_id, prompt)
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
                        "session_key": session_key,
                        "tier": tier,
                        "token": token,
                        "result": None,
                    }
                    # Pause the task watchdog while waiting for operator
                    # approval so human think-time doesn't consume the task
                    # budget.
                    watchdog.cancel()
                    got_reply = evt.wait(timeout=APPROVAL_TIMEOUT)
                    if not timed_out.is_set():
                        watchdog = threading.Timer(TASK_TIMEOUT, on_timeout)
                        watchdog.start()
                    entry = pending_approvals.pop(approval_msg_id, None)
                    decision = "APPROVED" if got_reply and entry and entry["result"] == "APPROVED" else "DENIED"

                    if not got_reply:
                        # Update the approval message to show it timed out
                        edit(chat_id, approval_msg_id, prompt + "\n\n⏰ Timed out — denied.")
                    else:
                        edit(chat_id, approval_msg_id,
                                     prompt + f"\n\n→ {decision}")

                    try:
                        proc.stdin.write(f"{decision}\n")
                        proc.stdin.flush()
                    except BrokenPipeError:
                        # Executor already exited — nothing to write.
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
            send(chat_id,
                         f"⚠️ Task exceeded {TASK_TIMEOUT // 60} min and was killed."
                         + (f"\n\nstderr:\n{stderr_tail}" if stderr_tail else ""))
        elif result_json:
            send(chat_id, format_result(result_json))
        else:
            detail = f" ({parse_errors} undecodable protocol line(s))" if parse_errors else ""
            send(chat_id,
                         f"⚠️ Task finished with exit code {proc.returncode} but emitted no "
                         f"parseable result{detail}."
                         + (f"\n\nstderr:\n{stderr_tail}" if stderr_tail else ""))
    except Exception as e:
        print(f"[serve] task failed: {type(e).__name__}: {e}", file=sys.stderr)
        try:
            send(chat_id, f"⚠️ Bot error: {type(e).__name__}: {e}")
        except Exception:
            pass  # the notifier must never be the thing that kills the task
    finally:
        # Release the per-session lock.  Also try to release the global
        # busy_lock for backward compatibility with tests that acquire it
        # directly before calling run_task.
        try:
            _get_session_lock(session_key).release()
        except RuntimeError:
            pass
        try:
            busy_lock.release()
        except RuntimeError:
            pass


def launch(chat_id, repo_url, task_text, executor_prefix="repo",
           session_key=None, send=None, edit=None):
    """Acquire the per-session lock and spawn a run_task thread. Returns True
    if launched, False if busy."""
    if session_key is None:
        session_key = str(chat_id)
    lock = _get_session_lock(session_key)
    if not lock.acquire(blocking=False):
        send(chat_id, "Still working on the previous task — one at a time for now.")
        return False
    threading.Thread(target=run_task, args=(chat_id, repo_url, task_text, executor_prefix),
                     kwargs={"session_key": session_key, "send": send, "edit": edit},
                     daemon=True).start()
    return True

