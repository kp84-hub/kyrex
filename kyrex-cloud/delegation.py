"""delegation.py — owner-scoped, single-level Bot-to-Bot delegation.

This is the FIRST safe coordination layer for Kyrex Chat. A coordinator Bot
(the owner's "Chief of Staff") can delegate a task to another Bot the SAME
owner owns; the delegated task is an ORDINARY target-Bot task created through
the EXISTING durable path (``CloudTaskStore`` -> ``TaskWorker`` ->
``serve.run_task``). Nothing here executes a target inline, and nothing here
starts a second registry, a second policy engine, a second task store, a
second event stream, or a new polling bus.

Two non-negotiable invariants:

  1. **The target Bot is authoritative.** The delegation carries only a text
     instruction and identities. The target's own provider profile, model,
     Rift, policy, browser allowlist, lifecycle, and approvals are resolved by
     the existing executor path from the target's own registry entry — never
     from the coordinator, and never shared between them.
  2. **One level only.** A delegation may not itself delegate. ``depth`` is
     capped at 1 and a record carrying a ``parent_delegation_id`` is refused,
     so no recursive chain, no autonomous scheduling, and no cross-owner work
     can be created here.

Safety of what is recorded and returned: a delegation row holds identities,
the (owner-typed) task text, a lifecycle status, timestamps, and a SANITIZED
final result summary. Provider keys, headers, tokens, Rift paths, system
prompts, raw approval secrets, and browser-session metadata are NEVER stored
or returned by this module.
"""
from __future__ import annotations

import sys
from pathlib import Path

# delegation.py sits inside kyrex-cloud/ — resolve the Cloud package the same
# way chat_service.py / dev_bot.py do so the EXISTING registry, store, tier
# table, and gates are reused rather than re-implemented.
_CLOUD_DIR = Path(__file__).resolve().parent
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

import bots as _bots            # noqa: E402 — authoritative registry + lifecycle
import serve as _serve          # noqa: E402 — host tier table + coordinator gate
# NOTE: the writable-Bot gate is read from ``serve`` (its single source of
# truth) rather than importing dev_bot, so this module stays importable outside
# the web backend (no cross-directory dependency).
from task_store import (        # noqa: E402 — the EXISTING durable store
    CloudTaskStore,
    STATUS_QUEUED,
    STATUS_DELEGATION_REJECTED,
    DELEGATION_TERMINAL_STATUSES,
)

# The maximum delegation depth for this slice. A delegation is level 1; a
# request that would create level 2 (delegation-from-a-delegation) is refused.
MAX_DEPTH = 1


class DelegationError(Exception):
    """The delegation request is refused (fail closed, with a clear reason).

    Raised for every un-eligible case: a coordinator that is not coordinator-
    capable, a foreign-owner target, a stopped/paused target, a target with no
    provider configuration, an unresolvable Rift, a nesting attempt, or an
    invalid executor prefix. The message is user-facing.
    """


# ── Target eligibility ─────────────────────────────────────────────────

def _bot_rift_resolves(bot: dict) -> bool:
    """Absolute path to an existing directory (mirrors chat_service)."""
    raw = str((bot or {}).get("rift") or "").strip()
    p = Path(raw)
    if not p.is_absolute():
        return False
    try:
        return p.resolve().is_dir()
    except OSError:
        return False


