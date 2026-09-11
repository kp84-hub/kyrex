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

import serve as _serve  # noqa: E402  — host tier table + the writable-Bot gate
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
