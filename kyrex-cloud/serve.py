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
from paths import DATA_DIR, data_dir
from git_workflow import is_allowlisted_external_repo, is_own_repo, scoped_token_for


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
    "browser": "browser_operator.py",
}
DEFAULT_EXECUTOR = "repo"

# Matches a single-word prefix at the very start of a message followed by ": ".
EXECUTOR_PREFIX_RE = re.compile(r"^(\w+):\s+(.*)")

# Matches a leading @<botid> prefix followed by whitespace.
# The bot id is alphanumeric plus underscore and hyphen.
BOT_PREFIX_RE = re.compile(r"^@([A-Za-z0-9_-]+):?\s+(.*)")


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


#: The ONE supported Glofox task: a fixed, structured schedule request.
#: The prefix routes without an executor script — run_task executes the
#: connector IN-PROCESS under its own fail-closed guard (no spawn, no
#: caller-controlled URL/branch/method/body/filter/date).
GLOFOX_TASK_TEXT = "glofox: schedule"
GLOFOX_SCHEDULE_REQUEST = "schedule"

#: The ONE supported Level 6 weekly MVP command. Like the Glofox task, the
#: prefix routes without an executor script — run_task executes it IN-PROCESS
#: under its own fail-closed guard (no spawn, no caller-controlled URL,
#: branch, filter, or date). The only inputs are fixed inside
#: ``level6_weekly``: the Level 6 Facebook page, the persistent
#: ``browser-bot`` profile, and the pinned Glofox schedule read.
LEVEL6_TASK_TEXT = "level6: weekly"
LEVEL6_WEEKLY_REQUEST = "weekly"

#: The three supported, byte-exact Calendar Reader commands. Like glofox and
#: level6 the prefix routes WITHOUT an executor script -- run_task executes the
#: read IN-PROCESS under its own fail-closed guard, using the OWNER-SCOPED
#: encrypted connector store (connectors.py) rather than any global refresh
#: token. There is NO caller-controlled calendar id, scope, or provider.
CALENDAR_TASK_TODAY = "calendar: today"
CALENDAR_TASK_TOMORROW = "calendar: tomorrow"
CALENDAR_TASK_WEEK = "calendar: week"
CALENDAR_TASK_TEXTS = frozenset({
    CALENDAR_TASK_TODAY, CALENDAR_TASK_TOMORROW, CALENDAR_TASK_WEEK,
})
#: command text -> window key understood by calendar_windows.
CALENDAR_WINDOW_FOR_TASK = {
    CALENDAR_TASK_TODAY: "today",
    CALENDAR_TASK_TOMORROW: "tomorrow",
    CALENDAR_TASK_WEEK: "week",
}


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
        if prefix == "glofox":
            # The exact structured Glofox request only; anything else with
            # the glofox prefix is an unknown task and is rejected.
            if rest.strip() == GLOFOX_SCHEDULE_REQUEST:
                return "glofox", GLOFOX_SCHEDULE_REQUEST, None
            return None, None, "glofox"
        if prefix == "level6":
            # The ONE structured Level 6 weekly command; any other level6
            # task is unknown and rejected (never routed to a default
            # executor, which would run it against the repo).
            if rest.strip() == LEVEL6_WEEKLY_REQUEST:
                return "level6", LEVEL6_WEEKLY_REQUEST, None
            return None, None, "level6"
        if prefix == "calendar":
            # The three byte-exact Calendar Reader commands only; any other
            # calendar request is unknown and rejected (never routed to a
            # default executor, which would run it against the repo).
            candidate = f"calendar: {rest.strip().lower()}"
            if candidate in CALENDAR_TASK_TEXTS:
                return "calendar", candidate, None
            return None, None, "calendar"
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
# Host source of truth: an operation's tier is a property of the
# operation, keyed in colon form (K_BOT_DESIGN.md). The executor never
# supplies this; the host looks it up. Unknown ops are denied upstream.
OPERATION_TIERS: dict[str, int] = {
    "fs:read": 0,
    "cal:list": 0,
    "mail:read": 0,
    "repo:read": 0,
    # Glofox schedule connector: pinned-branch, fail-closed read-only
    # surface (glofox_api.py).  Tier 0 = no approval needed; the connector
    # itself pins the branch, method, URL, and body — nothing to escalate.
    "glofox:read": 0,
    "browser:navigate": 0,
    "browser:read": 0,
    "browser:click": 0,
    "browser:screenshot": 0,
    "browser:download": 1,
    "browser:type": 1,
    "browser:upload": 1,
    "browser:submit": 2,
    "browser:delete": 2,
    "fs:write": 1,
    "cal:create": 1,
    "repo:pr": 1,
    "fs:delete": 2,
    "mail:send": 2,
    "repo:push": 2,
    # Coordination: delegating work to another Bot. This is a HOST operation,
    # never an executor op — no executor emits "bot.delegate", and it is not in
    # EXECUTORS. Tier 0 = a coordinator may delegate without an approval; the
    # TARGET's own restricted operations still gate through their own policy and
    # approval flow. Granting it is what makes a Bot a coordinator.
    "bot:delegate": 0,
}

# Recognised ops in dotted form (the wire format), derived from the
# tier table so the two never drift apart.
# Write-class ops that escalate to T2 when the target repo is external.
_EXTERNAL_WRITE_OPS = frozenset({"repo:pr", "repo:push", "fs:write", "fs:delete"})

KNOWN_OPERATIONS = frozenset(
    k.replace(":", ".", 1) for k in OPERATION_TIERS
)

# A target under any of these is K-Bot's own code/config; a bad self-
# edit there removes the channel used to send the fix, so it is always
# T2 regardless of the operation's base tier (K_BOT_DESIGN.md).
SCOPE_SENSITIVE = ("kyrex-cloud/", ".kyrex/", ".px/")

# A single unbound/operator session is not a Bot and carries no Bot
# policy. Rather than an empty policy (which default-denies even a
# harmless read) or a blanket deny-bypass, it gets an explicit,
# auditable grant of safe reads only. Everything else still
# default-denies.
UNBOUND_POLICY: dict[str, int] = {
    "fs:read": 0,
    "cal:list": 0,
}


# ---------------------------------------------------------------------------
# Writable-Bot gate — the single decision for "is this Bot a developer Bot?".
# It lives here, next to OPERATION_TIERS and the policy engine, so BOTH
# serve.run_task (the executor path) and dev_bot.submit_bot_task (the Chat
# entry point) apply the SAME rule instead of drifting apart.
# ---------------------------------------------------------------------------

# The single write-class operation that marks a Bot as a developer Bot: the
# ability to write files (edit_file / write_file_with_gate). Deletion
# (fs:delete), git push/PR (repo:push / repo:pr), and shell (run_command)
# remain governed by their own host tiers / engine gates and are NOT what
# makes a Bot "writable" here — a Bot that can write files is a coding Bot;
# one that can only list calendars or read mail is not. This is intentionally
# narrower than "any write-class operation" so calendar/mail-only Bots are
# never misrouted to the coding executor path.
DEVELOPER_WRITE_OPS: frozenset[str] = frozenset({"fs:write"})


# The named Developer preset — the ONE explicit, auditable convenience grant
# for making a Bot a writable coding Bot. It grants exactly the two write-
# class operations a coding Bot needs (edit files, open a PR) and NOTHING
# else: ``fs:delete`` and ``repo:push`` are deliberately absent, so they stay
# deny-by-default and continue to require an explicit owner decision. Reads
# remain at tier 0. The preset is defined here, next to the writable-Bot gate
# and the host tier table, so it can never drift from what the gate considers
# writable. Consumers (the Chat API, the UI) read it through the accessors
# below instead of re-declaring it.
DEVELOPER_PRESET_ID = "developer"
DEVELOPER_PRESET_LABEL = "Developer Bot"
DEVELOPER_PRESET: dict[str, int] = {
    "fs:read": 0,
    "repo:read": 0,
    "fs:write": 1,
    "repo:pr": 1,
}


def developer_preset_policy() -> dict:
    """Return a fresh copy of the named Developer preset policy."""
    return dict(DEVELOPER_PRESET)


def effective_permissions(bot_policy) -> dict:
    """Per-operation effective tier for every host-known operation.

    Returns ``{operation: tier}`` where *tier* is ``0``/``1``/``2`` or the
    string ``"deny"``. Derived with the SAME policy engine the executor
    enforces with (:func:`policy.evaluate` + the host tier table), so the
    values shown to an operator in a confirmation step are exactly the ones
    the host will act on. A malformed policy fails closed (every operation
    denied).
    """
    operations = sorted(OPERATION_TIERS)
    if not _valid_policy(bot_policy):
        return {op: "deny" for op in operations}
    out: dict[str, object] = {}
    for op in operations:
        decision = policy.evaluate(bot_policy, op, OPERATION_TIERS[op])
        out[op] = decision.get("effective_tier", "deny")
    return out


def validate_bot_policy(bot_policy) -> None:
    """Fail closed on a policy whose shape ``policy.evaluate`` cannot trust.

    Valid policies map string rules to ``0``/``1``/``2`` or ``"deny"`` — the
    exact value space the evaluator understands. An empty dict is valid (the
    most restrictive policy). Raises ``ValueError`` otherwise.
    """
    if not _valid_policy(bot_policy):
        raise ValueError(
            "bot policy must be a dict mapping string rules to 0, 1, 2, or 'deny'"
        )


def _valid_policy(bot_policy) -> bool:
    """True when *bot_policy* has the exact shape ``policy.evaluate`` understands."""
    if not isinstance(bot_policy, dict):
        return False
    for key, value in bot_policy.items():
        if not isinstance(key, str):
            return False
        if value == "deny":
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if value not in (0, 1, 2):
            return False
    return True


def is_writable_bot_policy(bot_policy) -> bool:
    """Return True iff *bot_policy* explicitly grants the developer write op.

    "Grants" means the EXISTING policy evaluator returns a numeric effective
    tier >= 1 for ``fs:write`` given its host-derived tier. Because numeric
    policy rules may only RAISE the host tier, a matching numeric rule for
    ``fs:write`` (host tier already 1) is a write grant. ``deny``, no matching
    rule, and malformed policies are read-only (fail closed).
    """
    if not _valid_policy(bot_policy):
        return False
    for op in sorted(DEVELOPER_WRITE_OPS):
        decision = policy.evaluate(bot_policy, op, OPERATION_TIERS[op])
        effective = decision.get("effective_tier")
        if isinstance(effective, int) and effective >= 1:
            return True
    return False