def resolve_delegation_target(owner: str, target_bot_id: str) -> dict:
    """Resolve *target_bot_id* to a target the coordinator's owner may delegate.

    Fail closed with a clear :class:`DelegationError` for every un-eligible
    case, checked in the order the user would want to hear them:

      * unknown target,
      * foreign owner (the target is not this owner's),
      * target not delegation-eligible (stopped / paused / unknown lifecycle),
      * unavailable Rift,
      * missing provider configuration.

    Returns the target's registry record. The record is used ONLY for the
    target's own id/rift/owner; none of its sensitive fields are copied onto
    the delegation.
    """
    owner = str(owner or "").strip()
    target_bot_id = str(target_bot_id or "").strip()
    if not owner:
        raise DelegationError("delegation requires an owner")
    if not target_bot_id:
        raise DelegationError("a target Bot id is required")

    try:
        registry = _bots.load_bots()
    except Exception as exc:  # RegistryError and anything else
        raise DelegationError(f"bot registry unavailable: {exc}")

    target = registry.get(target_bot_id)
    if target is None:
        raise DelegationError(f"unknown target Bot {target_bot_id!r}")

    target_owner = str(target.get("owner") or "").strip()
    # Cross-owner delegation is never permitted. An ownerless (legacy) Bot is
    # also not delegatable — it is visible but not owned by this owner.
    if target_owner != owner:
        raise DelegationError(
            f"target Bot {target_bot_id!r} is not owned by you"
        )

    status = str(target.get("status") or "").strip() or _bots.STATUS_STOPPED
    if not _bots.is_running(target):
        raise DelegationError(
            f"target Bot {target_bot_id!r} is {status} — start it before delegating"
        )

    if not _bot_rift_resolves(target):
        raise DelegationError(
            f"target Bot {target_bot_id!r} Rift is unavailable"
        )

    # Provider configuration. Reuse the SAME resolver the executor and the
    # Bot-bound Chat path use, so "configured" means exactly what it means
    # everywhere else. ``None`` (no profile reference) and a raised resolution
    # fault are both "unconfigured" for delegation.
    try:
        llm = _serve._bot_llm_config(target)
    except Exception as exc:
        raise DelegationError(
            f"target Bot {target_bot_id!r} has no usable provider configuration: {exc}"
        )
    if not llm:
        raise DelegationError(
            f"target Bot {target_bot_id!r} has no provider configuration"
        )

    return target


# ── Safe metadata / capability labels ──────────────────────────────────

# Human-readable labels for host operations. These describe WHAT a Bot is
# permitted to do; they never reveal policy rules, secrets, or paths.
_OP_LABELS: dict[str, str] = {
    "fs:read": "read files",
    "repo:read": "read repos",
    "fs:write": "write files",
    "repo:pr": "open pull requests",
    "repo:push": "push",
    "fs:delete": "delete files",
    "cal:list": "read calendar",
    "cal:create": "create events",
    "mail:read": "read mail",
    "mail:send": "send mail",
    "browser:navigate": "browse",
    "browser:read": "read pages",
    "browser:click": "click",
    "browser:type": "type",
    "browser:upload": "upload",
    "browser:download": "download",
    "browser:submit": "submit forms",
    "browser:delete": "delete remote items",
    "bot:delegate": "coordinate Bots",
}


def capability_labels(policy) -> list[str]:
    """Safe capability labels derived from a policy via the host tier table.

    A label is included for each host operation whose effective tier is not
    ``"deny"`` — i.e. the Bot is allowed it (possibly subject to approval).
    Labels are descriptive only; no rule, tier, secret, or path is exposed.
    """
    try:
        perms = _serve.effective_permissions(policy)
    except Exception:
        return []
    labels: list[str] = []
    for op, tier in perms.items():
        if tier == "deny":
            continue
        label = _OP_LABELS.get(op)
        if label and label not in labels:
            labels.append(label)
    return sorted(labels)


def role_label(bot: dict) -> str:
    """A safe role label from the existing gates (never from free text)."""
    if _serve.coordinator_granted(bot):
        return "coordinator"
    try:
        if _serve.is_writable_bot_policy((bot or {}).get("policy")):
            return "developer"
    except Exception:
        pass
    return "worker"


def safe_bot_metadata(bot: dict) -> dict:
    """The ONLY Bot shape a coordinator may see: id, name, status, role,
    capabilities, model, availability.

    Deliberately excludes every sensitive field: Rift path, policy rules,
    system prompt, provider profile id, credentials, tokens, and browser
    allowlist contents. Derived from the same helpers the Chat surfaces use, so
    nothing here can drift from what the UI already shows.
    """
    bot = bot or {}
    return {
        "id": bot.get("id"),
        "name": bot.get("name"),
        "status": bot.get("status"),
        "role": role_label(bot),
        "capabilities": capability_labels(bot.get("policy")),
        "model": bot.get("model") or "",
        "available": _bot_rift_resolves(bot),
    }


def visible_targets(owner: str, *, exclude_bot_id: str | None = None) -> list[dict]:
    """Safe metadata for every Bot the owner owns (never another owner's).

    Excludes the coordinator itself by default so a coordinator can only see
    the peers it may delegate to.
    """
    owner = str(owner or "").strip()
    try:
        registry = _bots.load_bots()
    except Exception as exc:
        raise DelegationError(f"bot registry unavailable: {exc}")
    out: list[dict] = []
    for bot in registry.values():
        if str(bot.get("owner") or "").strip() != owner:
            continue
        if exclude_bot_id and str(bot.get("id")) == str(exclude_bot_id):
            continue
        out.append(safe_bot_metadata(bot))
    return sorted(out, key=lambda b: str(b.get("id") or ""))


