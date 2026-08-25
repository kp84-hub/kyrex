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
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import audit  # append-only audit log
import bots  # bot registry
import policy  # bot policy evaluation
from paths import DATA_DIR


# ---------------------------------------------------------------------------
# MCP configuration delivery — reads MCP_SERVERS_JSON from env and writes it
# to ~/.kyrex/mcp_servers.json before any executor runs. This is a startup
# operation so credentials stay in the platform's env, never in the image or
# in git. See KX_SERVE_DESIGN.md § MCP configuration.
# ---------------------------------------------------------------------------

MCP_SERVERS_DIR = DATA_DIR
MCP_SERVERS_FILE = MCP_SERVERS_DIR / "mcp_servers.json"

_MCP_WRITTEN = False


def write_mcp_config():
    """Read MCP_SERVERS_JSON from the environment and write it to disk.

    - If the variable is absent, print a line to stderr saying MCP is
      unconfigured and return without writing anything.
    - If the variable is present but not valid JSON, print the parse error
      to stderr and do not write — a malformed config must not produce a
      partially written file.
    - If valid JSON, create ~/.kyrex if needed and write the file.
    """
    global _MCP_WRITTEN
    if _MCP_WRITTEN:
        return
    _MCP_WRITTEN = True

    raw = os.environ.get("MCP_SERVERS_JSON")
    if raw is None or raw.strip() == "":
        print("MCP unconfigured — MCP_SERVERS_JSON not set, zero servers loaded.",
              file=sys.stderr)
        return
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"MCP config parse error: {e}", file=sys.stderr)
        return

    MCP_SERVERS_DIR.mkdir(parents=True, exist_ok=True)
    MCP_SERVERS_FILE.write_text(json.dumps(parsed, indent=2))
    print(f"MCP config written to {MCP_SERVERS_FILE}", file=sys.stderr)


# Executor routing — maps a message prefix to a script path relative to SCRIPT_DIR.
# The default executor handles messages with no recognized prefix.
EXECUTORS = {
    "repo": "git_workflow.py",
    "fs": "fs_executor.py",
    "cal": "cal_executor.py",
}
DEFAULT_EXECUTOR = "repo"

# Matches a single-word prefix at the very start of a message followed by ": ".
EXECUTOR_PREFIX_RE = re.compile(r"^(\w+):\s+(.*)")

# Matches a leading @<botid> prefix followed by whitespace.
# The bot id is alphanumeric plus underscore and hyphen.
BOT_PREFIX_RE = re.compile(r"^@([A-Za-z0-9_-]+)\s+(.*)")


def resolve_bot_prefix(text: str):
    """Parse a leading ``@<botid>`` from *text*.

    Returns ``(bot_id, rest_text)`` if a valid bot prefix is found,
    or ``(None, text)`` if there is no such prefix.

    The bot id must match ``[A-Za-z0-9_-]+`` and be followed by whitespace.
    An ``@`` that appears mid-text or is not followed by a valid bot id
    and whitespace is not treated as a prefix — e.g. ``user@host do something``
    returns ``(None, text)``.
    """
    m = BOT_PREFIX_RE.match(text)
    if m:
        return m.group(1), m.group(2)
    return None, text

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
# Tier derivation — the host derives the operation's tier from the operation
# itself per the executor contract. The executor's self-declared tier is a
# hint the host may raise, but never a value the host acts on unverified.
# ---------------------------------------------------------------------------

# Operations the host recognises. An operation outside this set is denied
# before policy is consulted: "no rule matched" and "I do not know what this
# is" are different failures, and only the second should be immune to a
# permissive wildcard. Executors gain entries here as they gain operations.
KNOWN_OPERATIONS = frozenset({
    "fs.read",
    "fs.write",
    "fs.delete",
    "cal.list",
})


DESTRUCTIVE_VERBS = frozenset({
    "delete", "remove", "trash", "send", "push", "force", "revoke", "drop",
})


