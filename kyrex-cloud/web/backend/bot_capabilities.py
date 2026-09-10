"""bot_capabilities.py — Bot policy -> engine capability allowlist (Kyrex Chat).

Slice 3 of Bots-in-Kyrex-Chat: a Bot-bound Chat conversation executes with
the Bot's POLICY translated into the tool allowlist handed to the engine.

Intended path (the existing authority chain is unchanged):

    conversation.bot_id
        -> Bot registry      (bots.load_bots / resolve_bot_for_user)
        -> Bot config        (the resolved registry entry)
        -> Bot policy        (policy.evaluate — the ONE existing policy engine)
        -> tool/operation allowlist   (this module)
        -> engine            (KYREX_ALLOWED_TOOLS env; the engine filters the
                              advertised schema AND the dispatch loop)

Two non-negotiable invariants:

  1. Host safety is the floor. Chat serves a fixed host-allowed tool set
     (read-only inspection). A Bot policy may only REMOVE tools from that
     set. It can never add a tool the host does not serve (write/command
     tools stay hidden and unexecutable), never remove
     ``KYREX_READ_ONLY_REPO=1``, never weaken approval requirements, and
     never auto-approve anything.
  2. Policy evaluation reuses the EXISTING semantics through
     ``policy.evaluate``: exact rule > prefix wildcard > ``*``, no matching
     rule denies, an explicit ``deny`` denies, and a numeric rule yields
     ``max(policy_value, host_derived_tier)`` — the host-derived tier can
     never be lowered.

No second policy engine is created here. The host-derived tier for each
operation is read from ``serve.OPERATION_TIERS`` (the single host source of
truth for operation tiers, K_BOT_DESIGN.md) — never re-declared.

Engine tool <-> operation mapping (host-owned; engine tool names from
``kyrex_engine/kyrex/toolbox.py``):

    read_local_file   -> fs:read
    list_local_files  -> fs:read
    search            -> fs:read

``query_memory`` / ``query_knowledge`` / ``task_complete`` are HOST-GRANTED
Chat surface tools: they do not perform workspace operations, there is no
existing operation in the fs/cal/mail/repo taxonomy for them, and the host
already grants them to every Chat session (they are part of the
conversational surface, mirroring the UNBOUND_POLICY precedent of an
explicit, auditable host grant of safe reads). They cannot be removed by a
Bot policy in this slice; a future slice can op-gate them once the taxonomy
gains memory/knowledge operations.

Policy rules that name operations the Chat surface does not serve
(e.g. ``cal:create``, ``repo:push``) remain active for the K-Bot executor
path (serve.py) untouched; in Chat they can only ever map to nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# kyrex-cloud/web/backend/bot_capabilities.py sits inside kyrex-cloud/ —
# resolve the Cloud package the same way chat_service.py does so the
# EXISTING policy engine and the EXISTING host tier table are reused.
_SCRIPT_DIR = Path(__file__).resolve().parent            # web/backend/
_CLOUD_DIR = _SCRIPT_DIR.parent.parent                   # kyrex-cloud/
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

import policy as _policy  # noqa: E402  — the existing Bot policy engine
import serve as _serve    # noqa: E402  — the existing host tier table


class BotPolicyError(Exception):
    """The Bot's policy is malformed and cannot be trusted.

    Raised instead of evaluating a possibly-permissive interpretation of a
    broken policy. The caller fails the turn closed (ChatUnavailable) — never
    falls back to another Bot's policy, a global policy, a default policy, or
    unrestricted Chat behavior.
    """


# ── host-owned mapping: engine tool name -> operation (colon form) ─────
# These are the tools the Chat host serves that correspond to an operation
# in the existing policy taxonomy. The policy decision for the operation
# gates the tool's presence in the engine allowlist.
TOOL_OPERATIONS: dict[str, str] = {
    "read_local_file": "fs:read",
    "list_local_files": "fs:read",
    "search": "fs:read",
}

# Tools the host grants to every Chat session independent of Bot policy
# (see module docstring). task_complete is protocol-mandated: the engine's
# system prompt requires it to end a turn.
HOST_GRANTED_TOOLS: frozenset[str] = frozenset({
    "query_memory",
    "query_knowledge",
    "task_complete",
})

# The complete host-allowed tool set for Chat engine sessions. A Bot policy
# may only produce a SUBSET of this.
CHAT_HOST_BASE_TOOLS: frozenset[str] = frozenset(
    set(TOOL_OPERATIONS) | set(HOST_GRANTED_TOOLS)
)

# Engine tools that map to operations the Chat host does NOT serve (write /
# command surface). These are never in the allowlist regardless of policy —
# host tier 1/2 operations require the human approval UX, which is out of
# scope, so they stay unavailable (never auto-approved).
HOST_DENIED_TOOLS: dict[str, str | None] = {
    "edit_file": "fs:write",
    "write_file_with_gate": "fs:write",
    "run_command": None,  # command execution — no policy-grantable op in Chat
}


def validate_policy(policy) -> None:
    """Fail closed on a malformed Bot policy.

    Valid policies are dicts mapping string rules to ``0``, ``1``, ``2``
    (numeric tiers) or the string ``"deny"`` — the exact value space
    :func:`policy.evaluate` understands. Anything else raises
    :class:`BotPolicyError`; an empty dict is valid (it is the most
    restrictive policy — every operation default-denies).

    Raises:
        BotPolicyError: policy is not a dict, a rule key is not a string, or
        a rule value is outside the tier model.
    """
    if not isinstance(policy, dict):
        raise BotPolicyError(
            f"bot policy must be a dict, got {type(policy).__name__}"
        )
    for key, value in policy.items():
        if not isinstance(key, str):
            raise BotPolicyError(
                f"bot policy rule key must be a string, got {key!r}"
            )
        if value == "deny":
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise BotPolicyError(
                f"bot policy rule {key!r} has invalid value {value!r}; "
                "expected 0, 1, 2, or 'deny'"
            )
        if value not in (0, 1, 2):
            raise BotPolicyError(
                f"bot policy rule {key!r} has tier {value} outside 0..2"
            )


def derive_bot_capabilities(policy) -> dict:
    """Translate *policy* into the engine's tool allowlist.

    For every host-allowed tool that maps to an operation, the operation's
    host-derived tier (``serve.OPERATION_TIERS`` — the host's single source
    of truth) is fed through the EXISTING :func:`policy.evaluate`. The tool
    stays in the allowlist iff the decision's effective tier is ``0`` (host
    auto-allow). Everything else — no matching rule (default deny), an
    explicit ``deny``, or a policy that raises the effective tier above the
    host's — removes the tool from the allowlist; it is never auto-approved.

    The result is always a subset of :data:`CHAT_HOST_BASE_TOOLS`: the host
    base is the mask, so a policy can only restrict, never widen.

    Args:
        policy: the Bot's policy dict from the authoritative registry.

    Returns:
        ``{"tools": [...], "host_base": [...], "host_granted": [...],
        "decisions": {tool: {...decision + allowed...}}}``.

    Raises:
        BotPolicyError: the policy is malformed (fail closed).
    """
    validate_policy(policy)

    decisions: dict[str, dict] = {}
    for tool in sorted(TOOL_OPERATIONS):
        op = TOOL_OPERATIONS[tool]
        derived = _serve.OPERATION_TIERS.get(op)
        if derived is None:
            # Host does not know this operation — cannot vouch for the tool.
            # Fail closed: it is never exposed, whatever the policy claims.
            decisions[tool] = {
                "operation": op,
                "derived_tier": None,
                "effective_tier": "deny",
                "matched_rule": None,
                "reason": f"host does not recognise operation {op!r}",
                "allowed": False,
            }
            continue
        decision = _policy.evaluate(policy, op, derived)
        effective = decision["effective_tier"]
        # Host-serve rule: only effective tier 0 (auto-allow) is served by
        # Chat. "deny" and raised tiers drop the tool — an approval-required
        # operation must remain unavailable (approval UX out of scope).
        allowed = isinstance(effective, int) and effective == 0
        decisions[tool] = {
            "operation": op,
            "derived_tier": derived,
            "effective_tier": effective,
            "matched_rule": decision.get("matched_rule"),
            "reason": decision.get("reason"),
            "allowed": allowed,
        }

    allowed_tools = sorted(
        {t for t, d in decisions.items() if d["allowed"]}
        | set(HOST_GRANTED_TOOLS)
    )
    return {
        "tools": allowed_tools,
        "host_base": sorted(CHAT_HOST_BASE_TOOLS),
        "host_granted": sorted(HOST_GRANTED_TOOLS),
        "decisions": decisions,
    }