# ── Public delegation view ─────────────────────────────────────────────

def public_view(rec: dict) -> dict:
    """The NON-SECRET view of a delegation row (safe for any owner-facing UI).

    Includes only identities, lifecycle, timestamps, the owner-typed task text,
    and the sanitized result summary. Provider config, Rift paths, prompts,
    approval secrets, and browser-session metadata are never present on the
    stored row and so can never appear here.
    """
    rec = rec or {}
    return {
        "delegation_id": rec.get("delegation_id"),
        "coordinator_bot_id": rec.get("coordinator_bot_id"),
        "target_bot_id": rec.get("target_bot_id"),
        "parent_conversation_id": rec.get("parent_conversation_id"),
        "parent_delegation_id": rec.get("parent_delegation_id"),
        "task_id": rec.get("task_id"),
        "executor_prefix": rec.get("executor_prefix"),
        "depth": rec.get("depth", 1),
        "text": rec.get("task_text") or "",
        "status": rec.get("status"),
        "result_summary": rec.get("result_summary") or "",
        "error": rec.get("error") or "",
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
        "finished_at": rec.get("finished_at"),
        # Whether the terminal result has already been announced once to the
        # parent coordinator conversation. Lets the card show a result without
        # the coordinator re-announcing it on every subsequent turn.
        "relayed": bool(rec.get("relayed_at")),
    }


# ── Submission ─────────────────────────────────────────────────────────

def _validate_executor_prefix(executor_prefix: str) -> str:
    prefix = str(executor_prefix or "repo").strip().lower() or "repo"
    if prefix not in _serve.EXECUTORS:
        raise DelegationError(f"unknown executor prefix {prefix!r}")
    return prefix


# ── Delegated-intent routing for fixed read-only capabilities ──────────
#
# A Calendar Reader is a FIXED read-only capability, not a repository executor.
# Its only vocabulary is the three pinned commands, and its read runs IN-PROCESS
# (serve.run_task(executor_prefix="calendar")) -- never the generic repo
# executor, which would demand a repo URL and a Rift. A delegated calendar
# intent is therefore normalized to the ONE exact command and routed to that
# in-process branch; anything unsupported or ambiguous fails closed BEFORE any
# record is written. A non-calendar intent is returned UNCHANGED so ordinary
# and developer delegation behaviour is byte-identical.

def _is_calendar_reader(bot: dict) -> bool:
    """True iff *bot* holds EXACTLY the read-only ``cal:list`` grant.

    Mirrors the host's own exact-grant predicate (``serve.cal_list_granted``)
    together with the writable-check, so a Calendar Reader can never be
    confused with a repository executor. Any fault is "not a reader" (fail
    closed).
    """
    try:
        if _serve.is_writable_bot_policy((bot or {}).get("policy")):
            return False
        return bool(_serve.cal_list_granted((bot or {}).get("policy")))
    except Exception:
        return False