def scope_escalates(target: str) -> bool:
    """True if *target* touches K-Bot's own code or config."""
    t = target or ""
    return any(marker in t for marker in SCOPE_SENSITIVE)


# ---------------------------------------------------------------------------
# Coordinator-Bot gate — the single decision for "is this Bot a coordinator?".
# It lives here, next to the writable-Bot gate and the host tier table, so the
# Chat routing layer, the delegation module, and the engine capability mapping
# all apply the SAME rule instead of drifting apart. A coordinator is a Bot
# whose OWNER explicitly granted it the host coordination operation
# ``bot:delegate`` (tier 0). Nothing about the grant is cross-owner: it is a
# policy on a Bot the owner owns, and only the owner may configure a Bot.
# ---------------------------------------------------------------------------

# The only operation that makes a Bot a coordinator. It is a HOST operation:
# no executor implements it, and it never reaches ``serve.run_task``.
COORDINATOR_GRANT_OPS: frozenset[str] = frozenset({"bot:delegate"})

# The named Coordinator preset — the ONE explicit, auditable convenience grant
# for making a Bot the owner's "Chief of Staff". It grants exactly the
# coordination operation plus the safe reads a coordinator needs to describe
# work; it deliberately grants NO write, delete, push, or shell capability, so
# a coordinator can only observe and delegate — the delegated TARGET remains
# authoritative for any consequential action (and its approvals).
COORDINATOR_PRESET_ID = "coordinator"
COORDINATOR_PRESET_LABEL = "Chief of Staff (coordinator)"
COORDINATOR_PRESET: dict[str, int] = {
    "fs:read": 0,
    "repo:read": 0,
    "bot:delegate": 0,
}


def coordinator_preset_policy() -> dict:
    """Return a fresh copy of the named Coordinator preset policy."""
    return dict(COORDINATOR_PRESET)


def is_coordinator_policy(bot_policy) -> bool:
    """Return True iff *bot_policy* explicitly grants the coordination op.

    "Grants" means the EXISTING policy evaluator returns effective tier ``0``
    for ``bot:delegate`` given its host-derived tier. Because numeric rules may
    only RAISE the host tier and ``bot:delegate`` is host tier 0, only an
    explicit ``bot:delegate`` (or a covering wildcard) at tier 0 grants it.
    ``deny``, no matching rule, a raised tier, and malformed policies are all
    NOT coordinator grants (fail closed).
    """
    if not _valid_policy(bot_policy):
        return False
    for op in sorted(COORDINATOR_GRANT_OPS):
        decision = policy.evaluate(bot_policy, op, OPERATION_TIERS[op])
        effective = decision.get("effective_tier")
        if isinstance(effective, int) and effective == 0:
            return True
    return False


def coordinator_granted(bot) -> bool:
    """Convenience: is *bot* (a registry record) a coordinator?"""
    return is_coordinator_policy((bot or {}).get("policy"))


# ---------------------------------------------------------------------------
# Browser-Bot gate — the single decision for "is this Bot a read-only Browser
# Bot?". It lives here, next to the host tier table, so the Chat API, the UI
# badge, and the browser-route readiness check all read ONE definition.
#
# The two operations a Browser Bot is granted are exactly the ones the
# established read-only browser flow performs: navigation (``browser:navigate``)
# and page reading (``browser:read``), both at their host-derived tier 0.
# Every interaction/write browser op — click, screenshot, type, upload,
# download, submit, delete — is deliberately ABSENT from the preset, so it
# stays deny-by-default; so are ``fs:write``/``fs:delete``, ``repo:pr``/
# ``repo:push``, ``mail:send``, ``cal:create``, and the coordination op
# ``bot:delegate``. A Browser Bot observes pages; it never actuates or writes.
# ---------------------------------------------------------------------------
BROWSER_PRESET_ID = "browser"
BROWSER_PRESET_LABEL = "Browser Bot"
BROWSER_PRESET: dict[str, int] = {
    "browser:navigate": 0,
    "browser:read": 0,
    # The ONE server-controlled schedule read grant. Browsers cannot click,
    # type, submit, screenshot, download, upload, or delete; this adds
    # exactly the pinned Level 6 schedule read (glofox:read, tier 0) and
    # nothing else. EXISTING configured Bots keep their stored policies —
    # they do NOT gain this silently; they must be explicitly
    # reconfigured THROUGH the preset after deployment.
    "glofox:read": 0,
}

# The exact browser operations the preset grants (tier 0). Everything else the
# host knows stays denied; the preset policy and this set never drift because
# the set is derived from the policy.
BROWSER_GRANT_OPS: frozenset[str] = frozenset(BROWSER_PRESET)

# Operations that take a Bot OFF the read-only browser path. A policy that
# grants ANY of these is never reported as a Browser Bot, even if it also
# grants navigate/read — the badge is a least-privilege claim, so it must be
# false the moment a write/interaction/coordination capability is present.
BROWSER_DENIED_OPS: frozenset[str] = frozenset({
    "browser:click", "browser:screenshot", "browser:type",
    "browser:upload", "browser:download", "browser:submit",
    "browser:delete", "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "bot:delegate",
})


def browser_preset_policy() -> dict:
    """Return a fresh copy of the named Browser preset policy."""
    return dict(BROWSER_PRESET)


def glofox_read_granted(bot_policy) -> bool:
    """EXACT ``glofox:read`` read-tier grant test — shared by the executor
    path, the Chat submission path, and the Routine submission path.

    The policy must map the EXACT rule ``glofox:read`` to tier 0 and must
    not be denied: a prefix wildcard (``glofox:*``), the ``*`` catch-all,
    or any other key NEVER grants Glofox schedule reads. Mirrors the
    singularity check the executor path applies inline.
    """
    if not _valid_policy(bot_policy):
        return False
    derived = derive_host_tier("glofox:read")
    decision = policy.evaluate(bot_policy, "glofox:read", derived)
    tier = policy.enforce(decision)
    return (
        decision.get("matched_rule") == "glofox:read"
        and tier == 0
        and decision.get("effective_tier") != "deny"
    )


def is_browser_bot_policy(bot_policy) -> bool:
    """Return True iff *bot_policy* is EXACTLY a read-only Browser grant.

    A Browser-Bot policy must grant EVERY operation in the CURRENT Browser
    Bot preset — ``browser:navigate``, ``browser:read`` and the pinned
    ``glofox:read`` schedule read — at their host tier 0, and grant NONE
    of the interaction/write/coordination operations in
    :data:`BROWSER_DENIED_OPS`. Anything else — a missing grant, a deny, a
    raised tier, a malformed policy, or any extra capability — is NOT a
    Browser Bot (fail closed). This is the single predicate behind the
    Browser Bot badge, so the badge can only ever under-claim, never
    over-claim. LOCKSTEP with the preset: Bot policies stored BEFORE the
    ``glofox:read`` grant no longer match and must be explicitly
    reconfigured THROUGH the preset; they never silently gain the new
    grant (their stored policies still map only navigate/read — nothing
    is injected at classification time).
    """
    if not _valid_policy(bot_policy):
        return False
    for op in sorted(BROWSER_GRANT_OPS):
        decision = policy.evaluate(bot_policy, op, OPERATION_TIERS[op])
        effective = decision.get("effective_tier")
        if not (isinstance(effective, int) and effective == 0):
            return False
    for op in sorted(BROWSER_DENIED_OPS):
        decision = policy.evaluate(bot_policy, op, OPERATION_TIERS[op])
        if isinstance(decision.get("effective_tier"), int):
            return False
    return True


def browser_bot_granted(bot) -> bool:
    """Convenience: is *bot* (a registry record) a least-privilege Browser Bot?

    This is the POLICY half only — it says the capability grant is exactly the
    read-only browser grant. Whether the Bot can actually RUN still requires a
    non-empty allowlist and an explicit Browser Host binding; the Chat API
    composes that for the UI badge.
    """
    return is_browser_bot_policy((bot or {}).get("policy"))


# ---------------------------------------------------------------------------
# Glofox-Reader gate — the single decision for "is this Bot a Glofox Reader?".
# It lives here, next to the host tier table and the shared ``glofox:read``
# grant test, so the Chat API, the UI badge, and the routing layer read ONE
# definition instead of re-declaring the grant.
#
# A Glofox Reader is the SMALLEST read-only Bot that can serve the one pinned
# owner-facing command ``glofox: schedule``: it holds EXACTLY the server-defined
# ``glofox:read`` read-tier grant and NOTHING else. Every browser operation,
# every write/delete/push, mail, calendar, and the coordination op
# ``bot:delegate`` are deliberately ABSENT, so they stay deny-by-default. This
# is DISTINCT from the Browser preset (which also grants ``browser:navigate`` /
# ``browser:read`` and therefore is never a Glofox Reader): a Glofox Reader has
# no browser surface at all — no allowlist and no Browser Host binding.
# ---------------------------------------------------------------------------
GLOFOX_READER_PRESET_ID = "glofox-reader"
GLOFOX_READER_PRESET_LABEL = "Glofox Reader"
GLOFOX_READER_PRESET: dict[str, int] = {
    # The ONE server-controlled schedule read grant. Browsers cannot click,
    # type, submit, screenshot, download, upload, or delete, and a Glofox
    # Reader cannot browse at all; this adds exactly the pinned Level 6
    # schedule read (glofox:read, tier 0) and nothing else.
    "glofox:read": 0,
}


def glofox_reader_preset_policy() -> dict:
    """Return a fresh copy of the named Glofox Reader preset policy."""
    return dict(GLOFOX_READER_PRESET)