def derive_tier(executor_prefix: str, approval: dict) -> int:
    """Derive the operation tier the host will act on.

    Rules, applied in order:
      1. If the executor's declared tier is not 1 or 2, treat it as 2.
      2. If the summary's first word is a destructive verb, return 2
         regardless of what was declared.
      3. Otherwise return the declared tier.
      4. The host may raise a tier but never lower it, so return
         max(normalized_declared, derived).
    """
    declared = approval.get("tier", 2)
    if declared not in (1, 2):
        declared = 2

    summary = approval.get("summary", "")
    first_word = summary.split()[0].lower() if summary.strip() else ""

    if first_word in DESTRUCTIVE_VERBS:
        derived = 2
    else:
        derived = declared

    return max(declared, derived)


# ---------------------------------------------------------------------------
# Bot resolution — maps a session key to the Bot bound to that session.
# ---------------------------------------------------------------------------


def resolve_bot(session_key: str) -> dict | None:
    """Look up a Bot whose id matches *session_key* in the Bot registry.

    Returns the Bot dict if a Bot with that id exists, or ``None`` if no
    Bot is bound to that session key.  Never falls back to a default Bot
    or to another Bot's record.

    If the registry cannot be loaded (corrupt file, I/O error, etc.), a
    message is printed to stderr and ``None`` is returned — a registry
    failure must not block the caller from proceeding unbound.
    """
    try:
        registry = bots.load_bots()
    except Exception as exc:
        print(f"[serve] bot registry load failed: {exc}", file=sys.stderr)
        return None
    return registry.get(session_key)


# ---------------------------------------------------------------------------
# ExecutionContext — carries all resolved state for a single task run.
# ---------------------------------------------------------------------------


@dataclass
class ExecutionContext:
    """Resolved execution context for one task invocation.

    Populated by :func:`build_context` from either a bound Bot or the
    fallback unbound state.  Executors must never read this object — they
    receive only ``rift_path`` as an environment variable.
    """

    session_id: str
    rift_path: str | None = None
    policy: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    bot_id: str = ""


def build_context(session_key: str, executor_prefix: str = "repo") -> ExecutionContext:
    """Build an :class:`ExecutionContext` for a task.

    When a Bot is bound to *session_key*, the context is populated from that
    Bot's registry entry (rift, policy, id).  When the session is unbound
    (no Bot matches, or the registry is corrupt/unloadable), the context
    carries ``rift_path=None``, an empty policy, and ``bot_id`` set to
    *executor_prefix* so the audit trail is still populated with a meaningful
    identifier.

    Never raises.  A registry load failure is handled inside ``resolve_bot``
    and produces an unbound context.
    """
    bot = resolve_bot(session_key)
    if bot is not None:
        return ExecutionContext(
            session_id=session_key,
            rift_path=bot.get("rift"),
            policy=bot.get("policy", {}),
            bot_id=bot.get("id", executor_prefix),
        )
    return ExecutionContext(
        session_id=session_key,
        rift_path=None,
        policy={},
        bot_id=executor_prefix,
    )


# ---------------------------------------------------------------------------
# Host loop — moved from telegram_bot.py. Transport-neutral: nothing here
# imports or knows about Telegram. The adapter injects send/edit callables.
# ---------------------------------------------------------------------------

BASE_BRANCH = os.environ.get("KYREX_TARGET_BASE", "main")
SCRIPT_DIR = Path(__file__).resolve().parent

TASK_TIMEOUT = int(os.environ.get("KYREX_TASK_TIMEOUT", "1800"))

APPROVAL_TIMEOUT = int(os.environ.get("KYREX_APPROVAL_TIMEOUT", "600"))