def _resolve_delegated_route(caller_prefix: str, target: dict, text: str):
    """Resolve ``(executor_prefix, task_text)`` for a delegated task, fail closed.

    Reuses the SAME executor resolution the direct path uses
    (``serve.resolve_executor``): the reserved ``calendar:`` namespace is the
    Calendar Reader's ONLY vocabulary, and a supported request is normalized to
    the exact command (case/whitespace). Any other ``calendar:`` text is
    unsupported/ambiguous and is refused -- never routed to the repo executor
    or the engine/LLM.
    """
    stripped = str(text or "").strip()
    route_prefix, canonical, error_word = _serve.resolve_executor(stripped)

    # The reserved ``calendar:`` namespace: only the three exact commands. A
    # namespace match that did not resolve (e.g. ``calendar:week`` without the
    # separating space, or ``calendar: yesterday``) fails closed here.
    namespace = stripped.lower().startswith("calendar:")
    if error_word == "calendar" or (namespace and route_prefix != "calendar"):
        raise DelegationError(
            "unsupported calendar delegation request "
            f"{stripped!r}; the only accepted requests are "
            + ", ".join(sorted(_serve.CALENDAR_TASK_TEXTS)))

    if route_prefix != "calendar":
        # Not a calendar intent. A Calendar Reader is a fixed read-only
        # capability: it can run ONLY the pinned commands, so a non-calendar
        # task must never fall to the generic repo executor.
        if _is_calendar_reader(target):
            raise DelegationError(
                f"target Bot {str(target.get('id') or '')!r} is a Calendar "
                "Reader -- a fixed read-only capability that can run only "
                + ", ".join(sorted(_serve.CALENDAR_TASK_TEXTS)))
        return caller_prefix, stripped

    # A calendar intent: the target MUST be a Calendar Reader (exactly
    # ``cal:list`` at tier 0) and must NOT be write-capable. Re-checked here so
    # a delegated read can never borrow a repo executor's privileges.
    target_id = str(target.get("id") or "").strip()
    try:
        writable = _serve.is_writable_bot_policy(target.get("policy"))
    except Exception:
        writable = True                     # fail closed
    if writable:
        raise DelegationError(
            f"target Bot {target_id!r} is write-capable -- a calendar read "
            "routes to the Calendar Reader, never the repo executor")
    if not _serve.cal_list_granted(target.get("policy")):
        raise DelegationError(
            f"target Bot {target_id!r} is not a Calendar Reader -- configure "
            "it through the Calendar Reader preset first")
    return "calendar", canonical


def submit_delegation(
    owner: str,
    coordinator_bot: dict,
    target_bot_id: str,
    text: str,
    *,
    store: CloudTaskStore | None = None,
    parent_conversation_id: str | None = None,
    parent_task_id: str | None = None,
    parent_delegation_id: str | None = None,
    executor_prefix: str = "repo",
    depth: int = 1,
) -> dict:
    """Create a durable delegation and its ORDINARY target task.

    Steps (each fail-closed with a clear :class:`DelegationError`):

      1. the coordinator Bot must be coordinator-capable (its OWNER granted
         ``bot:delegate``) — this is the "explicitly granted" requirement;
      2. single-level guard: ``depth == 1`` and no ``parent_delegation_id``;
      3. the target must be resolvable for the SAME owner
         (:func:`resolve_delegation_target`);
      4. a durable delegation row is written;
      5. an ordinary target task is submitted through the EXISTING store with
         ``resolve_bot=True`` so the target's own model/provider/Rift/policy
         are authoritative at execution time.

    Returns the :func:`public_view` of the created delegation.
    """
    owner = str(owner or "").strip()
    coordinator_bot = coordinator_bot or {}
    coordinator_id = str(coordinator_bot.get("id") or "").strip()
    text = str(text or "").strip()

    if not owner:
        raise DelegationError("delegation requires an owner")
    if not coordinator_id:
        raise DelegationError("a coordinator Bot is required")
    if str(coordinator_bot.get("owner") or "").strip() != owner:
        raise DelegationError("the coordinator Bot is not owned by you")
    if not _serve.coordinator_granted(coordinator_bot):
        raise DelegationError(
            f"Bot {coordinator_id!r} is not coordinator-capable"
        )
    if not text:
        raise DelegationError("a delegation task text is required")

    # ── single-level guard (no recursive delegation) ───────────────────
    if parent_delegation_id:
        raise DelegationError(
            "recursive delegation is not permitted (one level only)"
        )
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        raise DelegationError("delegation depth must be an integer")
    if depth != MAX_DEPTH:
        raise DelegationError(
            f"delegation depth must be {MAX_DEPTH} (one level only)"
        )

    executor_prefix = _validate_executor_prefix(executor_prefix)

    # ── target eligibility (owner-scoped, fail closed) ─────────────────
    target = resolve_delegation_target(owner, target_bot_id)
    target_id = str(target.get("id") or target_bot_id).strip()
    if target_id == coordinator_id:
        raise DelegationError("a coordinator cannot delegate to itself")

    # Fixed read-only capabilities route through their OWN in-process handler: a
    # delegated calendar intent is normalized to the exact command and dispatched
    # via serve.run_task(executor_prefix="calendar") -- never the generic repo
    # executor (no Rift, no repo URL), and never the engine/LLM fallback. Any
    # unsupported/ambiguous calendar text fails closed BEFORE any record.
    executor_prefix, text = _resolve_delegated_route(
        executor_prefix, target, text)

    if store is None:
        store = CloudTaskStore()

    # ── durable delegation record first (so a submit fault is recoverable) ─
    delegation_id = store.create_delegation(
        owner=owner,
        coordinator_bot_id=coordinator_id,
        target_bot_id=target_id,
        task_text=text,
        parent_conversation_id=parent_conversation_id,
        parent_task_id=parent_task_id,
        parent_delegation_id=None,
        depth=depth,
        executor_prefix=executor_prefix,
        status=STATUS_QUEUED,
    )

    # ── the ordinary target task (EXISTING durable path) ───────────────
    # resolve_bot=True records the identity chain (bot_id -> rift) and makes the
    # worker run the task with Bot resolution enabled, so serve.build_context
    # loads the TARGET's own rift/policy/model/provider. conversation_id links
    # the target's engine session to the parent coordinator conversation, and
    # parent_delegation_id records the delegation on the task row itself.
    try:
        task_id = store.submit(
            session_key=target_id,
            task_text=text,
            repo_url=None,
            executor_prefix=executor_prefix,
            bot_id=target_id,
            rift=str(target.get("rift") or ""),
            chat_id=owner,
            resolve_bot=True,
            conversation_id=parent_conversation_id,
            parent_delegation_id=delegation_id,
        )
    except Exception as exc:
        # The target task could not be created: record the refusal on the
        # delegation so the coordinator conversation reports it clearly.
        store.set_delegation_status(
            delegation_id, STATUS_DELEGATION_REJECTED,
            error=f"could not create target task: {exc}",
        )
        raise DelegationError(f"could not create target task: {exc}")

    store.set_delegation_status(delegation_id, STATUS_QUEUED, task_id=task_id)
    return public_view(store.get_delegation(delegation_id) or {})