def is_glofox_reader_policy(bot_policy) -> bool:
    """Return True iff *bot_policy* is EXACTLY the read-only Glofox Reader grant.

    The policy must grant the EXACT ``glofox:read`` read-tier grant (via
    :func:`glofox_read_granted` — a prefix wildcard ``glofox:*`` or the ``*``
    catch-all never counts) AND grant NO other host-known operation. Anything
    else — a missing grant, a raised tier, a malformed policy, or ANY extra
    capability (browser, write, delete, push, mail, calendar, coordination) —
    is NOT a Glofox Reader (fail closed). This is the single predicate behind
    the Glofox Reader badge, so the badge can only ever under-claim.

    It is intentionally STRICTER than :func:`glofox_read_granted`: a Bot with
    the schedule grant plus any other capability is not the least-privilege
    Glofox Reader, even though the executor/route gates may still accept it.
    """
    if not _valid_policy(bot_policy):
        return False
    if not glofox_read_granted(bot_policy):
        return False
    for op in sorted(OPERATION_TIERS):
        if op == "glofox:read":
            continue
        decision = policy.evaluate(bot_policy, op, OPERATION_TIERS[op])
        if isinstance(decision.get("effective_tier"), int):
            return False
    return True


def glofox_reader_granted(bot) -> bool:
    """Convenience: is *bot* (a registry record) exactly a Glofox Reader?

    The POLICY half only — the least-privilege ``glofox:read`` grant. Whether
    the Bot can actually serve the pinned command still requires it to be
    lifecycle-``running`` and to hold a resolvable provider profile; the Chat
    routing layer applies those separately.
    """
    return is_glofox_reader_policy((bot or {}).get("policy"))


# ---------------------------------------------------------------------------
# Calendar-Reader gate -- the single decision for "is this Bot a Calendar
# Reader?". It lives here, next to the host tier table and the shared
# ``cal:list`` grant test, so the Chat API, the UI badge, and the routing
# layer read ONE definition.
#
# A Calendar Reader is the SMALLEST read-only Bot that can serve the three
# pinned owner-facing commands ``calendar: today|tomorrow|week``: it holds
# EXACTLY the host ``cal:list`` read-tier grant and NOTHING else. Every
# browser op, every write/delete/push, mail, ``cal:create``, and the
# coordination op ``bot:delegate`` are deliberately ABSENT, so they stay
# deny-by-default. It is DISTINCT from every other preset: none is widened.
# ---------------------------------------------------------------------------
CALENDAR_READER_PRESET_ID = "calendar-reader"
CALENDAR_READER_PRESET_LABEL = "Calendar Reader"
CALENDAR_READER_PRESET: dict[str, int] = {
    # The ONE server-controlled, read-only calendar grant. No create, no
    # mail, no browser, no filesystem/repo, no Glofox, no coordination.
    "cal:list": 0,
}

#: Bounded relay for a Calendar Reader response.
_CALENDAR_RESULT_CHAR_LIMIT = 4000


def calendar_reader_preset_policy() -> dict:
    """Return a fresh copy of the named Calendar Reader preset policy."""
    return dict(CALENDAR_READER_PRESET)


def cal_list_granted(bot_policy) -> bool:
    """EXACT ``cal:list`` read-tier grant test -- shared by the executor path,
    the Chat submission path, and the route-readiness check.

    The policy must map the EXACT rule ``cal:list`` to tier 0 and must not be
    denied: a prefix wildcard (``cal:*``), the ``*`` catch-all, or any other
    key NEVER grants a calendar read.
    """
    if not _valid_policy(bot_policy):
        return False
    derived = derive_host_tier("cal:list")
    decision = policy.evaluate(bot_policy, "cal:list", derived)
    tier = policy.enforce(decision)
    return (
        decision.get("matched_rule") == "cal:list"
        and tier == 0
        and decision.get("effective_tier") != "deny"
    )


def is_calendar_reader_policy(bot_policy) -> bool:
    """Return True iff *bot_policy* is EXACTLY the read-only Calendar Reader
    grant (``cal:list`` tier 0 and NOTHING else).

    Anything else -- a missing grant, a raised tier, a wildcard, a malformed
    policy, or ANY extra capability -- is NOT a Calendar Reader (fail closed).
    """
    if not _valid_policy(bot_policy):
        return False
    if not cal_list_granted(bot_policy):
        return False
    for op in sorted(OPERATION_TIERS):
        if op == "cal:list":
            continue
        decision = policy.evaluate(bot_policy, op, OPERATION_TIERS[op])
        if isinstance(decision.get("effective_tier"), int):
            return False
    return True


def calendar_reader_granted(bot) -> bool:
    """Convenience: is *bot* (a registry record) exactly a Calendar Reader?"""
    return is_calendar_reader_policy((bot or {}).get("policy"))


def _calendar_fail_closed(ctx, op_code, reason, chat_id, send) -> None:
    """Audit + report a terminal Calendar Reader failure. Never raises."""
    try:
        send(chat_id, f"\u26a0\ufe0f Calendar task failed closed: {reason}")
    except Exception:  # noqa: BLE001
        pass
    try:
        audit.log(
            bot_id=getattr(ctx, "bot_id", ""), operation=op_code, tier="n/a",
            decision="deny", outcome="fail_closed", detail={"reason": reason})
    except Exception as exc:
        print(f"[serve] audit log failure: {exc}", file=sys.stderr)


def _run_calendar_read_task(ctx, chat_id, task_text, task_id, send,
                            on_progress=None, on_result=None) -> None:
    """Execute ONE of the three pinned Calendar Reader commands, fail closed.

    Identity: a bound Bot (explicit owner + id). Policy: an EXACT ``cal:list``
    tier-0 rule. Credentials: the OWNER-SCOPED encrypted connector store --
    NEVER a global ``GOOGLE_REFRESH_TOKEN``. Lifecycle: the durable task must
    be running and uncancelled. On success the readable lines are delivered
    via BOTH ``on_result`` (durable terminal result) and the friendly relay.
    """
    window = CALENDAR_WINDOW_FOR_TASK.get(str(task_text or "").strip())
    if window is None:
        _calendar_fail_closed(
            ctx, "cal.list", f"unsupported calendar request {task_text!r}",
            chat_id, send)
        return
    bot_id = str(getattr(ctx, "bot_id", "") or "").strip()
    owner = str(getattr(ctx, "bot_owner", "") or "").strip()
    if not owner or not bot_id or bot_id == "calendar":
        _calendar_fail_closed(
            ctx, "cal.list",
            "calendar reads run only for a bound Bot with an owner",
            chat_id, send)
        return
    if not cal_list_granted(ctx.policy):
        try:
            audit.log(
                bot_id=ctx.bot_id, operation="cal.list", tier="n/a",
                decision="deny", outcome="blocked",
                detail={"reason": "no exact cal:list grant"})
        except Exception as exc:
            print(f"[serve] audit log failure: {exc}", file=sys.stderr)
        send(chat_id, "\u26a0\ufe0f Calendar read denied: no exact cal:list grant")
        return
    if not task_id:
        _calendar_fail_closed(
            ctx, "cal.list",
            "calendar reads require a durable task (worker path only)",
            chat_id, send)
        return
    from task_store import CloudTaskStore, STATUS_RUNNING  # local: cycle-safe
    store = CloudTaskStore()
    rec = store.get(task_id)
    if rec is None or rec.get("status") != STATUS_RUNNING:
        _calendar_fail_closed(
            ctx, "cal.list", f"task {task_id} is not running", chat_id, send)
        return
    if rec.get("cancel_requested"):
        _calendar_fail_closed(
            ctx, "cal.list", f"task {task_id} was cancelled", chat_id, send)
        return
    try:
        import calendar_windows as _cw
        import connectors as _connectors
    except Exception as exc:  # noqa: BLE001
        _calendar_fail_closed(
            ctx, "cal.list", f"reader unavailable: {exc}", chat_id, send)
        return
    try:
        label, time_min, time_max = _cw.window_bounds(window)
        events = _connectors.default_store().calendar(owner).events(
            time_min=time_min, time_max=time_max, max_results=_cw.MAX_EVENTS)
        text = _cw.render_events(label, events)
    except _connectors.ConnectorConfigError:
        _calendar_fail_closed(
            ctx, "cal.list",
            "Google Calendar is not configured on this host", chat_id, send)
        return
    except _connectors.ConnectorUnavailable:
        hint = "Connect Google Calendar in Settings, then try again."
        try:
            audit.log(bot_id=ctx.bot_id, operation="cal.list", tier="tier0",
                      decision="allow", outcome="unavailable",
                      detail={"reason": "connector not connected or expired"})
        except Exception as exc:
            print(f"[serve] audit log failure: {exc}", file=sys.stderr)
        if on_result is not None:
            try:
                on_result({"status": "no_changes", "count": 0,
                           "final_response": f"Calendar read unavailable. {hint}"})
            except Exception as exc:
                print(f"[serve] calendar on_result failure: {exc}",
                      file=sys.stderr)
        send(chat_id, f"\u26a0\ufe0f Calendar read unavailable. {hint}")
        return
    except Exception as exc:  # noqa: BLE001 -- every failure fails closed
        _calendar_fail_closed(
            ctx, "cal.list", f"{type(exc).__name__}: {exc}", chat_id, send)
        return
    if len(text) > _CALENDAR_RESULT_CHAR_LIMIT:
        text = text[:_CALENDAR_RESULT_CHAR_LIMIT] + " ... [truncated]"
    try:
        audit.log(bot_id=ctx.bot_id, operation="cal.list", tier="tier0",
                  decision="allow", outcome="auto",
                  detail={"window": window, "events": len(events)})
    except Exception as exc:
        print(f"[serve] audit log failure: {exc}", file=sys.stderr)
    if on_result is not None:
        try:
            on_result({"status": "no_changes", "final_response": text,
                       "count": len(events), "window": window})
        except Exception as exc:
            print(f"[serve] calendar on_result failure: {exc}", file=sys.stderr)
    send(chat_id, text)


