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
     never the user's connected repo — is the workspace.

Non-Bot Chat and read-only Bots keep the existing read-only EngineSession
path unchanged; this module is only a gate + entry point for the writable
developer path, and it never auto-approves anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

# kyrex-cloud/web/backend/dev_bot.py sits inside kyrex-cloud/ — resolve the
# Cloud package the same way chat_service.py does so the EXISTING policy
# engine and host tier table are reused.
_SCRIPT_DIR = Path(__file__).resolve().parent            # web/backend/
_CLOUD_DIR = _SCRIPT_DIR.parent.parent                   # kyrex-cloud/
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

import policy as _policy  # noqa: E402  — the existing Bot policy engine
import serve as _serve    # noqa: E402  — the existing host tier table


class DevBotError(Exception):
    """The requested Bot cannot be routed to the writable executor path."""


# The single write-class operation that marks a Bot as a developer Bot: the
# ability to write files (edit_file / write_file_with_gate). Deletion
# (fs:delete), git push/PR (repo:push / repo:pr), and shell (run_command)
# remain governed by their own host tiers / engine gates and are NOT what
# makes a Bot "writable" here — a Bot that can write files is a coding Bot;
# one that can only list calendars or read mail is not. This is intentionally
# narrower than "any write-class operation" so calendar/mail-only Bots are
# never misrouted to the coding executor path.
DEVELOPER_WRITE_OPS: frozenset[str] = frozenset({"fs:write"})


def _valid_policy(policy) -> bool:
    """True when *policy* has the exact shape ``policy.evaluate`` understands."""
    if not isinstance(policy, dict):
        return False
    for key, value in policy.items():
        if not isinstance(key, str):
            return False
        if value == "deny":
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if value not in (0, 1, 2):
            return False
    return True


def is_writable_bot_policy(policy) -> bool:
    """Return True iff *policy* explicitly grants the developer write op.

    "Grants" means the EXISTING policy evaluator returns a numeric effective
    tier >= 1 for ``fs:write`` given its host-derived tier. Because numeric
    policy rules may only RAISE the host tier, a matching numeric rule for
    ``fs:write`` (host tier already 1) is a write grant. ``deny``, no matching
    rule, and malformed policies are read-only (fail closed).
    """
    if not _valid_policy(policy):
        return False
    for op in sorted(DEVELOPER_WRITE_OPS):
        decision = _policy.evaluate(policy, op, _serve.OPERATION_TIERS[op])
        effective = decision.get("effective_tier")
        if isinstance(effective, int) and effective >= 1:
            return True
    return False


def submit_bot_task(user, bot, task_text, store=None):
    """Enqueue a Bot-bound coding task on the existing CloudTaskStore.

    Refuses (fail closed) when the Bot is not writable, so a read-only Bot can
    never reach the writable executor path. The task is bound to the Bot's own
    rift: ``session_key`` is the Bot id, ``resolve_bot=True``, and ``repo_url``
    is ``None`` — the user's connected repo is never referenced.
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
    )
