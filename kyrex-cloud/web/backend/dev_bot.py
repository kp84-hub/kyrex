"""dev_bot.py — Grok-style developer-Bot routing (Kyrex Chat -> executor path).

First increment toward a coding Bot. This module is deliberately small and
self-contained: it adds NO new execution framework and NO new policy engine.
It provides two things:

  1. ``is_writable_bot_policy`` — the single gate that decides whether a Bot
     is permitted OFF the read-only Chat engine session and onto the existing
     executor path. A Bot is writable iff its policy explicitly grants the
     developer write operation ``fs:write`` (the operation behind
     ``edit_file`` / ``write_file_with_gate``) using the EXISTING host tier
     table (``serve.OPERATION_TIERS``) and policy evaluator
     (``policy.evaluate``). Empty, deny-only, read-only, or malformed
     policies are read-only (fail closed).

  2. ``submit_bot_task`` — enqueues a Bot-bound coding task on the EXISTING
     CloudTaskStore so the worker executes it through the EXISTING
     ``serve.run_task`` -> ``git_workflow.py --rift`` -> ``headless_agent.py``
     path. The task is bound to the Bot (session_key == bot id,
     resolve_bot=True) and carries ``repo_url=None`` so the Bot's own rift —
     never the user's connected repo — is the workspace. Submission is gated
     on the Bot's lifecycle (``bots.is_running``): a paused/stopped Bot rejects
     new task work (an already-submitted task is unaffected by a later status
     change — the shared worker never re-checks status at claim time).

Non-Bot Chat and read-only Bots keep the existing read-only EngineSession
path unchanged; this module is only a gate + entry point for the writable
developer path, and it never auto-approves anything.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

# kyrex-cloud/web/backend/dev_bot.py sits inside kyrex-cloud/ — resolve the
# Cloud package the same way chat_service.py does so the EXISTING policy
# engine and host tier table are reused.
_SCRIPT_DIR = Path(__file__).resolve().parent            # web/backend/
_CLOUD_DIR = _SCRIPT_DIR.parent.parent                   # kyrex-cloud/
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

import serve as _serve  # noqa: E402  — host tier table + the writable-Bot gate
import bots as _bots  # noqa: E402  — the authoritative Bot lifecycle gate
from git_workflow import is_git_repo as _is_git_repo  # noqa: E402


class DevBotError(Exception):
    """The requested Bot cannot be routed to the writable executor path."""


# The single writable-Bot gate lives in serve.py so serve.run_task (the
# executor path) and submit_bot_task (this Chat entry point) apply the SAME
# decision (single source of truth). Re-exported here for callers.
is_writable_bot_policy = _serve.is_writable_bot_policy

# The named Developer preset and its derived effective permissions are owned
# by serve.py (next to the gate). Re-exported here so the Chat API and UI have
# one import for "what makes a Developer Bot".
DEVELOPER_PRESET_ID = _serve.DEVELOPER_PRESET_ID
DEVELOPER_PRESET_LABEL = _serve.DEVELOPER_PRESET_LABEL
DEVELOPER_PRESET = _serve.DEVELOPER_PRESET
developer_preset_policy = _serve.developer_preset_policy
effective_permissions = _serve.effective_permissions
validate_bot_policy = _serve.validate_bot_policy

# The named Browser preset — the read-only Browser Bot grant. Owned by serve.py
# (next to the browser gate) and re-exported here so the Chat API and UI have
# one import for "what makes a Browser Bot". The preset grants ONLY browser
# navigation + page reading; every interaction/write op stays denied.
BROWSER_PRESET_ID = _serve.BROWSER_PRESET_ID
BROWSER_PRESET_LABEL = _serve.BROWSER_PRESET_LABEL
BROWSER_PRESET = _serve.BROWSER_PRESET
browser_preset_policy = _serve.browser_preset_policy
is_browser_bot_policy = _serve.is_browser_bot_policy

# The named Glofox Reader preset — the least-privilege schedule read grant.
# Owned by serve.py (next to the Glofox-Reader gate) and re-exported here so
# the Chat API and UI have one import for "what makes a Glofox Reader". The
# preset grants ONLY the pinned ``glofox:read``; every browser, write, delete,
# push, mail, calendar, and coordination op stays denied.
GLOFOX_READER_PRESET_ID = _serve.GLOFOX_READER_PRESET_ID
GLOFOX_READER_PRESET_LABEL = _serve.GLOFOX_READER_PRESET_LABEL
GLOFOX_READER_PRESET = _serve.GLOFOX_READER_PRESET
glofox_reader_preset_policy = _serve.glofox_reader_preset_policy
is_glofox_reader_policy = _serve.is_glofox_reader_policy
is_glofox_reader_bot = _serve.glofox_reader_granted

# The named Level 6 Weekly preset — the dedicated fail-closed grant for the
# pinned ``level6: weekly`` command. Owned by serve.py (next to the exact-grant
# gate) and re-exported here so the Chat API and UI have one import for "what
# makes a Level 6 Weekly Bot". The preset grants EXACTLY the four read-only
# operations the command performs (browser navigate/read/screenshot + the pinned
# glofox:read) and NOTHING else: it is neither the Browser preset nor the Glofox
# Reader preset, and NEITHER existing preset is widened.
LEVEL6_WEEKLY_PRESET_ID = _serve.LEVEL6_WEEKLY_PRESET_ID
LEVEL6_WEEKLY_PRESET_LABEL = _serve.LEVEL6_WEEKLY_PRESET_LABEL
LEVEL6_WEEKLY_PRESET = _serve.LEVEL6_WEEKLY_PRESET
level6_weekly_preset_policy = _serve.level6_weekly_preset_policy
level6_weekly_preset_allowlist = _serve.level6_weekly_preset_allowlist
is_level6_weekly_policy = _serve.level6_weekly_granted
level6_browser_bot_id = _serve.level6_browser_bot_id


def rift_is_repo(rift) -> bool:
    """True iff *rift* is an absolute path to an existing git repository.

    A Bot's Rift must be a real repository workspace — never an empty or
    arbitrary directory. Fail closed on anything else.
    """
    raw = str(rift or "").strip()
    if not raw:
        return False
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        return False
    return _is_git_repo(path)


def validate_developer_rift(bot) -> None:
    """Raise DevBotError unless *bot*'s Rift is a real git repository.

    Called before a Bot is made writable: a Developer Bot runs arbitrary
    coding tasks against its Rift, so the workspace must be an actual
    repository. An empty directory, a missing path, or a non-repo directory
    are all rejected with a clear message — nothing is silently accepted or
    auto-cloned here.
    """
    bot = bot or {}
    bot_id = str(bot.get("id") or "").strip()
    rift = str(bot.get("rift") or "").strip()
    if not rift:
        raise DevBotError(f"bot {bot_id!r} has no rift")
    path = Path(rift)
    if not path.is_absolute():
        raise DevBotError(f"bot {bot_id!r} rift {rift!r} is not an absolute path")
    if not path.is_dir():
        raise DevBotError(f"bot {bot_id!r} rift {rift!r} is not an existing directory")
    if not _is_git_repo(path):
        raise DevBotError(
            f"bot {bot_id!r} rift {rift!r} is not a git repository — a Developer "
            "Bot requires a real Kyrex repository workspace, not an empty or "
            "arbitrary directory"
        )


def submit_bot_task(user, bot, task_text, store=None, conversation_id=None):
    """Enqueue a Bot-bound coding task on the existing CloudTaskStore.

    Refuses (fail closed) when the Bot is not RUNNING (no new work for a
    paused/stopped Bot) or is not writable, so a read-only or stopped Bot can
    never reach the writable executor path. The task is bound to the Bot's own
    rift: ``session_key`` is the Bot id, ``resolve_bot=True``, and ``repo_url``
    is ``None`` — the user's connected repo is never referenced.

    *conversation_id* is the durable Chat conversation identity (independent
    of ``session_key``, which stays the per-Bot id). It is recorded on the
    task so the executor gives the engine a per-conversation session
    directory: two conversations bound to the same Bot must never share
    engine history, while the Bot's shared Rift/policy/model are unchanged.

    A task accepted here is durable: a LATER status change (pause/stop) does
    not cancel or block it — the worker never re-checks Bot status at claim
    time, exactly because a status is a work-eligibility label on a shared
    worker, not a process handle.
    """
    bot = bot or {}
    bot_id = str(bot.get("id") or "").strip()
    rift = str(bot.get("rift") or "").strip()
    task_text = str(task_text or "").strip()
    if not bot_id:
        raise DevBotError("bot id is required")
    if not rift:
        raise DevBotError(f"bot {bot_id!r} has no rift")
    if not task_text:
        raise DevBotError("task_text is required")
    # Lifecycle gate (server-side, authoritative): only a running Bot accepts
    # new task submissions. Paused/stopped reject new work.
    if not _bots.is_running(bot):
        raise DevBotError(
            f"bot {bot_id!r} is {bot.get('status') or _bots.STATUS_STOPPED} — "
            "start it before submitting tasks"
        )
    if not is_writable_bot_policy(bot.get("policy")):
        raise DevBotError(f"bot {bot_id!r} is read-only")

    from task_store import CloudTaskStore  # local import, no hard dependency

    if store is None:
        store = CloudTaskStore()

    return store.submit(
        session_key=bot_id,
        task_text=task_text,
        repo_url=None,
        executor_prefix="repo",
        bot_id=bot_id,
        rift=rift,
        chat_id=str(user or ""),
        resolve_bot=True,
        # The per-conversation isolation key (None for non-Chat callers, which
        # then fall back to the session key in serve.run_task).
        conversation_id=(str(conversation_id).strip() or None
                         if conversation_id else None),
    )


# ═════════════════════════════════════════════════════════════════════════
# Browser Bot bridge — structured read-only browser task submission
# ═════════════════════════════════════════════════════════════════════════
#
# The bearer of the durable browser path (submit_bot_task) is the writable
# repo executor; a Browser Bot is a DIFFERENT kind of bound Bot. It owns a
# non-empty browser domain allowlist and an explicit host binding, and its
# chat must reach the EXISTING `serve.run_task(executor_prefix="browser")`
# path (browser_preflight_block -> browser_session_for ->
# browser_host_dispatch -> browser_host_channel) rather than the engine
# session (which has no browser tool — never has, never will).
#
# Safety boundaries enforced HERE, before any task is created:
#   * Only `navigate` and `read` steps (tier-0 browser operations).
#   * URL shape, scheme, and userinfo rejected by the bounded step validator
#     below (http(s) with a host; no embedded credentials).
#   * Task text is COMPILED from validated steps (the raw user text is never
#     executed).
#
# The bounded validator below is DELIBERATELY self-contained: the committed
# bridge must never import or require the separately developed ``routines``
# module at runtime. It implements ONLY the minimal navigate/read request +
# step validation the bridge needs — no routines schema, storage, coordinator
# steps, or API — so a clean checkout of this commit serves Browser Bot
# requests with routines.py entirely absent.
#   * Empty allowlist is the deny-every-navigation default and rejects.
#   * An fs:write-capable Bot is not a Browser Bot: write-capable routing
#     stays on the repo path and rejects here (never both).
#   * An explicit host binding must exist; missing/revoked/offline is
#     reported here and/or fail-closed in run_task.

# Deliberately explicit, safe first UX: only these two verb forms are
# understood. Anything else is ambiguous and answered with a usage message
# rather than guessed at.
_BROWSER_REQUEST_RE = re.compile(
    r"^\s*(?P<verb>read|browse)\s+(?P<target>\S+)\s*$", re.IGNORECASE)

_BROWSER_USAGE = (
    "I only accept explicit read-only browser requests:\n"
    "  read https://allowed-domain/page   # fetch and show page text\n"
    "  browse https://allowed-domain/     # navigate, then read\n"
    "One URL per request; the domain must be on my allowlist. "
    "Free-form requests (log in, click, submit, download, …) are not supported."
)


# ── bounded browser step validation (self-contained) ───────────────────────
# A Browser Bot accepts ONLY an ordered, bounded list of read-only browser
# steps — ``navigate`` and ``read``. This is the minimal validation the
# bridge needs and nothing more: it rejects write-capable / unknown actions
# and malformed steps BEFORE any task row is written, bounds the step count,
# URL length, and per-step field set, and compiles the ordered steps into the
# EXISTING browser executor's task protocol text (``{"actions": [...]}``).
# It lives here, inside the committed Browser bridge boundary, so the bridge
# has NO runtime dependency on the separately developed ``routines`` module
# (which is not part of this commit).
_BROWSER_MAX_STEPS = 12
_BROWSER_MAX_URL_LEN = 2048
_BROWSER_STEP_ACTIONS = ("navigate", "read")
_BROWSER_WRITE_CAPABLE_ACTIONS = (
    "click", "type", "upload", "download", "submit", "delete",
)
_BROWSER_STEP_KEYS = {
    "navigate": frozenset({"action", "url"}),
    "read": frozenset({"action"}),
}
_BROWSER_URL_SCHEME_RE = re.compile(r"^https?$", re.IGNORECASE)


class BrowserStepError(Exception):
    """A browser step list is not a bounded, read-only navigate/read spec."""


def _validate_browser_step(index: int, step) -> dict:
    """Validate ONE browser step; return its normalized form. Raises on excess."""
    if not isinstance(step, dict):
        raise BrowserStepError(f"step {index} must be an object")
    action = str(step.get("action") or "").strip().lower()
    if action not in _BROWSER_STEP_ACTIONS:
        if action in _BROWSER_WRITE_CAPABLE_ACTIONS:
            raise BrowserStepError(
                f"step {index} ({action!r}) is a write-capable browser action "
                "— a Browser Bot accepts read-only steps only")
        raise BrowserStepError(
            f"unsupported step type {action!r} at index {index}; "
            f"supported: {list(_BROWSER_STEP_ACTIONS)}")

    allowed = _BROWSER_STEP_KEYS[action]
    unknown = [k for k in step if k not in allowed]
    if unknown:
        raise BrowserStepError(
            f"step {index} ({action!r}) has unsupported field(s) {unknown}")

    normalized: dict = {"action": action}
    if action == "navigate":
        url = str(step.get("url") or "").strip()
        if not url:
            raise BrowserStepError(f"step {index} (navigate) requires a url")
        if len(url) > _BROWSER_MAX_URL_LEN:
            raise BrowserStepError(
                f"step {index} url exceeds {_BROWSER_MAX_URL_LEN} chars")
        try:
            parts = urlsplit(url)
        except ValueError:
            raise BrowserStepError(
                f"step {index} url is not parseable") from None
        if not _BROWSER_URL_SCHEME_RE.match(parts.scheme or ""):
            raise BrowserStepError(
                f"step {index} url must be http(s) with a host — and must "
                "never carry credentials")
        if parts.username or parts.password or "@" in (parts.netloc or ""):
            raise BrowserStepError(
                f"step {index} url must not carry credentials (userinfo)")
        if not parts.hostname:
            raise BrowserStepError(f"step {index} url has no host")
        normalized["url"] = url
    return normalized


def validate_browser_steps(steps) -> list[dict]:
    """Validate an ordered browser step list, preserving order. Raises on excess.

    Bounded, read-only, and self-contained: only ``navigate`` and ``read``
    steps validate; the step count, URL length, and per-step field set are
    bounded. Write-capable or unknown actions are rejected before anything is
    persisted.
    """
    if not isinstance(steps, (list, tuple)):
        raise BrowserStepError("steps must be a list")
    if not steps:
        raise BrowserStepError("a browser task requires at least one step")
    if len(steps) > _BROWSER_MAX_STEPS:
        raise BrowserStepError(
            f"a browser task allows at most {_BROWSER_MAX_STEPS} steps")
    return [_validate_browser_step(i, s) for i, s in enumerate(steps)]


def browser_task_text_for(steps) -> str:
    """Compile ordered navigate/read steps into the EXISTING browser-task
    protocol text (``{"actions": [...]}``).

    Returns "" when no browser-executable step exists. Self-contained: it
    depends on nothing outside this module.
    """
    actions = []
    for step in steps:
        if step["action"] == "navigate":
            actions.append({"action": "navigate", "url": step["url"]})
        elif step["action"] == "read":
            actions.append({"action": "read"})
        else:
            break
    if not actions:
        return ""
    return json.dumps({"actions": actions}, ensure_ascii=False)


def parse_browser_request(text) -> list[dict]:
    """Parse `read <url>` / `browse <url>` into validated steps.

    Returns the ordered [navigate, read] step list, or raises DevBotError
    with the short usage message for anything ambiguous or unsupported.
    A bare host is normalised to https://<host>. Credential-bearing,
    non-http(s), or unparseable targets are rejected.
    """
    raw = str(text or "").strip()
    m = _BROWSER_REQUEST_RE.match(raw)
    if not m:
        raise DevBotError(
            "ambiguous browser request — a Browser Bot accepts only "
            "`read <url>` or `browse <url>`\n" + _BROWSER_USAGE)
    target = m.group("target")
    if target.count("/") == 0 and ":" not in target:
        target = f"https://{target}"          # bare host convenience
    parts = urlsplit(target)
    if (parts.scheme or "").lower() not in ("http", "https"):
        raise DevBotError("only http(s) URLs are accepted\n" + _BROWSER_USAGE)
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise DevBotError("URLs must not carry credentials\n" + _BROWSER_USAGE)
    if " " in target or target != target.strip() or not parts.hostname:
        raise DevBotError("that does not look like one URL\n" + _BROWSER_USAGE)
    try:
        return validate_browser_steps(
            [{"action": "navigate", "url": target}, {"action": "read"}])
    except BrowserStepError as exc:
        raise DevBotError(f"{exc}\n{_BROWSER_USAGE}") from exc


def _host_allowed(browser_allowlist, url) -> bool:
    """The URL's hostname against the Bot's (non-empty) allowlist.

    Matches an allowlist entry exactly, or a subdomain of one. Fail closed:
    an empty/broken allowlist rejects every URL.
    """
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").strip().lower()
    if not host:
        return False
    for entry in browser_allowlist or []:
        entry = str(entry or "").strip().lower()
        if not entry:
            continue
        if host == entry or host.endswith("." + entry):
            return True
    return False


def browser_route_ready(bot) -> bool:
    """True when a Bot is EXPLICITLY bound to a Browser Host AND has a
    non-empty allowlist AND is NOT write-capable.

    A Glofox Reader (the exact least-privilege ``glofox:read`` grant) is NEVER
    browser-route-ready: its policy has no browser surface, so it can never
    compete with — or be promoted above — the pinned glofox route by later
    adding an allowlist or a host binding. This keeps the Glofox Reader route
    exclusive even if the registry is mutated outside the preset API.

    This is the Chat-side half of the three-way route in chat_service; the
    authoritative checks re-run on execution (serve preflight + host
    channel), so this can only approve — never widen — that path.
    """
    bot = bot or {}
    try:
        if is_writable_bot_policy(bot.get("policy")):
            return False                      # writable route, exactly once
        if _serve.is_glofox_reader_policy(bot.get("policy")):
            return False                      # Glofox Reader never routes browser
    except Exception:
        return False
    raw = bot.get("browser_allowlist")
    if not isinstance(raw, list) or not any(
            isinstance(h, str) and h.strip() for h in raw):
        return False
    try:
        import browser_hosts as _bh
        return bool(_bh.binding_for(bot.get("owner"), bot.get("id")))
    except Exception:
        return False                         # registry fault = no route


def browser_bot_ready(bot) -> bool:
    """The single "is this a runnable least-privilege Browser Bot?" predicate.

    True iff the Bot's POLICY is exactly the read-only browser grant (grants
    navigate + read, and NO interaction/write/coordination op) AND
    :func:`browser_route_ready` holds (non-empty allowlist, explicit Browser
    Host binding, not write-capable). The Chat API badge and the Chat roster
    both read THIS, so the badge never drifts from the capability + eligibility
    rules the executor enforces. Fail closed on any exception.
    """
    bot = bot or {}
    try:
        if not is_browser_bot_policy(bot.get("policy")):
            return False
        return browser_route_ready(bot)
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════
# Glofox schedule bridge — ONE pinned owner-facing command
# ═════════════════════════════════════════════════════════════════════════
#
# The Level 6 schedule read is a single, server-defined Chat command:
#     glofox: schedule
# (already the exact text serve.resolve_executor maps to the glofox
# executor). There is NO caller-controlled URL, branch, method, body,
# filter, pagination, or date — everything is pinned inside glofox_api.
#
# Safety boundaries enforced HERE, before any task row is created:
#   * The task text is ONLY the fixed pinned request (a different text
#     rejects; nothing is guessed, compiled, or forwarded).
#   * The Bot must be RUNNING (like every durable submission).
#   * The Bot's policy must grant EXACTLY "glofox:read" (tier 0) via the
#     shared host predicate — wildcards never count.
#   * A write-capable Bot (fs:write) NEVER routes here: repo path only.
#   * No allowlist, no Browser Host binding, no browser allowlist check:
#     there is no navigation — the submission is only the pinned read.

def glofox_route_ready(bot) -> bool:
    """True when a bound Bot may receive the pinned `glofox: schedule`
    command through Chat: RUNNING, not write-capable, and holding the
    EXACT server-defined ``glofox:read`` read-tier grant.

    The authoritative checks re-run inside `serve.run_task`'s glofox
    branch (policy → identity → lifecycle → connector), so route readiness
    can only approve — never widen — that path.
    """
    bot = bot or {}
    try:
        if not _bots.is_running(bot):
            return False
        if is_writable_bot_policy(bot.get("policy")):
            return False                    # write-capable routes to repo
        return _serve.glofox_read_granted(bot.get("policy"))
    except Exception:
        return False                        # any fault = no route


GLOFOX_SCHEDULE_COMMAND = _serve.GLOFOX_TASK_TEXT


def submit_glofox_task(user, bot, task_text, store=None, conversation_id=None):
    """Enqueue a Bot-bound Level 6 schedule read on the existing
    CloudTaskStore, executed through `serve.run_task`'s IN-PROCESS glofox
    branch (no process spawn, no browser host, no rift).

    This is the exact owner-facing command — the same `glofox: schedule`
    task text the Telegram path and the Routine submission produce. The
    ONLY permitted value of *task_text* is that pinned string; anything
    else fails closed BEFORE any task is written.
    """
    bot = bot or {}
    bot_id = str(bot.get("id") or "").strip()
    owner = str(bot.get("owner") or "").strip()
    text = str(task_text or "").strip()
    if not bot_id or not owner:
        raise DevBotError("bot id and owner are required")
    # Owner scoping: only the OWNER may submit for their Bot. An unowned Bot
    # (owner == "") still fails closed here — unlike the visible-roster rule,
    # the glofox read requires a real owner-scoped identity (serve.run_task
    # re-checks it too). A foreign owner is refused before any task row.
    if owner != str(user or "").strip():
        raise DevBotError(
            f"bot {bot_id!r} belongs to another owner — fail closed")
    if text != GLOFOX_SCHEDULE_COMMAND:
        raise DevBotError(
            f"unsupported Glofox request {text!r}; the only accepted "
            f"request is {GLOFOX_SCHEDULE_COMMAND!r}")
    if not _bots.is_running(bot):
        raise DevBotError(
            f"bot {bot_id!r} is {bot.get('status') or _bots.STATUS_STOPPED} — "
            "start it before submitting tasks")
    try:
        if is_writable_bot_policy(bot.get("policy")):
            raise DevBotError(
                f"bot {bot_id!r} is write-capable — it routes to the repo "
                "executor, not the glofox executor")
    except DevBotError:
        raise
    except Exception:
        raise DevBotError("policy evaluation failed — fail closed")
    if not _serve.glofox_read_granted(bot.get("policy")):
        raise DevBotError(
            f"bot {bot_id!r} does not grant exactly glofox:read (tier 0) — "
            "reconfigure it through the Browser Bot preset first")

    from task_store import CloudTaskStore  # local import, no hard dependency
    if store is None:
        store = CloudTaskStore()

    return store.submit(
        session_key=bot_id,
        task_text=GLOFOX_SCHEDULE_COMMAND,
        repo_url=None,
        executor_prefix="glofox",
        bot_id=bot_id,
        rift=str(bot.get("rift") or "").strip(),
        chat_id=str(user or ""),
        resolve_bot=True,
        conversation_id=(str(conversation_id).strip() or None
                         if conversation_id else None),
    )


def submit_browser_task(user, bot, steps, store=None, conversation_id=None):
    """Enqueue a Bot-bound READ-ONLY browser task on the existing
    CloudTaskStore, executed through `serve.run_task` ->
    `browser_preflight_block` -> `browser_host_dispatch` -> the explicitly
    bound Browser Host channel. No local browser executor exists; an
    missing/revoked binding or offline host fails closed there.

    *steps* must be a validated, bounded structured list containing ONLY
    `navigate` and `read` actions. All policy/allowlist/approval/audit
    handling is the existing path's, unchanged.
    """
    bot = bot or {}
    bot_id = str(bot.get("id") or "").strip()
    owner = str(bot.get("owner") or "").strip()
    if not bot_id:
        raise DevBotError("bot id is required")

    # Structure gate: bounded, ordered, navigate/read only. Anything else
    # (write-capable actions, extra fields, oversize) rejects BEFORE any
    # task row is written.
    try:
        steps = validate_browser_steps(steps)
    except BrowserStepError as exc:
        raise DevBotError(str(exc)) from exc
    for step in steps:
        if step.get("action") not in ("navigate", "read"):
            raise DevBotError(
                f"step {step.get('action')!r} is not an accepted Browser Bot "
                "action; only navigate and read are")
    if not any(s.get("action") == "navigate" for s in steps):
        raise DevBotError("a browser task needs at least one navigate step")

    allowlist = bot.get("browser_allowlist")
    if not isinstance(allowlist, list) or not any(
            isinstance(h, str) and h.strip() for h in allowlist):
        raise DevBotError(
            f"bot {bot_id!r} has an empty browser allowlist — every "
            "navigation is denied (fail closed)")
    for step in steps:
        if step.get("action") == "navigate" and not _host_allowed(
                allowlist, step.get("url", "")):
            raise DevBotError(
                "domain is not on this Bot's browser allowlist")
    if len(allowlist) > 64:
        raise DevBotError(f"browser allowlist exceeds 64 entries")

    # Lifecycle + authority gates (server-side, authoritative), mirroring
    # submit_bot_task: paused/stopped reject new work; write-capable Bots
    # belong to the repo path, never the browser path.
    if not _bots.is_running(bot):
        raise DevBotError(
            f"bot {bot_id!r} is {bot.get('status') or _bots.STATUS_STOPPED} — "
            "start it before submitting tasks")
    try:
        if is_writable_bot_policy(bot.get("policy")):
            raise DevBotError(
                f"bot {bot_id!r} is write-capable — it routes to the repo "
                "executor, not the browser executor")
    except DevBotError:
        raise
    except Exception:
        raise DevBotError("policy evaluation failed — fail closed")

    # Explicit host binding (the fail-closed path in run_task remains, but
    # failing the turn early gives a clear message with no orphan task).
    try:
        import browser_hosts as _bh
        if not _bh.binding_for(owner, bot_id):
            raise DevBotError(
                f"bot {bot_id!r} has no Browser Host explicitly bound — "
                "browser tasks fail closed without one")
    except DevBotError:
        raise
    except Exception:
        raise DevBotError("browser host registry unavailable — fail closed")

    from task_store import CloudTaskStore  # local import, no hard dependency

    if store is None:
        store = CloudTaskStore()

    return store.submit(
        session_key=bot_id,
        task_text=browser_task_text_for(steps),
        repo_url=None,
        executor_prefix="browser",
        bot_id=bot_id,
        rift=str(bot.get("rift") or "").strip(),
        chat_id=str(user or ""),
        resolve_bot=True,
        conversation_id=(str(conversation_id).strip() or None
                         if conversation_id else None),
    )


# ═════════════════════════════════════════════════════════════════════════
# Level 6 weekly bridge — ONE pinned owner-facing command
# ═════════════════════════════════════════════════════════════════════════
#
# The Level 6 weekly read is a single, server-defined Chat command:
#     level6: weekly
# (the exact text serve.resolve_executor maps to the level6 executor). There
# is NO caller-controlled URL, branch, method, body, filter, or date — the
# Facebook page, the persistent ``browser-bot`` profile, and the Glofox
# schedule read are all pinned inside level6_weekly / serve.
#
# Safety boundaries enforced HERE, before any task row is created:
#   * The owner-facing command text is ONLY the fixed pinned request (a
#     different text rejects; nothing is guessed, compiled, or forwarded).
#   * The Bot must be RUNNING (like every durable submission).
#   * The Bot's policy must be EXACTLY the dedicated Level 6 Weekly grant
#     (browser:navigate/read/screenshot + the pinned glofox:read) via the
#     shared host predicate — wildcards and partial grants never count.
#   * A write-capable Bot (fs:write) NEVER routes here: repo path only.
#   * No local browser executor: the capture reuses the owner's existing
#     persistent ``browser-bot`` Browser Host binding through the
#     already-implemented dispatch path (serve._level6_browser_dispatch),
#     which swaps only the bot id.

#: The owner-facing command (the text an owner types, and the text the Chat
#: route intercepts byte-exactly).
LEVEL6_WEEKLY_COMMAND = _serve.LEVEL6_TASK_TEXT

#: The task text the IN-PROCESS level6 handler expects
#: (``serve._run_level6_weekly_task`` compares against
#: ``serve.LEVEL6_WEEKLY_REQUEST`` — the prefix-stripped request the
#: ``level6: <request>`` executor contract uses, exactly as the Telegram path
#: passes it). The Chat submission stores THIS value so the durable task
#: reaches the handler and the six-line result is produced.
LEVEL6_WEEKLY_REQUEST = _serve.LEVEL6_WEEKLY_REQUEST


def level6_route_ready(bot) -> bool:
    """True when a bound Bot may receive the pinned `level6: weekly` command
    through Chat: RUNNING, not write-capable, and holding the EXACT dedicated
    Level 6 Weekly grant (browser:navigate + browser:read +
    browser:screenshot + glofox:read, tier 0, and nothing else).

    The authoritative checks re-run inside `serve.run_task`'s level6 branch
    (identity → policy → browser capture → Glofox join), so route readiness
    can only approve — never widen — that path.
    """
    bot = bot or {}
    try:
        if not _bots.is_running(bot):
            return False
        if is_writable_bot_policy(bot.get("policy")):
            return False                    # write-capable routes to repo
        return _serve.level6_weekly_granted(bot.get("policy"))
    except Exception:
        return False                        # any fault = no route


def submit_level6_task(user, bot, task_text, store=None, conversation_id=None):
    """Enqueue a Bot-bound Level 6 weekly read on the existing CloudTaskStore,
    executed through `serve.run_task`'s IN-PROCESS level6 branch (the pinned
    Facebook capture on the persistent ``browser-bot`` profile + the pinned
    Glofox schedule read; no process spawn, no local browser executor, no
    rift).

    This is the exact owner-facing command — the same ``level6: weekly`` task
    text the Telegram path and the Routine submission produce. The ONLY
    permitted value of *task_text* is that pinned string; anything else fails
    closed BEFORE any task is written.
    """
    bot = bot or {}
    bot_id = str(bot.get("id") or "").strip()
    owner = str(bot.get("owner") or "").strip()
    text = str(task_text or "").strip()
    if not bot_id or not owner:
        raise DevBotError("bot id and owner are required")
    # Owner scoping: only the OWNER may submit for their Bot. A foreign owner
    # is refused before any task row; serve.run_task re-checks the identity.
    if owner != str(user or "").strip():
        raise DevBotError(
            f"bot {bot_id!r} belongs to another owner — fail closed")
    if text != LEVEL6_WEEKLY_COMMAND:
        raise DevBotError(
            f"unsupported Level 6 request {text!r}; the only accepted "
            f"command is {LEVEL6_WEEKLY_COMMAND!r}")
    if not _bots.is_running(bot):
        raise DevBotError(
            f"bot {bot_id!r} is {bot.get('status') or _bots.STATUS_STOPPED} — "
            "start it before submitting tasks")
    try:
        if is_writable_bot_policy(bot.get("policy")):
            raise DevBotError(
                f"bot {bot_id!r} is write-capable — it routes to the repo "
                "executor, not the level6 executor")
    except DevBotError:
        raise
    except Exception:
        raise DevBotError("policy evaluation failed — fail closed")
    if not _serve.level6_weekly_granted(bot.get("policy")):
        raise DevBotError(
            f"bot {bot_id!r} does not hold the exact Level 6 Weekly grant "
            "(browser:navigate/read/screenshot + glofox:read) — reconfigure "
            "it through the Level 6 Weekly preset first")

    from task_store import CloudTaskStore  # local import, no hard dependency

    if store is None:
        store = CloudTaskStore()

    return store.submit(
        session_key=bot_id,
        # The pinned owner-facing command is validated above; the executor's
        # own in-process handler requires the prefix-stripped request text
        # (serve._run_level6_weekly_task). Never a caller value, never a
        # compiled/guessed one.
        task_text=LEVEL6_WEEKLY_REQUEST,
        repo_url=None,
        executor_prefix="level6",
        bot_id=bot_id,
        rift=str(bot.get("rift") or "").strip(),
        chat_id=str(user or ""),
        resolve_bot=True,
        conversation_id=(str(conversation_id).strip() or None
                         if conversation_id else None),
    )