# ---------------------------------------------------------------------------
# Level 6 Weekly gate — the single decision for "may this Bot run the one
# pinned `level6: weekly` command?". It lives here, next to the host tier
# table and the shared exact-grant helpers, so the executor path and any
# future Chat/UI surface read ONE definition.
#
# The command needs EXACTLY four read-only operations and nothing else: the
# browser capture (``browser:navigate`` + ``browser:read`` +
# ``browser:screenshot``) and the pinned Level 6 schedule read
# (``glofox:read``). All four are host tier 0. Every interaction/write browser
# op (click, type, upload, download, submit, delete), filesystem/repo writes,
# mail, calendar, and the coordination op ``bot:delegate`` are deliberately
# ABSENT, so they stay deny-by-default.
#
# This is its OWN dedicated preset. It is NOT the Browser preset (which has
# only navigate/read/glofox:read) and NOT the Glofox Reader preset (which has
# no browser surface at all): neither existing preset is widened, and this one
# grants nothing beyond the four operations the command performs.
# ---------------------------------------------------------------------------
LEVEL6_WEEKLY_PRESET_ID = "level6-weekly"
LEVEL6_WEEKLY_PRESET_LABEL = "Level 6 Weekly"
LEVEL6_WEEKLY_PRESET: dict[str, int] = {
    "browser:navigate": 0,
    "browser:read": 0,
    "browser:screenshot": 0,
    "glofox:read": 0,
}

# The exact operations the preset grants (tier 0). Derived from the policy so
# the two can never drift.
LEVEL6_WEEKLY_GRANT_OPS: frozenset[str] = frozenset(LEVEL6_WEEKLY_PRESET)


def level6_weekly_preset_policy() -> dict:
    """Return a fresh copy of the dedicated Level 6 Weekly preset policy."""
    return dict(LEVEL6_WEEKLY_PRESET)


#: The ONE browser domain the Level 6 weekly capture may open — the host of the
#: pinned Level 6 Facebook page. FIXED by the preset (the capture itself is
#: pinned to that page); a caller never supplies it, and the Chat surface stores
#: exactly this list. `facebook.com` (the bare hostname the Browser Operator's
#: allowlist uses) is the only entry.
LEVEL6_WEEKLY_ALLOWLIST: tuple[str, ...] = ("facebook.com",)


def level6_weekly_preset_allowlist() -> list[str]:
    """Return a fresh copy of the preset's fixed browser domain allowlist."""
    return list(LEVEL6_WEEKLY_ALLOWLIST)


def level6_browser_bot_id() -> str:
    """The persistent Browser-Host profile id the weekly capture runs under.

    A Level 6 Weekly Bot does NOT carry its own Browser Host binding: the
    capture reuses the owner's EXISTING persistent ``browser-bot`` profile
    through the already-implemented dispatch path
    (``_level6_browser_dispatch`` swaps only the bot id). The single source of
    truth is ``level6_weekly.BROWSER_BOT_ID``; the Chat surface reads THIS
    accessor so it never re-declares the id.
    """
    import level6_weekly as _level6
    return str(_level6.BROWSER_BOT_ID)


def _exact_zero_grant(bot_policy, op: str) -> bool:
    """True iff *bot_policy* maps the EXACT rule *op* to tier 0.

    A prefix wildcard (``browser:*``), the ``*`` catch-all, or any other key
    NEVER grants the operation — mirrors the singularity check the Glofox
    executor path applies to ``glofox:read``.
    """
    if not _valid_policy(bot_policy):
        return False
    decision = policy.evaluate(bot_policy, op, OPERATION_TIERS[op])
    tier = policy.enforce(decision)
    return (
        decision.get("matched_rule") == op
        and tier == 0
        and decision.get("effective_tier") != "deny"
    )


def level6_weekly_granted(bot_policy) -> bool:
    """Return True iff *bot_policy* is EXACTLY the Level 6 Weekly grant.

    Fail closed: the policy must grant EVERY operation in the dedicated preset
    (``browser:navigate``, ``browser:read``, ``browser:screenshot``, and the
    pinned ``glofox:read``) via its EXACT rule at host tier 0, AND grant NONE
    of the other host-known operations. A missing grant, a deny, a raised
    tier, a malformed policy, a wildcard in place of an exact rule, or ANY
    extra capability is NOT a Level 6 Weekly grant — the command then refuses
    to run rather than borrowing a broader capability.
    """
    if not _valid_policy(bot_policy):
        return False
    for op in sorted(LEVEL6_WEEKLY_GRANT_OPS):
        if not _exact_zero_grant(bot_policy, op):
            return False
    for op in sorted(OPERATION_TIERS):
        if op in LEVEL6_WEEKLY_GRANT_OPS:
            continue
        decision = policy.evaluate(bot_policy, op, OPERATION_TIERS[op])
        if isinstance(decision.get("effective_tier"), int):
            return False
    return True


def derive_host_tier(colon_op: str, target: str = "",
                     declared=None, count=None, is_external: bool = False):
    """Derive the tier the host will act on, from the operation itself.

    Returns an int tier, or ``None`` if *colon_op* is not a recognised
    operation (the caller denies unknown ops before policy).

    Rules (K_BOT_DESIGN.md / K_BOT_AUTONOMY.md):
      * base tier comes from OPERATION_TIERS, never the executor;
      * scope escalation forces T2 for self-directed ops;
      * volume escalation forces T2 when a structured count > 50;
      * a valid executor-declared tier is a hint that can only RAISE.
    """
    base = OPERATION_TIERS.get(colon_op)
    if base is None:
        return None
    if scope_escalates(target):
        base = 2
    # External-repo writes escalate to T2: pushing/PR-ing to a repo that
    # is not our own is the highest-consequence action. Host-decided
    # (is_external), never from the executor's target field.
    if is_external and colon_op in _EXTERNAL_WRITE_OPS:
        base = 2
    if isinstance(count, int) and count > 50:
        base = 2
    if isinstance(declared, int):
        # Hint may only raise, and never above T2 — an executor cannot
        # invent a tier, so an out-of-range value is clamped, not trusted.
        base = max(base, min(declared, 2))
    return base


DESTRUCTIVE_VERBS = frozenset({
    "delete", "remove", "trash", "send", "push", "force", "revoke", "drop",
})


def derive_tier(executor_prefix: str, approval: dict) -> int:
    """Derive an approval's tier host-side.

    Prefers a structured ``op`` when the executor supplies one, so the
    tier comes from OPERATION_TIERS. Absent a recognised op the tier is
    ambiguous, so it escalates to at least T1 (never T0), and to T2 on
    a destructive verb or a scope-sensitive target. A valid declared
    tier may only raise the result.
    """
    op = approval.get("op") or ""
    colon = op.replace(".", ":", 1) if "." in op else op
    target = approval.get("target", "")
    declared = approval.get("tier")
    count = approval.get("count")

    base = derive_host_tier(colon, target, declared=declared, count=count)
    if base is not None:
        return base

    # No recognised structured op: ambiguity escalates.
    summary = approval.get("summary", "")
    first_word = summary.split()[0].lower() if summary.strip() else ""
    base = 2 if first_word in DESTRUCTIVE_VERBS else 1
    if scope_escalates(target):
        base = 2
    if isinstance(count, int) and count > 50:
        base = 2
    if isinstance(declared, int):
        base = max(base, min(declared, 2))
    return base


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
    bot_owner: str = ""
    browser_allowlist: list = field(default_factory=list)
    model: str = ""
    system_prompt: str = ""
    # Per-Bot LLM configuration resolved from the Bot's encrypted provider
    # profile. Populated ONLY for a Bot that references a resolvable profile;
    # these are the secrets (API key + header values) the executor must run
    # with, and they take priority over every KYREX_* global. Empty when the
    # Bot has no profile reference (legacy behaviour preserved).
    llm_provider: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_headers: dict = field(default_factory=dict)
    llm_profile_id: str = ""
    # Set when the Bot DOES reference a profile that cannot be resolved: the
    # run must fail closed rather than silently fall back to the globals.
    llm_error: str = ""


def bot_browser_allowlist(bot: dict) -> list[str]:
    """Return a Bot's browser site/domain allowlist (always a clean list).

    Fail closed: a missing, non-list, or malformed field yields an empty
    list, which the Browser Operator treats as "deny every navigation". This
    is the single reader of the ``browser_allowlist`` registry field; the
    executor receives the result through ``KYREX_BROWSER_ALLOWLIST``.
    """
    raw = (bot or {}).get("browser_allowlist")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        host = entry.strip().lower()
        if host and host not in out:
            out.append(host)
    return out


def browser_preflight_block(ctx: "ExecutionContext", task_text: str) -> str | None:
    """Return a block reason if a browser task must not run, else ``None``.

    Runs the Browser Operator's own preflight against the Bot's allowlist so a
    blocked navigation never spawns a process. The operator is imported lazily
    (only browser tasks pay for it) and a failure to import fails closed —
    the task is blocked, never run unguarded.
    """
    try:
        import browser_operator as _browser
    except Exception as exc:
        return f"browser operator unavailable: {exc}"
    allowed, reason = _browser.preflight(
        task_text, getattr(ctx, "browser_allowlist", None)
    )
    return None if allowed else reason


#: Bounded result relay: formatted output above this many characters is
#: truncated with an explicit marker rather than streamed unbounded.
_GLOFOX_RESULT_CHAR_LIMIT = 4000


def _glofox_fail_closed(
    ctx: ExecutionContext,
    op_code: str,
    reason: str,
    chat_id: int,
    send,
) -> None:
    """Audit + report a terminal Glofox task failure. Never raises."""
    try:
        send(chat_id, f"⚠️ Glofox task failed closed: {reason}")
    except Exception:  # noqa: BLE001 — transport failure must not mask audit
        pass
    try:
        audit.log(
            bot_id=ctx.bot_id,
            operation=op_code,
            tier="deny" if op_code == "glofox.read" else "n/a",
            decision="deny",
            outcome="fail_closed",
            detail={"reason": reason},
        )
    except Exception as exc:
        print(f"[serve] audit log failure: {exc}", file=sys.stderr)