def list_delegations(
    owner: str,
    *,
    store: CloudTaskStore | None = None,
    coordinator_bot_id: str | None = None,
    parent_conversation_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Owner-scoped list of delegations (public views only)."""
    owner = str(owner or "").strip()
    if not owner:
        return []
    if store is None:
        store = CloudTaskStore()
    rows = store.list_delegations(
        owner=owner,
        coordinator_bot_id=coordinator_bot_id,
        parent_conversation_id=parent_conversation_id,
        limit=limit,
    )
    return [public_view(r) for r in rows]


# ── Owner-scoped status queries ────────────────────────────────────────
#
# The status path is READ-ONLY with respect to authority: it can only ever
# return delegations the SAME owner created through the SAME coordinator Bot.
# It never approves, denies, cancels, or mutates the target task — a delegated
# approval stays the owner's, through the target task's existing flow.

def is_terminal(status: str | None) -> bool:
    """True when *status* is a delegation lifecycle terminal state."""
    return str(status or "") in DELEGATION_TERMINAL_STATUSES


def fetch_delegation(
    owner: str, delegation_id: str, *,
    store: CloudTaskStore | None = None,
    coordinator_bot_id: str | None = None,
) -> dict | None:
    """Return one OWNER-scoped delegation record (raw), or ``None``.

    A coordinator may only ever see delegations it created for its OWNER. A
    delegation that belongs to another owner — or to another coordinator — is
    reported as ``None`` rather than an error, so a status query can never be
    used to probe for another owner's work.
    """
    owner = str(owner or "").strip()
    delegation_id = str(delegation_id or "").strip()
    if not owner or not delegation_id:
        return None
    if store is None:
        store = CloudTaskStore()
    rec = store.get_delegation(delegation_id)
    if rec is None:
        return None
    if str(rec.get("owner") or "").strip() != owner:
        return None
    if coordinator_bot_id is not None and \
            str(rec.get("coordinator_bot_id") or "").strip() != \
            str(coordinator_bot_id or "").strip():
        return None
    return rec


def owner_scoped_delegations(
    owner: str, *,
    store: CloudTaskStore | None = None,
    coordinator_bot_id: str | None = None,
    conversation_id: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """Raw OWNER-scoped delegation records, newest first.

    Both filters are applied server-side (in SQL): a coordinator sees only the
    delegations it created, for its own owner, in the requested conversation.
    """
    owner = str(owner or "").strip()
    if not owner:
        return []
    if store is None:
        store = CloudTaskStore()
    return store.list_delegations(
        owner=owner,
        coordinator_bot_id=coordinator_bot_id,
        parent_conversation_id=conversation_id,
        limit=limit,
    )