# Per-session locking. The transport chooses the session key and this module
# treats it as opaque — it is a chat id today, but a forum topic or an HTTP
# session tomorrow, and nothing here should assume otherwise.
#
# One lock per session rather than one globally is an approval-model
# constraint before it is a throughput one: handle_approval_reply resolves a
# bare "y" by finding the single pending approval for a session, and two
# concurrent tasks in one session make that reply ambiguous with no safe
# default.
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()


def session_lock(session_key) -> threading.Lock:
    """The lock for one session, created on first use.

    The dict is never pruned. A lock is a few dozen bytes and the key space is
    the set of sessions that have ever run a task, so this is bounded in
    practice — but it is unbounded in principle, and worth revisiting if K-Bot
    ever serves many short-lived sessions.
    """
    key = str(session_key)
    with _session_locks_guard:
        lock = _session_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _session_locks[key] = lock
        return lock


def session_busy(session_key) -> bool:
    return session_lock(session_key).locked()


def any_session_busy() -> bool:
    with _session_locks_guard:
        return any(lock.locked() for lock in _session_locks.values())


# Keyed by (session_key, message_id). The session component is what stops a
# reply in one session from resolving an approval in another — a message_id
# alone is only unique within a chat.
pending_approvals: dict[tuple, dict] = {}

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
    """Route a Telegram reply to its pending approval, if any.

    Returns True if the message was consumed as an approval reply, False
    otherwise so handle_message proceeds normally.

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
      as a normal task, including messages carrying a reply_to_message.
    """
    skey = str(session_key if session_key is not None else chat_id)
    # The Telegram adapter knows the chat but not which Bot a task was
    # bound to, so a reply to a bot-bound approval arrives with no session
    # key and would otherwise miss. Recover the session from the approval
    # this reply points at - but only among approvals raised in this chat,
    # because a message id is unique within a chat and not across them.
    if reply_to_id is not None and session_key is None:
        for (_sk, _mid), _pending in pending_approvals.items():
            if _mid == reply_to_id and _pending.get("chat_id") == chat_id:
                skey = str(_sk)
                break
    reply_to = reply_to_id is not None
    reply_text = (reply_text or "").strip()

    if reply_to:
        pending = pending_approvals.get((skey, reply_to_id))
        if not pending:
            # reply_to doesn't match any pending approval.
            # If the text looks like a tier-1 answer (y/yes/n/no), treat as
            # stale reply and consume.  Anything else falls through so it
            # can be launched as a normal task.
            if reply_text.lower() in ("y", "yes", "n", "no"):
                return True
            return False
        if chat_id != pending["chat_id"]:
            return False  # reply from wrong chat — ignore
    else:
        # Bare message, no reply_to.  Accept it as an approval reply only if
        # exactly one approval is pending for this chat.
        # Scope to this session only. Scanning every pending approval would
        # let a bare "y" here resolve an approval that belongs elsewhere,
        # which is the exact confusion the session component of the key
        # exists to prevent.
        # With an explicit session key, scope to that session. Without one,
        # the caller is a transport that knows the chat but not which Bot the
        # task was bound to - so scope to the chat instead. Both are narrower
        # than "any pending approval anywhere", which is what must not happen.
        _scope_for_bare_reply = session_key is not None
        pending_for_session = {
            k: v for k, v in pending_approvals.items()
            if (k[0] == skey if _scope_for_bare_reply
                else v.get("chat_id") == chat_id)
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
             send=None, edit=None, session_key=None):
    """Host-side task runner. `send(chat_id, text) -> message_id | None` and
    `edit(chat_id, message_id, text)` are injected by the transport, so this
    module stays free of any Telegram dependency."""
    _skey = str(session_key if session_key is not None else chat_id)
    # Build the execution context once.  When a Bot is bound to this session
    # the context carries its rift, policy, and id.  When unbound the context
    # carries rift_path=None, empty policy, and bot_id set to executor_prefix.
    ctx = build_context(_skey, executor_prefix)
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
        # Executors must not know about Bots — they receive an authorised
        # filesystem root or nothing.  When the context has a rift_path
        # it is delivered as KYREX_FS_ROOT, overriding any inherited value.
        # When there is no rift_path the inherited environment is untouched
        # so today's behaviour is unchanged.
        proc_env = None
        if ctx.rift_path is not None:
            proc_env = os.environ.copy()
            proc_env["KYREX_FS_ROOT"] = ctx.rift_path
        # stderr gets its own pipe. Merging it into stdout let an unbuffered
        # stderr write land mid-line and corrupt the KYREX_RESULT_JSON line —
        # same rule as the engine: nothing but protocol on a protocol channel.
        executor_cmd = [
            sys.executable, str(SCRIPT_DIR / executor_script),
            "--repo-url", repo_url,
            "--base", BASE_BRANCH,
            "--task", task_text,
        ]
        # A Bot bound to a persistent Rift hands that Rift to the repo
        # executor explicitly (--rift) so the workspace is reused and never
        # wiped.  This is the repo executor only; other executors keep their
        # existing unbound behaviour and still receive KYREX_FS_ROOT when bound.
        if executor_prefix == "repo" and ctx.rift_path is not None:
            executor_cmd += ["--rift", ctx.rift_path]
        proc = subprocess.Popen(
            executor_cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
            env=proc_env,
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
        _last_op_info = None  # carries op_id, op, target, decision, tier from operation to approval
        _operation_count = 0
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
                elif line.startswith("KYREX_OPERATION:"):
                    try:
                        op_data = json.loads(line[len("KYREX_OPERATION:"):])
                    except json.JSONDecodeError:
                        parse_errors += 1
                        try:
                            proc.stdin.write("DENY\n")
                            proc.stdin.flush()
                        except BrokenPipeError:
                            pass
                        continue

                    # Strip any tier the executor sent — the host alone
                    # derives the tier from the operation description.
                    executor_tier = op_data.pop("tier", None)
                    op = op_data.get("op", "")
                    target = op_data.get("target", "")
                    summary = op_data.get("summary", "")
                    detail = op_data.get("detail")

                    # Generate a short correlation id so this operation's audit
                    # entry and any subsequent approval share an op_id.
                    _op_id = uuid.uuid4().hex[:8]
                    _last_op_info = {"op_id": _op_id, "op": op, "target": target}

                    # Convert dotted op to colon form for policy matching.
                    # e.g. "fs.read" -> "fs:read".  An op with no dot is
                    # treated as-is (bare word).
                    if "." in op:
                        colon_op = op.replace(".", ":", 1)
                        executor_prefix = op.split(".")[0]
                    else:
                        colon_op = op
                        executor_prefix = op

                    # Derive the host-side tier from the operation
                    # description.  There is no executor-declared tier
                    # (the protocol deliberately omits it), so the host
                    # starts from 0 and raises only when the summary's
                    # first word is a destructive verb — exactly the same
                    # verb list used by derive_tier, not new logic.
                    first_word = summary.split()[0].lower() if summary.strip() else ""
                    if first_word in DESTRUCTIVE_VERBS:
                        derived_tier = 2
                    else:
                        derived_tier = 0

                    # An operation the host cannot classify is denied here,
                    # before policy: a permissive wildcard must not be able
                    # to authorise something we do not recognise.
                    if op not in KNOWN_OPERATIONS:
                        try:
                            proc.stdin.write("DENY\n")
                            proc.stdin.flush()
                        except BrokenPipeError:
                            pass
                        try:
                            audit.log(
                                bot_id=ctx.bot_id,
                                operation=op or "(missing)",
                                tier="unknown",
                                decision="deny",
                                outcome="blocked",
                                detail={"target": target,
                                        "reason": "unrecognised operation"},
                                op_id=_op_id,
                            )
                        except Exception as exc:
                            print(f"[serve] audit log failure: {exc}",
                                  file=sys.stderr)
                        continue

                    # Evaluate policy.
                    policy_info = None
                    try:
                        pol_decision = policy.evaluate(
                            ctx.policy, colon_op, derived_tier,
                        )
                        tier = policy.enforce(pol_decision)
                        policy_info = {
                            "matched_rule": pol_decision.get("matched_rule"),
                            "reason": pol_decision.get("reason"),
                        }
                    except Exception as exc:
                        print(
                            f"[serve] policy evaluation failed: {exc}",
                            file=sys.stderr,
                        )
                        tier = derived_tier

                    # Determine host decision from the effective tier.
                    if isinstance(tier, str) and tier == "deny":
                        host_decision = "DENY"
                        audit_decision = "deny"
                        audit_outcome = "blocked"
                    elif tier == 0:
                        host_decision = "ALLOW"
                        audit_decision = "allow"
                        audit_outcome = "auto"
                    else:
                        # tier 1 or 2 — needs human approval.
                        host_decision = "APPROVE"
                        audit_decision = "approval_required"
                        audit_outcome = "auto"

                    # Record the decision against this operation so that, when
                    # the executor's KYREX_RESULT_JSON arrives, the follow-up
                    # entry can reuse the same op/ op_id/ decision as the
                    # operation's own audit entry.
                    _operation_count += 1
                    _last_op_info = {
                        "op_id": _op_id,
                        "op": op,
                        "target": target,
                        "decision": audit_decision,
                        "tier": f"tier{tier if isinstance(tier, int) else 'deny'}",
                    }

                    # Write exactly one audit entry before the decision
                    # reaches the executor.  Never blocks the decision.
                    try:
                        audit_bot_id = ctx.bot_id
                        audit_detail: dict = {
                            "target": target,
                            "policy_rule": (
                                policy_info.get("matched_rule")
                                if policy_info
                                else None
                            ),
                        }
                        if policy_info:
                            audit_detail["reason"] = policy_info.get("reason")
                        if executor_tier is not None:
                            audit_detail["ignored_executor_tier"] = executor_tier
                        if ctx.rift_path is None:
                            audit_detail["note"] = "session unbound"
                        audit.log(
                            bot_id=audit_bot_id,
                            operation=op,  # dotted form e.g. "fs.read"
                            tier=f"tier{tier if isinstance(tier, int) else 'deny'}",
                            decision=audit_decision,
                            outcome=audit_outcome,
                            detail=audit_detail,
                            op_id=_op_id,
                        )
                    except Exception as exc:
                        print(
                            f"[serve] audit log failure: {exc}",
                            file=sys.stderr,
                        )

                    # Write the decision to the executor's stdin (one line).
                    try:
                        proc.stdin.write(f"{host_decision}\n")
                        proc.stdin.flush()
                    except BrokenPipeError:
                        # Executor already exited — nothing to write.
                        pass

                elif line.startswith("KYREX_APPROVAL:"):
                    try:
                        approval = json.loads(line[len("KYREX_APPROVAL:"):])
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue
                    derived_tier = derive_tier(executor_prefix, approval)
                    summary = approval.get("summary", "")
                    token = approval.get("token", "")
                    detail = approval.get("detail", "")

                    # Policy evaluation — never blocks the approval.
                    # The context carries the Bot's policy when bound, or
                    # an empty dict when unbound.
                    bot_policy = ctx.policy
                    first_word = summary.split()[0].lower() if summary.strip() else ""
                    operation = f"{executor_prefix}:{first_word}"
                    policy_info = None
                    try:
                        pol_decision = policy.evaluate(bot_policy, operation, derived_tier)
                        tier = policy.enforce(pol_decision)
                        policy_info = {
                            "matched_rule": pol_decision.get("matched_rule"),
                            "reason": pol_decision.get("reason"),
                        }
                    except Exception as exc:
                        print(f"[serve] policy evaluation failed: {exc}", file=sys.stderr)
                        tier = derived_tier

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
                    pending_approvals[(_skey, approval_msg_id)] = {
                        "event": evt,
                        "chat_id": chat_id,
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
                    entry = pending_approvals.pop((_skey, approval_msg_id), None)
                    decision = "APPROVED" if got_reply and entry and entry["result"] == "APPROVED" else "DENIED"

                    # Record every approval decision to the audit log.
                    # A failure to write must never block the decision from
                    # reaching the executor.
                    if not got_reply:
                        audit_decision = "timeout"
                    else:
                        audit_decision = "approved" if decision == "APPROVED" else "denied"
                    try:
                        audit_bot_id = ctx.bot_id
                        # Carry forward the operation correlation id and the
                        # original op/ target so both audit entries name the
                        # same operation the same way.
                        # An executor that has not migrated to the operation
                        # protocol raises an approval with nothing preceding it.
                        # Fall back to the approval's own summary rather than
                        # recording an entry with no operation name.
                        _op_id = _last_op_info["op_id"] if _last_op_info else ""
                        _op = _last_op_info["op"] if _last_op_info else summary
                        _target = _last_op_info["target"] if _last_op_info else ""
                        audit_detail: dict = {}
                        if policy_info is not None:
                            audit_detail["policy"] = policy_info
                        if ctx.rift_path is None:
                            audit_detail["note"] = "session unbound"
                        audit_detail["target"] = _target
                        audit.log(
                            bot_id=audit_bot_id,
                            operation=_op,  # original op e.g. "fs.read"
                            tier=f"tier{tier}",
                            decision=audit_decision,
                            outcome=result_json.get("status", "pending") if result_json else "pending",
                            detail=audit_detail,
                            op_id=_op_id,
                        )
                    except Exception as exc:
                        print(f"[serve] audit log failure: {exc}", file=sys.stderr)

                    # Update tracking so the follow-up entry (written when
                    # KYREX_RESULT_JSON arrives) uses the approval decision.
                    if _last_op_info is not None:
                        _last_op_info["decision"] = audit_decision

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
                        continue

                    # Follow-up audit entry recording the executor's actual
                    # outcome.  Written only for operations that were approved
                    # by human decision ("approved"), not auto-allowed or
                    # denied.  Failure to write this entry must never affect
                    # the task's own result reporting.
                    # Any operation that actually ran gets an outcome, not
                    # just the ones a human approved. A denied operation
                    # never ran, so there is nothing to report about it.
                    if (_last_op_info is not None
                            and _last_op_info["decision"] in ("approved", "allow")):
                        try:
                            _outcome_detail = {"target": _last_op_info.get("target", "")}
                            if _operation_count > 1:
                                _outcome_detail["note"] = "result attributed to the last operation"
                            audit.log(
                                bot_id=ctx.bot_id,
                                operation=_last_op_info.get("op", ""),
                                tier=_last_op_info.get("tier", ""),
                                decision=_last_op_info["decision"],
                                outcome=result_json.get("status", "unknown"),
                                detail=_outcome_detail,
                                op_id=_last_op_info["op_id"],
                            )
                        except Exception as exc:
                            print(f"[serve] audit outcome log failure: {exc}", file=sys.stderr)
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
        session_lock(_skey).release()


def launch(chat_id, repo_url, task_text, executor_prefix="repo",
           send=None, edit=None, session_key=None):
    """Acquire this session's lock and spawn a run_task thread. Returns True
    if launched, False if that session is already busy."""
    skey = str(session_key if session_key is not None else chat_id)
    if not session_lock(skey).acquire(blocking=False):
        send(chat_id, "Still working on the previous task — one at a time for now.")
        return False
    threading.Thread(target=run_task, args=(chat_id, repo_url, task_text, executor_prefix),
                     kwargs={"send": send, "edit": edit, "session_key": skey},
                     daemon=True).start()
    return True