def _run_glofox_schedule_task(
    ctx: "ExecutionContext",
    chat_id,
    task_text: str,
    task_id,
    send,
    on_progress=None,
    on_result=None,
) -> None:
    """Execute the ONE supported Glofox request, fail closed.

    Identity: a bound Bot (explicit owner + id) whose ALLOWLISTED identity
    is the connector's only production entry point.  Policy: an EXACT
    ``glofox:read`` rule granting tier 0 — prefix wildcards and ``*`` never
    count.  Lifecycle: the durable task must be in ``running`` and
    uncancelled immediately before and after the network work.  On
    success the validated rows are delivered via BOTH ``on_result``
    (durable terminal result; the worker only marks the task ``done``
    when this callback captured a result) and the friendly relay.
    """
    # 1. Exact structured request — no caller-controlled surface.
    if task_text != GLOFOX_TASK_TEXT:
        _glofox_fail_closed(
            ctx, "glofox.schedule",
            f"unsupported Glofox request {task_text!r}", chat_id, send
        )
        return

    # 2. Owner-scoped bound Bot identity (same bar as browser sessions).
    bot_id = str(getattr(ctx, "bot_id", "") or "").strip()
    owner = str(getattr(ctx, "bot_owner", "") or "").strip()
    # The UNBOUND context is built with bot_id = executor_prefix, so a
    # bound Bot's id always differs from "glofox".
    if not owner or not bot_id or bot_id == "glofox":
        _glofox_fail_closed(
            ctx, "glofox.schedule",
            "Glofox tasks run only for a bound Bot with an owner", chat_id, send
        )
        return

    # 3. EXACT glofox:read grant at tier 0 — prefix wildcards and "*"
    # NEVER count (shared predicate: dev_bot/routines submissions apply
    # the SAME gate).  Policy is evaluated BEFORE any task-state probe,
    # so an ungrantable identity is refused without touching task
    # internals.
    if not glofox_read_granted(ctx.policy):
        try:
            audit.log(
                bot_id=ctx.bot_id,
                operation="glofox.read",
                tier="n/a",
                decision="deny",
                outcome="blocked",
                detail={"reason": "no exact glofox:read grant",
                        "matched_rule": policy.evaluate(
                            ctx.policy, "glofox:read", 0).get("matched_rule")},
            )
        except Exception as exc:
            print(f"[serve] audit log failure: {exc}", file=sys.stderr)
        send(chat_id, "⚠️ Glofox task denied: no exact glofox:read grant")
        return

    # 4b. Durable task lifecycle: must exist, be RUNNING, and uncancelled
    # immediately BEFORE the network work (the worker owns the lifecycle;
    # this is the in-process double-check + cancellation probe).
    if not task_id:
        _glofox_fail_closed(
            ctx, "glofox.schedule",
            "Glofox tasks require a durable task (worker path only)",
            chat_id, send
        )
        return
    from task_store import CloudTaskStore, STATUS_RUNNING  # local import: cycle-safe
    store = CloudTaskStore()
    rec = store.get(task_id)
    if rec is None or rec.get("status") != STATUS_RUNNING:
        _glofox_fail_closed(
            ctx, "glofox.schedule",
            f"task {task_id} is not running", chat_id, send
        )
        return
    if rec.get("cancel_requested"):
        _glofox_fail_closed(
            ctx, "glofox.schedule", f"task {task_id} was cancelled",
            chat_id, send
        )
        return

    # 5. Run the connector in-process. It is read-only and pinned; the only
    # failure modes are GlofoxError subclasses, which fail closed. Lazy
    # import: an import failure is a terminal failure, never a fallback.
    try:
        import glofox_api as _glofox
    except Exception as exc:  # noqa: BLE001
        _glofox_fail_closed(
            ctx, "glofox.schedule", f"connector unavailable: {exc}", chat_id, send
        )
        return
    try:
        rows = _glofox.week_0830_classes()
    except Exception as exc:  # noqa: BLE001 — every failure fails closed
        _glofox_fail_closed(
            ctx, "glofox.read", f"{type(exc).__name__}: {exc}", chat_id, send
        )
        return

    if store.is_cancel_requested(task_id):
        # Cancelled mid-flight: relay nothing, no result, fail closed.
        _glofox_fail_closed(
            ctx, "glofox.read", f"task {task_id} was cancelled", chat_id, send
        )
        return

    if not rows:
        _glofox_fail_closed(
            ctx, "glofox.read", "no validated 8:30 AM classes in the window",
            chat_id, send
        )
        return

    lines = [
        f"{row['date']} {row['class_name']}"
        f" — {row['trainer_name']} ({row['trainer_id']}) [{row['event_id']}]"
        for row in rows
    ]
    relay = "\n".join(lines)
    if len(relay) > _GLOFOX_RESULT_CHAR_LIMIT:
        relay = relay[:_GLOFOX_RESULT_CHAR_LIMIT] + " … [truncated]"
    message = "📅 Glofox 8:30 AM classes (next Mon-Sat):\n" + relay
    try:
        audit.log(
            bot_id=ctx.bot_id,
            operation="glofox.read",
            tier="tier0",
            decision="allow",
            outcome="auto",
            detail={"rows": len(rows), "dates": [r["date"] for r in rows]},
        )
    except Exception as exc:
        print(f"[serve] audit log failure: {exc}", file=sys.stderr)
    # The worker finalizes the durable task from the on_result capture
    # (state["result_captured"]); without it execute_task marks the task
    # FAILED ("no result produced by executor") even on success.
    if on_result is not None:
        try:
            # The durable terminal result carries the READ itself (validated
            # rows) plus the same human-facing text the relay used, so a Chat
            # viewer of this turn renders the schedule as the assistant reply
            # (status "no_changes" ⇒ format_result echoes final_response).
            on_result({
                "status": "no_changes",
                "final_response": message,
                "rows": rows,
                "count": len(rows),
                "dates": [r["date"] for r in rows],
            })
        except Exception as exc:
            print(f"[serve] glofox on_result failure: {exc}", file=sys.stderr)
    send(chat_id, message)


def _level6_fail_closed(
    ctx: ExecutionContext,
    op_code: str,
    reason: str,
    chat_id,
    send,
) -> None:
    """Audit + report a terminal Level 6 weekly failure. Never raises."""
    try:
        send(chat_id, f"⚠️ Level 6 weekly failed closed: {reason}")
    except Exception:  # noqa: BLE001 — transport failure must not mask audit
        pass
    try:
        audit.log(
            bot_id=ctx.bot_id,
            operation=op_code,
            tier="deny" if op_code == "level6.weekly" else "n/a",
            decision="deny",
            outcome="fail_closed",
            detail={"reason": reason},
        )
    except Exception as exc:
        print(f"[serve] audit log failure: {exc}", file=sys.stderr)


def _level6_browser_dispatch(ctx: "ExecutionContext", task_text: str, *,
                             on_progress=None):
    """Dispatch the pinned capture to the persistent ``browser-bot`` profile.

    The capture runs on the SAME existing Browser Host path every browser
    task uses, but selects the dedicated persistent ``browser-bot`` profile:
    the ``(owner, browser-bot)`` profile directory on the host is what keeps
    the Facebook session alive across runs. The Level 6 Bot remains the
    authorization identity; only the server-derived profile/binding id differs.
    The channel revalidates that split against the byte-exact task and exact
    Level 6 grant before any operation reaches the host.
    """
    try:
        import level6_weekly as _level6
    except Exception as exc:  # noqa: BLE001 — fail closed, never a fallback
        return None, f"level6 weekly module unavailable: {exc}"
    return browser_host_dispatch(
        ctx, task_text, on_progress=on_progress,
        profile_bot_id=_level6.BROWSER_BOT_ID,
    )


def _run_level6_weekly_task(
    ctx: "ExecutionContext",
    chat_id,
    task_text: str,
    send,
    on_progress=None,
    on_result=None,
) -> None:
    """Execute the ONE supported ``level6: weekly`` command, fail closed.

    Identity: a bound Bot (explicit owner + id). Policy: the dedicated
    Level 6 Weekly grant — the four EXACT tier-0 operations and nothing else.
    Capture: the EXISTING Browser Host path against the persistent
    ``browser-bot`` profile. Join: the EXISTING pinned Glofox schedule read,
    joined by exact ``America/New_York`` calendar date. Every missing or
    ambiguous step is a terminal failure.
    """
    # 1. Exact structured request — no caller-controlled surface.
    if task_text != LEVEL6_WEEKLY_REQUEST:
        _level6_fail_closed(
            ctx, "level6.weekly",
            f"unsupported level6 request {task_text!r}", chat_id, send
        )
        return

    # 2. Owner-scoped bound Bot identity (same bar as the Glofox task).
    bot_id = str(getattr(ctx, "bot_id", "") or "").strip()
    owner = str(getattr(ctx, "bot_owner", "") or "").strip()
    if not owner or not bot_id or bot_id == "level6":
        _level6_fail_closed(
            ctx, "level6.weekly",
            "Level 6 weekly runs only for a bound Bot with an owner",
            chat_id, send
        )
        return

    # 3. The dedicated fail-closed policy grant. Evaluated BEFORE any browser
    # work, so an ungrantable identity never reaches the host.
    if not level6_weekly_granted(ctx.policy):
        try:
            audit.log(
                bot_id=ctx.bot_id,
                operation="level6.weekly",
                tier="n/a",
                decision="deny",
                outcome="blocked",
                detail={"reason": "no exact Level 6 Weekly grant"},
            )
        except Exception as exc:
            print(f"[serve] audit log failure: {exc}", file=sys.stderr)
        send(chat_id, "⚠️ Level 6 weekly denied: no exact Level 6 Weekly grant")
        return

    # 4. Run the command in-process: capture on the bound Browser Host with
    # the persistent browser-bot profile, parse, and join with the pinned
    # Glofox read. The connector is read-only and pinned; every failure mode
    # is a Level6Error/GlofoxError, which fails closed. Lazy imports: an
    # import failure is terminal, never a fallback.
    try:
        import level6_weekly as _level6
    except Exception as exc:  # noqa: BLE001
        _level6_fail_closed(
            ctx, "level6.weekly", f"command unavailable: {exc}", chat_id, send
        )
        return
    try:
        import glofox_api as _glofox
    except Exception as exc:  # noqa: BLE001
        _level6_fail_closed(
            ctx, "level6.weekly", f"connector unavailable: {exc}", chat_id, send
        )
        return

    try:
        lines = _level6.run_weekly(
            dispatch=lambda text: _level6_browser_dispatch(
                ctx, text, on_progress=on_progress
            ),
            # Read the EXACT six dates parsed from the validated post — never
            # the connector's own clock-driven "next week" window. The dates
            # come only from the post; nothing caller-supplied reaches here.
            glofox_read=_glofox._week_0830_classes_for_dates,
        )
    except Exception as exc:  # noqa: BLE001 — every failure fails closed
        _level6_fail_closed(
            ctx, "level6.weekly", f"{type(exc).__name__}: {exc}", chat_id, send
        )
        return

    if not lines:
        _level6_fail_closed(
            ctx, "level6.weekly", "the command produced no weekly lines",
            chat_id, send
        )
        return

    relay = "\n".join(lines)
    if len(relay) > _GLOFOX_RESULT_CHAR_LIMIT:
        relay = relay[:_GLOFOX_RESULT_CHAR_LIMIT] + " … [truncated]"
    message = "🏋️ Level 6 — THE WEEKLY SIX:\n" + relay
    try:
        audit.log(
            bot_id=ctx.bot_id,
            operation="level6.weekly",
            tier="tier0",
            decision="allow",
            outcome="auto",
            detail={"lines": len(lines)},
        )
    except Exception as exc:
        print(f"[serve] audit log failure: {exc}", file=sys.stderr)
    if on_result is not None:
        try:
            on_result({
                "status": "no_changes",
                "final_response": message,
                "lines": lines,
                "count": len(lines),
            })
        except Exception as exc:
            print(f"[serve] level6 on_result failure: {exc}", file=sys.stderr)
    send(chat_id, message)


