"""taskstore.py — persistent task queue for K-Bot.

A *task* is one queued/running/awaiting_approval/done/failed unit of work for a
Bot (or an unbound session).  The store is a single JSON file at
``paths.data_dir()/tasks.json``, overridable for tests via the
``TASKSTORE_FILE`` environment variable or by assigning ``taskstore.TASKSTORE_FILE``
directly (the way ``bots.BOTS_FILE`` is overridden in the bot-registry tests).

The store is pure data.  All orchestration — session locks, the status
transitions that happen during a run, and how a TaskStore failure is handled so
it never breaks task execution — lives in ``serve.py``.  A TaskStore method
raises on a genuine I/O or data error (like ``bots.py`` does); callers that must
survive such failures, i.e. ``serve.run_task``, wrap the call.

A task record carries:

    task_id        — unique id (uuid4 hex), the dict key on disk
    bot_id         — the session key this task was launched under.  For a
                     Bot-bound session this equals the Bot id; for an unbound
                     session it equals the chat id.  This is what
                     list_by_bot() filters on.
    repo_url       — the target repository URL
    task_text      — the instruction text
    executor_prefix— e.g. "repo", "fs", "cal"
    status         — one of queued / running / awaiting_approval / done / failed
    workspace_ref  — the Bot's rift path (ctx.rift_path), or None when unbound
    result         — the executor's KYREX_RESULT_JSON dict on success, else None
    error          — a human-readable failure string on failure, else None
    created_at     — UTC ISO-8601 timestamp set on create()
    updated_at     — UTC ISO-8601 timestamp bumped on every update()

Lifecycle:  queued → running → (awaiting_approval)* → done | failed.

This module has no recovery logic (no resume, retry, or checkpoint restore) —
that is a later milestone.  See K_BOT_AUTONOMY.md (the task-queue entry in the
"what Rift does not hold" table) and the next-milestone spec.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from paths import data_dir

# Overridable for tests (mirrors bots.BOTS_FILE / audit.AUDIT_FILE).
TASKSTORE_FILE = os.environ.get("TASKSTORE_FILE") or str(data_dir() / "tasks.json")

# Status vocabulary.  A task moves:
#   queued -> running -> (awaiting_approval)* -> done | failed
_VALID_STATUSES = frozenset({
    "queued", "running", "awaiting_approval", "done", "failed",
})

# Fields a caller may write through update().  Anything else is rejected so the
# on-disk schema cannot silently drift.
_WRITABLE_FIELDS = frozenset({
    "status", "error", "result", "workspace_ref",
    "bot_id", "repo_url", "task_text", "executor_prefix",
})

# One lock guards the whole file.  The key space is small (every task that has
# ever been launched) and every operation is a full read-modify-write of a
# single JSON object, so a coarse lock is correct and simple.
_lock = threading.Lock()


class TaskStoreError(Exception):
    """The task store exists but cannot be trusted, or a request was invalid."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    """Current UTC time as a lexicographically sortable ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir() -> None:
    """Create the parent directory of TASKSTORE_FILE if it doesn't exist."""
    Path(TASKSTORE_FILE).parent.mkdir(parents=True, exist_ok=True)


def _load() -> dict:
    """Load the store dict from TASKSTORE_FILE.

    Returns ``{}`` when the file is absent.  Raises ``TaskStoreError`` on
    invalid JSON — a corrupt store must not look empty, or the next save would
    overwrite the damaged file and turn recoverable corruption into loss (same
    rule as ``bots.load_bots``).
    """
    try:
        with open(TASKSTORE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise TaskStoreError(
            "task store at %s is not valid JSON: %s" % (TASKSTORE_FILE, exc)
        ) from exc


def _save(store: dict) -> None:
    """Write the store dict to TASKSTORE_FILE as JSON."""
    _ensure_dir()
    with open(TASKSTORE_FILE, "w") as f:
        json.dump(store, f, indent=2, sort_keys=True)
        f.write("\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create(bot_id: str, repo_url: str, task_text: str,
           executor_prefix: str = "repo", task_id: str | None = None) -> dict:
    """Create a new queued task and persist it.

    Args:
        bot_id:         the session key this task runs under.
        repo_url:       target repository URL.
        task_text:      the instruction text.
        executor_prefix: which executor handles it (default "repo").
        task_id:        optional explicit id; one is generated if omitted.

    Returns the created record dict (with ``status == "queued"``).

    Raises:
        TaskStoreError if *task_id* is supplied but already present, or if the
        backing file is corrupt JSON.
    """
    if task_id is None:
        task_id = uuid.uuid4().hex
    now = _now()
    record = {
        "task_id": task_id,
        "bot_id": bot_id,
        "repo_url": repo_url,
        "task_text": task_text,
        "executor_prefix": executor_prefix,
        "status": "queued",
        "workspace_ref": None,
        "result": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    with _lock:
        store = _load()
        if task_id in store:
            raise TaskStoreError(
                "task id %r already exists in %s" % (task_id, TASKSTORE_FILE)
            )
        store[task_id] = record
        _save(store)
    return record


def get(task_id: str) -> dict | None:
    """Return the task record for *task_id*, or ``None`` if unknown."""
    with _lock:
        store = _load()
    return store.get(task_id)


def update(task_id: str, **fields) -> dict | None:
    """Update fields on an existing task and return the updated record.

    Only fields in ``_WRITABLE_FIELDS`` may be written; any other key raises
    ``TaskStoreError`` so the schema cannot drift.  ``status`` is validated
    against ``_VALID_STATUSES``.  ``updated_at`` is bumped on every call.

    Returns the updated record, or ``None`` if *task_id* is unknown.  Never
    creates a task that does not exist.

    Raises:
        TaskStoreError if a field is not writable or a status is invalid, or if
        the backing file is corrupt JSON.
    """
    invalid = set(fields) - _WRITABLE_FIELDS
    if invalid:
        raise TaskStoreError(
            "cannot update fields %s on task %r (allowed: %s)"
            % (sorted(invalid), task_id, sorted(_WRITABLE_FIELDS))
        )
    if "status" in fields and fields["status"] not in _VALID_STATUSES:
        raise TaskStoreError(
            "invalid status %r; must be one of %s"
            % (fields["status"], sorted(_VALID_STATUSES))
        )
    with _lock:
        store = _load()
        if task_id not in store:
            return None
        record = dict(store[task_id])
        record.update(fields)
        record["updated_at"] = _now()
        store[task_id] = record
        _save(store)
    return record


def list_by_bot(bot_id: str) -> list[dict]:
    """Return every task recorded for *bot_id*, oldest first (FIFO queue order).

    Sorted by ``created_at`` then ``task_id`` so the order is deterministic and
    matches the queued-tasks mental model.
    """
    with _lock:
        store = _load()
    tasks = [rec for rec in store.values() if rec.get("bot_id") == bot_id]
    tasks.sort(key=lambda r: (r.get("created_at", ""), r.get("task_id", "")))
    return tasks