def browser_session_for(ctx: "ExecutionContext"):
    """Reuse/create the managed browser session for a bound Bot.

    Returns ``(session, reused)`` or ``(None, False)`` when no session may
    exist for this context. A session requires BOTH an owner and a bot id
    (the isolation key); an unbound or ownerless context gets none, and the
    executor then falls back to its per-run directory. Every failure — a
    missing module, an unconfigured encryption secret, a registry fault —
    degrades to "no managed session" rather than failing the task: lifecycle
    is an enhancement, never a precondition for running an operation.
    """
    if ctx is None or getattr(ctx, "rift_path", None) is None:
        return None, False
    owner = str(getattr(ctx, "bot_owner", "") or "").strip()
    bot_id = str(getattr(ctx, "bot_id", "") or "").strip()
    if not owner or not bot_id:
        return None, False
    try:
        import browser_sessions as _sessions
        return _sessions.get_or_create(owner, bot_id)
    except Exception as exc:  # noqa: BLE001 — never fail a task on lifecycle
        print(f"[serve] browser session unavailable: {exc}", file=sys.stderr)
        return None, False


def browser_session_detach(ctx: "ExecutionContext") -> None:
    """Mark a bound Bot's session ``disconnected`` after its task finishes.

    Detaching — never ending — is what makes reconnect work: the browser
    record and any parked approval survive the UI going away.
    """
    owner = str(getattr(ctx, "bot_owner", "") or "").strip()
    bot_id = str(getattr(ctx, "bot_id", "") or "").strip()
    if not owner or not bot_id:
        return
    try:
        import browser_sessions as _sessions
        _sessions.mark_disconnected(owner, bot_id)
    except Exception:
        pass


def browser_host_dispatch(ctx: "ExecutionContext", task_text: str, *,
                          session_id: str = "", on_progress=None,
                          profile_bot_id=None):
    """Dispatch a browser task to the Bot's EXPLICITLY bound Browser Host.

    Returns ``(result, error)``. ``error`` is ``None`` ONLY on success; EVERY
    other outcome is a FAIL-CLOSED error string, because a production browser
    task has NO local executor:

      * the context carries no owner/bot id (a fault),
      * the Browser Host registry cannot be imported or read (a fault),
      * the Bot has NO explicit binding, or its binding is revoked/invalid,
      * the bound host is offline/unavailable, or the Cloud's own allowlist
        preflight rejects the task.

    There is deliberately NO implicit single-host fallback and NO local
    fallback. Dispatch happens only when an EXPLICIT binding exists
    (``browser_hosts.binding_for``), and the channel resolves the host with
    ``host_for(owner, bot_id)`` — which returns ``None`` for an unbound Bot
    rather than guessing an owner's only host. Cloud policy, the Bot allowlist,
    host-side intersection, managed sessions, approvals, and audit are all
    preserved because the task flows through the SAME channel the host uses.
    ``profile_bot_id`` defaults to the authorization Bot id. Its sole split
    use is the fixed Level 6 route, and the channel independently rejects any
    other split identity before host dispatch.
    """
    owner = str(getattr(ctx, "bot_owner", "") or "").strip()
    bot_id = str(getattr(ctx, "bot_id", "") or "").strip()
    profile_bot_id = str(profile_bot_id or bot_id).strip()
    if not owner or not bot_id:
        return None, ("a browser task requires an owner-scoped Browser Bot "
                      "binding (no Bot owner/id in context)")
    try:
        import browser_hosts as _hosts
    except Exception as exc:  # noqa: BLE001 — fail closed, never local
        return None, f"browser host registry unavailable: {exc}"
    try:
        bound_host = _hosts.binding_for(owner, profile_bot_id)
    except Exception as exc:  # noqa: BLE001 — fail closed, never local
        return None, f"browser host registry fault: {exc}"
    if not bound_host:
        return None, (f"no Browser Host is bound to Bot {profile_bot_id!r} — bind one "
                      "before running a browser task")
    try:
        import browser_host_channel as _channel
        import browser_host_bridge as _bridge
        result, error = _bridge.request_browser_dispatch(
            owner=owner, bot_id=bot_id, host_id=bound_host,
            task_text=task_text, session_id=session_id,
            on_progress=on_progress, profile_bot_id=profile_bot_id,
        )
        # The bridge owns the topology decision: when THIS process owns a
        # live channel it dispatches synchronously through the SAME
        # HostManager path; the worker process (no channels) creates the
        # durable request and waits for the socket-owning process's terminal
        # result. ``error`` is None only on success; every other outcome is a
        # fail-closed string (accurate HostUnavailable, or the explicit
        # BrowserChannelUnavailable topology error). No local fallback.
        return result, error
    except Exception as exc:  # noqa: BLE001 — fail closed, never local
        return None, f"{type(exc).__name__}: {exc}"


def build_context(
    session_key: str,
    executor_prefix: str = "repo",
    allow_bot_resolution: bool = True,
) -> ExecutionContext:
    """Build an :class:`ExecutionContext` for a task.

    When a Bot is bound to *session_key*, the context is populated from that
    Bot's registry entry (rift, policy, id).  When the session is unbound
    (no Bot matches, or the registry is corrupt/unloadable), the context
    carries ``rift_path=None``, an empty policy, and ``bot_id`` set to
    *executor_prefix* so the audit trail is still populated with a meaningful
    identifier.

    ``allow_bot_resolution=False`` skips the Bot registry entirely and always
    yields the unbound context.  Web-submitted tasks use this: a web task's
    ``session_key`` is the operator's GitHub username, and merely matching a
    registered Bot's id must never bind a web task to that Bot's Rift,
    policy, or identity.  Telegram/bot sessions keep the default (registry
    lookup) so bound-Bot behaviour is unchanged.

    Never raises.  A registry load failure is handled inside ``resolve_bot``
    and produces an unbound context.
    """
    bot = resolve_bot(session_key) if allow_bot_resolution else None
    if bot is not None:
        ctx = ExecutionContext(
            session_id=session_key,
            rift_path=bot.get("rift"),
            policy=bot.get("policy", {}),
            bot_id=bot.get("id", executor_prefix),
            bot_owner=str(bot.get("owner") or "").strip(),
            browser_allowlist=bot_browser_allowlist(bot),
            model=str(bot.get("model") or "").strip(),
            system_prompt=str(bot.get("system_prompt") or "").strip(),
        )
        # Per-Bot LLM configuration. A Bot that references a provider profile
        # is served ONLY with that profile's provider/base URL/key/headers —
        # never the host's global provider. A reference that cannot be
        # resolved records llm_error so the executor fails closed instead of
        # silently running on the globals.
        try:
            llm = _bot_llm_config(bot)
        except Exception as exc:  # noqa: BLE001 — any resolution fault fails closed
            ctx.llm_error = str(exc)
        else:
            if llm:
                ctx.llm_provider = str(llm.get("provider") or "")
                ctx.llm_base_url = str(llm.get("base_url") or "")
                ctx.llm_api_key = str(llm.get("api_key") or "")
                ctx.llm_headers = dict(llm.get("headers") or {})
                ctx.llm_profile_id = str(llm.get("profile_id") or "")
        return ctx
    return ExecutionContext(
        session_id=session_key,
        rift_path=None,
        policy=dict(UNBOUND_POLICY),
        bot_id=executor_prefix,
    )


def _bot_llm_config(bot: dict):
    """Resolve a bound Bot's provider-profile config, or ``None``.

    Returns ``{provider, base_url, api_key, headers, model, profile_id}`` when
    the Bot references a resolvable provider profile (owner-scoped), or
    ``None`` when the Bot has NO profile reference — a legacy/unconfigured
    Bot keeps the executor's existing environment behaviour. Raises for a Bot
    that DOES reference a profile which cannot be resolved (missing, another
    owner's, model not in the profile, or no key) so the caller can fail
    closed.

    Lazy import: the web backend owns the encrypted profile store and the
    resolver; it is added to sys.path on demand so serve.py stays importable
    in deployments without the web backend present.
    """
    profile_id = str((bot or {}).get("provider_profile_id") or "").strip()
    if not profile_id:
        return None
    owner = str((bot or {}).get("owner") or "").strip()
    backend_dir = SCRIPT_DIR / "web" / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    import bot_provider  # noqa: E402 — lazy: web-backend-only dependency
    return bot_provider.resolve_bot_provider(owner, bot)


def apply_bot_identity_env(env: dict, ctx: "ExecutionContext") -> None:
    """Propagate a bound Bot's model and system prompt to the executor env.

    The repo executor (git_workflow -> headless_agent -> core_bridge.py)
    resolves its provider/model from KYREX_PROVIDER / KYREX_MODEL and injects
    the Bot system prompt from KYREX_CHAT_SYSTEM_PROMPT — the same env keys
    the read-only Chat engine path already uses. This keeps a Bot's identity
    (model + prompt) authoritative on the executor path too. Only called for
    a bound Bot (``ctx.rift_path`` set), so unbound/web tasks keep the
    process environment untouched.
    """
    env["KYREX_BOT_ID"] = str(ctx.bot_id or "")
    # Session isolation needs the owner too, so two Bots' browser sessions
    # (or one Bot serving two owners) never share a user-data directory.
    env["KYREX_BOT_OWNER"] = str(getattr(ctx, "bot_owner", "") or "")
    # The Browser Operator enforces this allowlist server-side on every
    # navigation and action. An empty list is delivered as an empty list —
    # the executor, not the env, decides that empty means "deny all".
    allowlist = getattr(ctx, "browser_allowlist", None)
    if allowlist:
        env["KYREX_BROWSER_ALLOWLIST"] = json.dumps(list(allowlist))
    # Per-Bot LLM configuration takes absolute priority. A Bot that references
    # a provider profile runs ONLY with that profile's provider/base URL/key/
    # headers — the KYREX_* globals (and the legacy model-prefix provider) are
    # never consulted for it.
    llm_error = str(getattr(ctx, "llm_error", "") or "").strip()
    llm_key = str(getattr(ctx, "llm_api_key", "") or "").strip()
    model = (ctx.model or "").strip()
    if llm_error:
        # A Bot that references a profile which cannot be resolved must fail
        # closed: strip the global credentials/model so the run reports a
        # clear provider error instead of silently borrowing the host's key.
        env["KYREX_PROVIDER_ERROR"] = llm_error
        env["KYREX_API_KEY"] = ""
        env["KYREX_MODEL"] = ""
        env.pop("KYREX_BASE_URL", None)
        env.pop("OPENAI_BASE_URL", None)
        env.pop("ANTHROPIC_BASE_URL", None)
    elif llm_key:
        llm_provider = (
            str(getattr(ctx, "llm_provider", "") or "").strip().lower() or "openai"
        )
        env["KYREX_PROVIDER"] = llm_provider
        env["KYREX_API_KEY"] = llm_key
        base_url = str(getattr(ctx, "llm_base_url", "") or "").strip()
        if llm_provider == "anthropic":
            if base_url:
                env["ANTHROPIC_BASE_URL"] = base_url
        elif base_url:
            env["KYREX_BASE_URL"] = base_url
            env["OPENAI_BASE_URL"] = base_url
        headers = getattr(ctx, "llm_headers", None) or {}
        if headers:
            env["KYREX_PROVIDER_HEADERS"] = json.dumps(headers)
        if model:
            # The exact configured model is authoritative; a "provider:model"
            # prefix is tolerated but the model name is what runs.
            env["KYREX_MODEL"] = (
                model.partition(":")[2].strip() if ":" in model else model
            )
    elif model:
        if ":" in model:
            provider, _, name = model.partition(":")
            provider = provider.strip().lower()
            name = name.strip()
            if provider:
                env["KYREX_PROVIDER"] = provider
            if name:
                env["KYREX_MODEL"] = name
        else:
            env["KYREX_MODEL"] = model
    system_prompt = (ctx.system_prompt or "").strip()
    if system_prompt:
        env["KYREX_CHAT_SYSTEM_PROMPT"] = system_prompt


# ---------------------------------------------------------------------------
# Engine session isolation — per-conversation durable history.
# ---------------------------------------------------------------------------

def _safe_segment(value, fallback: str = "unknown") -> str:
    """Filesystem-safe single path segment (never '.', '..', or empty)."""
    raw = str(value or "").strip()
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw)
    safe = safe.strip(".") or ""
    return safe[:128] or fallback


def conversation_session_dir(owner, bot_id, conversation_id) -> str:
    """Absolute engine-session directory for one conversation.

    The single isolation key for a conversation's durable engine history:
    ``(owner, bot_id, conversation_id)``. Two conversations bound to the SAME
    Bot resolve to DIFFERENT directories, so a new conversation can never load
    another conversation's messages, files, task state, or loop state — while
    the Bot's shared policy, Rift, provider, and model are untouched.

    The directory lives under the Cloud data root (NOT the Rift), so session
    and audit files never appear as uncommitted files in the Bot's workspace,
    and one workspace never leaks into another conversation's session path.

    The result is deterministic for a given key (stable across worker
    retries/resumes), so a resumed task reloads exactly its own history.
    """
    base = data_dir() / "engine_sessions"
    path = (base
            / _safe_segment(owner, "owner")
            / _safe_segment(bot_id, "bot")
            / _safe_segment(conversation_id, "conversation"))
    return str(path)


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
        return final_response[-600:] if final_response else "(no response)"

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
             send=None, edit=None, session_key=None, task_id=None,
             on_approval=None, on_approval_resolved=None,
             on_result=None, on_progress=None, resolve_bot=True,
             conversation_id=None):
    """Host-side task runner. `send(chat_id, text) -> message_id | None` and
    `edit(chat_id, message_id, text)` are injected by the transport, so this
    module stays free of any Telegram dependency."""
    _skey = str(session_key if session_key is not None else chat_id)
    # Build the execution context once.  When a Bot is bound to this session
    # the context carries its rift, policy, and id.  When unbound the context
    # carries rift_path=None, empty policy, and bot_id set to executor_prefix.
    # resolve_bot=False (web-submitted sessions) skips the Bot registry so a
    # GitHub username that merely equals a Bot id can never bind that Bot.
    ctx = build_context(_skey, executor_prefix, allow_bot_resolution=resolve_bot)
    status_msg_id = None
    progress_lines = []
    last_edit = 0.0
    # Managed browser session for this task (bound Bot browser tasks only).
    # Declared before the try so the finally block can always detach it.
    _browser_session = None
    _browser_session_reused = False

    def maybe_edit():
        nonlocal last_edit
        now = time.monotonic()
        if status_msg_id and now - last_edit > 2.5:  # throttle: Telegram edit rate limits
            last_edit = now
            body = "\n".join(f"  → {p}" for p in progress_lines[-6:])
            edit(chat_id, status_msg_id, f"⏳ Working: {task_text}\n{body}")

    try:
        # Glofox schedule connector — the smallest exact Bot task path. Runs
        # IN-PROCESS (no process spawn): a fixed, structured request with NO
        # caller-controlled URL, branch, method, body, filter, or date.
        # Guards, in order: exact task text; owner-scoped bound Bot identity;
        # an EXACT "glofox:read" policy grant (wildcards never match); live
        # durable-task lifecycle (running/cancelled re-checked before and
        # after the network work); bounded result relay; audit + "⚠️" real
        # terminal errors. Any fault above fails closed.
        if executor_prefix == "glofox":
            _run_glofox_schedule_task(
                ctx, chat_id, task_text, task_id, send,
                on_progress=on_progress,
                on_result=on_result,
            )
            return

        # Level 6 weekly MVP command — the pinned "THE WEEKLY SIX" capture and
        # Glofox join. Runs IN-PROCESS (no process spawn) on the SAME Browser
        # Host path (persistent ``browser-bot`` profile) and the SAME pinned
        # Glofox reader, under its own dedicated fail-closed policy grant.
        if executor_prefix == "level6":
            _run_level6_weekly_task(
                ctx, chat_id, task_text, send,
                on_progress=on_progress,
                on_result=on_result,
            )
            return

        # Calendar Reader -- the three pinned owner-facing commands. Runs
        # IN-PROCESS against the OWNER-SCOPED encrypted connector store (no
        # spawn, no global refresh token): a fixed, read-only window with NO
        # caller-controlled calendar id, scope, provider, or date.
        if executor_prefix == "calendar":
            _run_calendar_read_task(
                ctx, chat_id, task_text, task_id, send,
                on_progress=on_progress,
                on_result=on_result,
            )
            return

        # Browser Operator preflight — enforce the Bot's site/domain allowlist
        # host-side before any process is spawned. The executor re-checks the
        # allowlist on every navigation and action; this outer layer guarantees
        # a blocked task is reported and never starts, rather than silently
        # reaching the browser.
        if executor_prefix == "browser":
            _blocked = browser_preflight_block(ctx, task_text)
            if _blocked:
                # Terminal browser failure. Reported with the SAME "⚠️"
                # convention every other run_task failure uses so task_store
                # captures it as the durable task's final error — a bare status
                # message would leave the task reporting the generic
                # "no result produced by executor".
                send(chat_id, f"⚠️ Browser task blocked: {_blocked}")
                try:
                    audit.log(
                        bot_id=ctx.bot_id,
                        operation="browser.navigate",
                        tier="deny",
                        decision="deny",
                        outcome="blocked",
                        detail={"reason": _blocked},
                    )
                except Exception as exc:
                    print(f"[serve] audit log failure: {exc}", file=sys.stderr)
                return

        # Managed browser session: reuse a live one or start a fresh one for
        # this (owner, bot). The executor receives its directory via env, so
        # the browser profile persists across runs — that persistence IS the
        # reconnect. Nothing here is a secret: the session token never leaves
        # the sealed metadata blob.
        if executor_prefix == "browser":
            _browser_session, _browser_session_reused = browser_session_for(ctx)
            if _browser_session is not None:
                try:
                    audit.log(
                        bot_id=ctx.bot_id,
                        operation="browser.session",
                        tier="n/a",
                        decision="allow",
                        outcome=("reused" if _browser_session_reused
                                 else "created"),
                        detail={"ref": _browser_session.session_id,
                                "state": _browser_session.state},
                    )
                except Exception as exc:
                    print(f"[serve] audit log failure: {exc}", file=sys.stderr)
                if on_progress is not None:
                    try:
                        on_progress({"browser": "managed",
                                     "ref": _browser_session.session_id,
                                     "state": _browser_session.state})
                    except Exception:
                        pass

        # Hosted browser execution: a browser task MUST run on the Bot's
        # explicitly bound Browser Host, through the EXISTING host channel
        # (browser_host_channel.dispatch_browser_task), reusing the Cloud's
        # policy/allowlist/approval authority, the host-side allowlist
        # intersection, managed sessions, and audit. There is NO local browser
        # executor: an unbound Bot, a revoked/invalid binding, a registry fault,
        # or an offline host FAILS CLOSED below and is never run locally. This
        # routes to the one existing browser path; it is not a parallel one.
        if executor_prefix == "browser":
            _host_result, _host_err = browser_host_dispatch(
                ctx, task_text,
                session_id=(_browser_session.session_id
                            if _browser_session is not None else ""),
                on_progress=on_progress,
            )
            status_msg_id = send(chat_id, f"⏳ Starting: {task_text}")
            if _host_err is not None:
                # FAIL CLOSED. There is no local browser executor: an unbound
                # Bot, a revoked/invalid binding, a registry fault, or an
                # offline host is reported and never run locally.
                try:
                    audit.log(
                        bot_id=ctx.bot_id,
                        operation="browser.navigate",
                        tier="deny",
                        decision="deny",
                        outcome="fail_closed",
                        detail={"reason": _host_err},
                    )
                except Exception as exc:
                    print(f"[serve] audit log failure: {exc}", file=sys.stderr)
                # Terminal browser failure → the durable task's final error.
                # "⚠️" is the convention task_store.send_cb captures, so the
                # REAL fail-closed reason is preserved instead of being
                # overwritten by "no result produced by executor".
                send(chat_id, f"⚠️ Browser task failed closed: {_host_err}")
                return
            try:
                if on_result is not None:
                    on_result(_host_result)
            except Exception:
                pass
            send(chat_id, format_result(_host_result))
            return

        # Defense in depth: a production browser task is dispatched or failed
        # closed ABOVE and can never reach a local spawn. This guard makes that
        # structural — Kyrex Cloud retains no local browser executor.
        if executor_prefix == "browser":
            send(chat_id, "⚠️ Browser task failed closed: no local browser "
                          "executor exists")
            return

        status_msg_id = send(chat_id, f"⏳ Starting: {task_text}")

        executor_script = EXECUTORS[executor_prefix]
        # Executors must not know about Bots — they receive an authorised
        # filesystem root or nothing.  When the context has a rift_path
        # it is delivered as KYREX_FS_ROOT, overriding any inherited value.
        # When there is no rift_path the inherited environment is untouched
        # so today's behaviour is unchanged.
        # Fail closed: writable ONLY for our own/default repo, OR for a Bot
        # explicitly authorised to write. Everything else -- allowlisted
        # external, unknown, or unparseable -- is read-only.
        # A Bot-bound rift alone does NOT grant write capability: writable Bot
        # execution requires the explicit fs:write developer grant. A Bot with
        # a rift but no such grant keeps its previous behaviour (writable only
        # for its own repo; external remains read-only). When the Bot IS
        # authorised, its rift is the writable workspace regardless of
        # repo_url -- repo_url may seed an empty rift but must never force it
        # read-only or substitute the user's connected repo for the Bot's own
        # workspace.
        bot_bound_writable = (
            ctx.rift_path is not None
            and is_writable_bot_policy(ctx.policy)
        )
        writable_own = executor_prefix == "repo" and (
            bot_bound_writable or (bool(repo_url) and is_own_repo(repo_url))
        )
        read_only_repo = executor_prefix == "repo" and (
            (ctx.rift_path is not None and not bot_bound_writable)
            or (bool(repo_url) and not writable_own)
        )
        proc_env = None
        if ctx.rift_path is not None or read_only_repo:
            proc_env = os.environ.copy()
            if ctx.rift_path is not None:
                proc_env["KYREX_FS_ROOT"] = ctx.rift_path
            if read_only_repo:
                proc_env.pop("GITHUB_TOKEN", None)
                proc_env["KYREX_READ_ONLY_REPO"] = "1"
            # Per-conversation engine session isolation. The executor
            # (git_workflow -> headless_agent -> core_bridge) inherits this
            # env, so the engine persists/loads its session and reasoning
            # audit under THIS conversation's directory — never the shared
            # Rift. Keyed by (owner, bot_id, conversation_id); when no Chat
            # conversation is supplied (e.g. a Telegram task) the session key
            # is the stable per-session key, so retries still resume exactly
            # their own history and two sessions never share one.
            sess_key = str(conversation_id or _skey)
            proc_env["KYREX_SESSION_DIR"] = conversation_session_dir(
                ctx.bot_owner or chat_id, ctx.bot_id or executor_prefix, sess_key)
            # A bound Bot's identity (model + system prompt) travels with it.
            if ctx.rift_path is not None:
                apply_bot_identity_env(proc_env, ctx)
            # Host-owned managed-session directory. Supplied only to the
            # browser executor for a session that actually exists, so every
            # other executor's environment is untouched.
            if _browser_session is not None and executor_prefix == "browser":
                try:
                    import browser_sessions as _sessions
                    proc_env.update(_sessions.session_env(_browser_session))
                except Exception as exc:  # noqa: BLE001
                    print(f"[serve] browser session env unavailable: {exc}",
                          file=sys.stderr)
        # stderr gets its own pipe. Merging it into stdout let an unbuffered
        # stderr write land mid-line and corrupt the KYREX_RESULT_JSON line —
        # same rule as the engine: nothing but protocol on a protocol channel.
        executor_cmd = [
            sys.executable, str(SCRIPT_DIR / executor_script),
            "--base", BASE_BRANCH,
            "--task", task_text,
        ]
        # repo_url is optional: tasks that do not target a repository (e.g. a
        # plain question) omit it, and the executor is run without --repo-url.
        # When present it is passed through unchanged, preserving existing
        # behaviour for the Bot / web paths.
        if repo_url:
            executor_cmd += ["--repo-url", repo_url]
        # A Bot bound to a persistent Rift hands that Rift to the repo
        # executor explicitly (--rift) so the workspace is reused and never
        # wiped.  This is the repo executor only; other executors keep their
        # existing unbound behaviour and still receive KYREX_FS_ROOT when bound.
        if executor_prefix == "repo" and ctx.rift_path is not None:
            executor_cmd += ["--rift", ctx.rift_path]
        if read_only_repo:
            executor_cmd += ["--read-only"]
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
                        if on_progress is not None:
                            on_progress(note)
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

                    # Host derives the tier purely from the operation
                    # itself (OPERATION_TIERS) plus scope and volume
                    # escalation. The executor-declared tier was stripped
                    # above and is NOT fed back in here — on this path the
                    # host derives alone; the stripped value is only
                    # logged. Unknown ops are denied just below.
                    _is_external_repo = (
                        executor_prefix == "repo" and bool(repo_url)
                        and not is_own_repo(repo_url)
                    )
                    derived_tier = derive_host_tier(
                        colon_op, target,
                        count=op_data.get("count"),
                        is_external=_is_external_repo,
                    )
                    if derived_tier is None:
                        derived_tier = 2  # unrecognised → most cautious

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
                    # For an approved external repo write, append a per-repo
                    # scoped token so the executor can push. Fail closed: no
                    # scoped credential -> plain APPROVE, which the executor's
                    # push-verdict reader refuses (cannot push without a token).
                    _decision_line = host_decision
                    if (host_decision == "APPROVE"
                            and colon_op in ("repo:push", "repo:pr")
                            and _is_external_repo):
                        _scoped = scoped_token_for(repo_url)
                        if _scoped:
                            _decision_line = f"APPROVE {_scoped}"
                    try:
                        proc.stdin.write(f"{_decision_line}\n")
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

                    # A policy *deny* on an approval means the host has no rule
                    # permitting the operation, but the executor still raised an
                    # approval for the operator to decide.  For an *unbound*
                    # session (no Bot / no policy — e.g. the persistent Cloud
                    # task worker running a web-submitted task) the approval must
                    # remain operator-resolvable: a deny-locked approval that can
                    # never be accepted is worse than letting the human decide.
                    # Bound sessions keep their policy-derived tier untouched.
                    # No unbound deny-bypass: an unbound session carries
                    # UNBOUND_POLICY (explicit safe reads), so a bare deny here
                    # means the op is genuinely unauthorised when no human is
                    # being asked.  But the executor raised KYREX_APPROVAL and
                    # the operator IS being asked: a string tier ("deny") would
                    # make every reply (even an unrelated message) resolve the
                    # approval as a blanket DENIED and the operator's "y" could
                    # never approve.  For unbound sessions revert to the
                    # host-derived tier so the pending approval stays
                    # operator-resolvable; bound sessions are untouched.
                    if ctx.rift_path is None and not isinstance(tier, int):
                        effective_tier = derived_tier
                    else:
                        effective_tier = tier
                    if effective_tier == 2:
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
                        "tier": effective_tier,
                        "token": token,
                        "result": None,
                    }
                    # Surface the approval in the persistent task store when a
                    # store-backed caller supplied the hooks.  This runs after
                    # the in-memory pending entry exists so the cancel-at-
                    # approval path can resolve it immediately.  Report the
                    # EFFECTIVE tier (the one the operator actually faces, and
                    # the one stored in the pending entry): for unbound
                    # sessions the raw policy tier is the string "deny", which
                    # would make the cancel-at-approval deny text empty and
                    # the immediate cancellation unresolvable.
                    if on_approval is not None:
                        on_approval(approval_msg_id, effective_tier, token,
                                    summary, detail)
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

                    # Persist the approval resolution to the store (if hooked).
                    if on_approval_resolved is not None:
                        on_approval_resolved(approval_msg_id, decision)

                    try:
                        proc.stdin.write(f"{decision}\n")
                        proc.stdin.flush()
                    except BrokenPipeError:
                        # Executor already exited — nothing to write.
                        pass

                elif line.startswith("KYREX_RESULT_JSON:"):
                    try:
                        result_json = json.loads(line[len("KYREX_RESULT_JSON:"):])
                        if on_result is not None:
                            on_result(result_json)
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
        # Detach (never end) the managed browser session: the record and any
        # parked approval must outlive this task so a reconnect finds them.
        if _browser_session is not None:
            browser_session_detach(ctx)
        # Release the per-session lock only if it is actually held.  When
        # run_task is invoked directly by the persistent CloudTaskStore
        # worker (rather than via launch(), which acquires the lock in the
        # caller's thread), no one holds this lock, and an unconditional
        # release would raise RuntimeError.  When launch() did acquire it,
        # the lock is held and this releases it as before.
        _slock = session_lock(_skey)
        if _slock.locked():
            _slock.release()


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
