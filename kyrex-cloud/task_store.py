#!/usr/bin/env python3
"""task_store.py — Persistent Cloud Task Lifecycle store for Kyrex Cloud.

This is the production implementation of the Persistent Cloud Task Lifecycle
described in KX_SERVE_DESIGN.md.  It is the single source of truth for task
state across processes and restarts, and it is the link between the Cloud API,
the worker pool, and the *existing* execution path (``serve.run_task``).

Design constraints honoured here (see the approved milestone spec):

  * SQLite only, via the Python standard library.  No Postgres, Redis, or any
    other external service.
  * The database lives under ``DATA_DIR`` (``~/.kyrex`` by default, overridable
    via ``KYREX_DATA_DIR``) so it survives container restarts instead of living
    on ephemeral container storage.
  * Stable ``task_id`` and a persistent task schema.
  * Task lifecycle:
        queued -> running -> awaiting_approval -> running -> done
                                  |
                                  +-> (cancelled | failed)
    plus ``failed`` (execution error / crash / timeout) and ``cancelled``
    (operator cancellation or rejection / interrupted approval on restart).
  * Atomic task claiming so two workers/processes cannot claim the same task.
  * Durable, task_id-linked approval requests while the in-memory approval
    protocol in ``serve`` is left intact.
  * Restart-safe discovery/recovery: orphaned ``running`` / ``awaiting_approval``
    tasks from a dead worker are recovered on the next worker startup.
  * Identity chain preserved:  task_id -> bot_id -> session_key -> rift -> run_id.
  * Same-Bot execution is serialised; different-Bot tasks may run concurrently
    across workers (no parallel same-Bot Rift clones in this milestone).

Nothing here spawns its own executor: the worker calls ``serve.run_task`` (the
existing, unchanged execution implementation) with thin integration callbacks.
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from paths import DATA_DIR

# ── Lifecycle states ────────────────────────────────────────────────────────
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

VALID_STATUSES = frozenset({
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_AWAITING_APPROVAL,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_CANCELLED,
})

# Terminal states: no further transition is expected.
TERMINAL_STATUSES = frozenset({
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_CANCELLED,
})

# Non-terminal ("active work") states. The Chat sidebar's active-work line is
# shown for exactly these — a task is advertised as active while, and only
# while, the durable store still says so.
NONTERMINAL_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_AWAITING_APPROVAL)

# Executor result statuses that count as a *failed* task run (the agent did not
# complete successfully).  Everything else (including "no_changes") is "done".
_FAILED_EXECUTOR_STATUSES = frozenset({
    "agent_failed",
    "git_failed",
    "error",
})

# ── Delegation lifecycle (Bot-to-Bot coordination) ──────────────────────────
# A delegation's status mirrors the linked target task's lifecycle, plus
# ``rejected`` — the coordinator's request was refused before any target task
# was created (target stopped/paused, unconfigured provider, unresolvable Rift,
# foreign owner, or a nesting/cross-owner violation). ``rejected`` is terminal.
STATUS_DELEGATION_REJECTED = "rejected"
DELEGATION_STATUSES = frozenset({
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_AWAITING_APPROVAL,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_DELEGATION_REJECTED,
})
DELEGATION_TERMINAL_STATUSES = frozenset({
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_DELEGATION_REJECTED,
})

DEFAULT_DB_NAME = "cloud_tasks.db"

# Active tasks are leased independently of worker liveness. Workers renew each
# running/awaiting_approval task periodically; recovery treats a missing or
# expired lease as abandoned. The timeout comfortably exceeds heartbeat cadence.
TASK_TIMEOUT_SECONDS = int(os.environ.get("KYREX_TASK_TIMEOUT", "1800"))
APPROVAL_TIMEOUT_SECONDS = int(os.environ.get("KYREX_APPROVAL_TIMEOUT", "600"))
RECOVERY_GRACE_SECONDS = int(os.environ.get("KYREX_TASK_RECOVERY_GRACE", "60"))
WORKER_RECOVERY_INTERVAL = float(
    os.environ.get("KYREX_TASK_RECOVERY_INTERVAL", "30")
)
TASK_STALE_AFTER = TASK_TIMEOUT_SECONDS + RECOVERY_GRACE_SECONDS
APPROVAL_STALE_AFTER = APPROVAL_TIMEOUT_SECONDS + RECOVERY_GRACE_SECONDS


class TaskStoreError(Exception):
    """Base class for task store errors."""


class DuplicateTaskId(TaskStoreError):
    """Raised when submitting a task_id that already exists."""


class TaskNotFound(TaskStoreError):
    """Raised when a task_id does not exist."""


# Terminal reasons for the Browser Host dispatch bridge. Both are fail-closed
# outcomes: the work is NEVER handed to a host after either applies.
BROWSER_DISPATCH_EXPIRED = (
    "HostUnavailable: browser host dispatch deadline expired before the "
    "socket-owning process could run it"
)
BROWSER_DISPATCH_CANCELLED = "HostUnavailable: browser host task cancelled"


def _deadline_passed(deadline_at, now: Optional[str] = None) -> bool:
    """True when a dispatch's ISO deadline is at/behind *now* (UTC ISO order)."""
    if not deadline_at:
        return False
    return str(deadline_at) <= (now or _now_iso())


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _new_task_id() -> str:
    """Generate a stable, sortable, unique task_id."""
    return f"task-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _resolve_bot(session_key: Optional[str]) -> dict:
    """Resolve Bot identity for *session_key* (best-effort, never raises).

    Returns a dict with ``bot_id`` and ``rift`` (either may be ``None``) so the
    task row can record the identity chain even when no Bot is bound.
    """
    result = {"bot_id": None, "rift": None}
    if not session_key:
        return result
    try:
        import bots  # local import to keep the store decoupled at import time
        registry = bots.load_bots()
        bot = registry.get(session_key)
        if bot is not None:
            result["bot_id"] = bot.get("id")
            result["rift"] = bot.get("rift")
    except Exception:
        # A registry failure must not block submission.
        pass
    return result


class CloudTaskStore:
    """SQLite-backed persistent store for the Cloud task lifecycle.

    Thread-safe via a single connection guarded by a lock, and process-safe via
    ``BEGIN IMMEDIATE`` transactions for the atomic claim path.  All paths are
    resolved under ``DATA_DIR`` so the database survives restarts.
    """

    def __init__(self, db_path: Optional[str | Path] = None):
        if db_path is None:
            db_path = DATA_DIR / DEFAULT_DB_NAME
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the worker's heartbeat thread and the API
        # request handlers may both touch the connection; access is serialised
        # by _lock below.
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False
        )
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # Another process holds the write lock; WAL mode persists in the
            # DB file, so a mode set by a concurrent opener is sufficient.
            pass
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        self._init_schema()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id          TEXT PRIMARY KEY,
                    session_key     TEXT NOT NULL,
                    claimed_by      TEXT,
                    claimed_at      TEXT,
                    bot_id          TEXT,
                    bot_prefix      TEXT,
                    rift            TEXT,
                    chat_id         TEXT,
                    executor_prefix TEXT NOT NULL DEFAULT 'repo',
                    repo_url        TEXT,
                    task_text       TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    run_id          TEXT,
                    result          TEXT,
                    error           TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at      TEXT NOT NULL,
                    started_at      TEXT,
                    heartbeat_at    TEXT,
                    finished_at     TEXT,
                    updated_at      TEXT NOT NULL,
                    conversation_id TEXT,
                    parent_delegation_id TEXT
                );

                CREATE TABLE IF NOT EXISTS approval_requests (
                    approval_id TEXT PRIMARY KEY,
                    task_id     TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    chat_id     TEXT,
                    message_id  TEXT NOT NULL,
                    tier        INTEGER,
                    token       TEXT,
                    summary     TEXT,
                    detail      TEXT,
                    decision    TEXT NOT NULL DEFAULT 'pending',
                    created_at  TEXT NOT NULL,
                    resolved_at TEXT,
                    operator_reply TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_approval_task
                    ON approval_requests(task_id, decision);

                CREATE TABLE IF NOT EXISTS task_events (
                    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id    TEXT NOT NULL,
                    type       TEXT NOT NULL,
                    payload    TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_task
                    ON task_events(task_id, event_id);

                CREATE TABLE IF NOT EXISTS workers (
                    worker_id  TEXT PRIMARY KEY,
                    last_seen  TEXT NOT NULL,
                    started_at TEXT NOT NULL
                );

                -- Bot-to-Bot delegation records (single-level coordination).
                -- A delegation links a coordinator Bot's conversation to an
                -- ORDINARY target-Bot task created through this same store.
                -- It carries only non-secret metadata: identities, the task
                -- text, a lifecycle status, timestamps, and a SANITIZED final
                -- result summary. Provider keys, headers, tokens, Rift paths,
                -- system prompts, raw approval secrets, and browser-session
                -- metadata are never stored here.
                CREATE TABLE IF NOT EXISTS delegations (
                    delegation_id       TEXT PRIMARY KEY,
                    owner               TEXT NOT NULL,
                    coordinator_bot_id  TEXT NOT NULL,
                    target_bot_id       TEXT NOT NULL,
                    parent_conversation_id TEXT,
                    parent_task_id      TEXT,
                    parent_delegation_id TEXT,
                    depth               INTEGER NOT NULL DEFAULT 1,
                    executor_prefix     TEXT NOT NULL DEFAULT 'repo',
                    task_id             TEXT,
                    task_text           TEXT NOT NULL,
                    status              TEXT NOT NULL,
                    result_summary      TEXT,
                    error               TEXT,
                    created_at          TEXT NOT NULL,
                    updated_at          TEXT NOT NULL,
                    finished_at         TEXT,
                    -- Set once this delegation's terminal result has been
                    -- relayed into the parent coordinator conversation. NULL
                    -- means the result has not yet been announced; the relay
                    -- is idempotent (announce exactly once, never a duplicate).
                    relayed_at          TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_delegation_owner
                    ON delegations(owner, created_at);
                CREATE INDEX IF NOT EXISTS idx_delegation_conversation
                    ON delegations(parent_conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_delegation_task
                    ON delegations(task_id);

                -- Browser Host dispatch bridge (durable cross-process
                -- request/reply). The live host channel is owned in-memory by
                -- the socket-owning process (the FastAPI web app); serve.run_task
                -- runs in the SEPARATE worker process, whose HostManager owns no
                -- channels. This table is the durable handoff that lets the
                -- worker ask the socket owner to run the dispatch and read back
                -- its progress + terminal result — the same request/reply
                -- discipline as approval_requests.operator_reply, in reverse.
                -- It carries NO secret: owner/bot/host ids, the (already
                -- non-secret) task text, and the redacted result payload.
                CREATE TABLE IF NOT EXISTS browser_dispatches (
                    dispatch_id  TEXT PRIMARY KEY,
                    task_id      TEXT NOT NULL,
                    owner        TEXT NOT NULL,
                    bot_id       TEXT NOT NULL,
                    host_id      TEXT,
                    session_id   TEXT,
                    task_text    TEXT NOT NULL,
                    status       TEXT NOT NULL DEFAULT 'pending',
                    claimed_by   TEXT,
                    claim_token  TEXT,
                    claimed_at   TEXT,
                    result       TEXT,
                    error        TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    deadline_at  TEXT,
                    progress     TEXT NOT NULL DEFAULT '[]',
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    finished_at  TEXT,
                    -- Authorization and browser-profile identities normally
                    -- match.  The fixed Level 6 reader is the sole exception:
                    -- its dedicated Bot authorizes the operation while the
                    -- owner's existing browser-bot profile supplies the
                    -- authenticated Facebook session.
                    profile_bot_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_browser_dispatch_status
                    ON browser_dispatches(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_browser_dispatch_task
                    ON browser_dispatches(task_id);
                """
            )
            self._conn.commit()
            # Backfill columns added after first deployment so an existing
            # cloud_tasks.db is never left with a schema/query mismatch (the
            # row-to-task mapping is positional and assumes every column
            # defined above is present).
            existing_cols = {
                r[1] for r in self._conn.execute(
                    "PRAGMA table_info(tasks)"
                ).fetchall()
            }
            if "claimed_at" not in existing_cols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN claimed_at TEXT")
                self._conn.commit()
            if "chat_id" not in existing_cols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN chat_id TEXT")
                self._conn.commit()
            if "conversation_id" not in existing_cols:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN conversation_id TEXT"
                )
                self._conn.commit()
            if "parent_delegation_id" not in existing_cols:
                self._conn.execute(
                    "ALTER TABLE tasks ADD COLUMN parent_delegation_id TEXT"
                )
                self._conn.commit()

            # Delegation rows predating the result-relay marker get the column
            # appended, keeping the positional SELECT * mapping in step. The
            # relay marker is the ONLY durable record that a terminal result
            # has already been announced to the coordinator conversation, so
            # the announce-once guarantee survives process restarts.
            delegation_cols = {
                r[1] for r in self._conn.execute(
                    "PRAGMA table_info(delegations)"
                ).fetchall()
            }
            if delegation_cols and "relayed_at" not in delegation_cols:
                self._conn.execute(
                    "ALTER TABLE delegations ADD COLUMN relayed_at TEXT"
                )
                self._conn.commit()

            approval_cols = {
                r[1] for r in self._conn.execute(
                    "PRAGMA table_info(approval_requests)"
                ).fetchall()
            }
            if "operator_reply" not in approval_cols:
                self._conn.execute(
                    "ALTER TABLE approval_requests ADD COLUMN operator_reply TEXT"
                )
                self._conn.commit()

            browser_dispatch_cols = {
                r[1] for r in self._conn.execute(
                    "PRAGMA table_info(browser_dispatches)"
                ).fetchall()
            }
            if (browser_dispatch_cols
                    and "profile_bot_id" not in browser_dispatch_cols):
                self._conn.execute(
                    "ALTER TABLE browser_dispatches "
                    "ADD COLUMN profile_bot_id TEXT"
                )
                self._conn.commit()

    # ── Submission ──────────────────────────────────────────────────────

    def submit(
        self,
        session_key: str,
        task_text: str,
        repo_url: Optional[str] = None,
        executor_prefix: str = "repo",
        bot_id: Optional[str] = None,
        bot_prefix: Optional[str] = None,
        rift: Optional[str] = None,
        chat_id: Optional[str] = None,
        task_id: Optional[str] = None,
        resolve_bot: bool = True,
        conversation_id: Optional[str] = None,
        parent_delegation_id: Optional[str] = None,
    ) -> str:
        """Create a new queued task and return its stable task_id.

        *conversation_id* is the durable Chat conversation identity. It is
        recorded on the row so the worker can hand the engine a
        per-conversation session directory — two conversations bound to the
        same Bot must never share durable engine history. It is independent of
        *session_key* (which stays the per-Bot serialisation key).

        *resolve_bot=False* (web-submitted tasks) never auto-resolves a
        registered Bot merely because *session_key* happens to equal a Bot id:
        the row stays unbound unless an explicit *bot_id*/*rift* is supplied,
        and the worker honours that by running the task with bot resolution
        disabled.  Telegram/bot submissions keep the default registry lookup.

        Raises :class:`DuplicateTaskId` if *task_id* is supplied and already
        exists (the store refuses duplicate task IDs).
        """
        if not session_key:
            raise TaskStoreError("session_key is required")
        if not task_text or not task_text.strip():
            raise TaskStoreError("task_text is required")

        task_id = task_id or _new_task_id()
        now = _now_iso()

        # Resolve Bot identity when not explicitly supplied, recording the
        # identity chain (bot_id -> session_key -> rift) durably.  Web tasks
        # opt out: their session_key is a GitHub username that must never
        # bind a Bot just because the ids happen to match.
        if (bot_id is None or rift is None) and resolve_bot:
            resolved = _resolve_bot(session_key)
            bot_id = bot_id if bot_id is not None else resolved["bot_id"]
            rift = rift if rift is not None else resolved["rift"]

        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing:
                raise DuplicateTaskId(f"task_id {task_id!r} already exists")

            self._conn.execute(
                """
                INSERT INTO tasks (
                    task_id, session_key, bot_id, bot_prefix, rift, chat_id,
                    executor_prefix, repo_url, task_text, status,
                    cancel_requested, created_at, updated_at, conversation_id,
                    parent_delegation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                """,
                (
                    task_id, session_key, bot_id, bot_prefix, rift, chat_id,
                    executor_prefix, repo_url, task_text, STATUS_QUEUED,
                    now, now, conversation_id, parent_delegation_id,
                ),
            )
            self._conn.commit()

        self.add_event(task_id, "submitted", {
            "session_key": session_key,
            "executor_prefix": executor_prefix,
        })
        return task_id

    # ── Retrieval ──────────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[dict]:
        """Return the task dict for *task_id*, or ``None`` if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_task(row)

    def latest_active_task_for_conversation(
        self, conversation_id: str
    ) -> Optional[dict]:
        """Newest NON-TERMINAL task linked to *conversation_id*, or ``None``.

        Durable-only: reads the ``tasks`` table and nothing else. The Chat
        sidebar uses it to advertise a conversation's active work (queued /
        running / awaiting_approval) for exactly as long as the store says so
        — a terminal (or absent) task is never active work, so the line can
        clear without any synthesized state.
        """
        cid = str(conversation_id or "").strip()
        if not cid:
            return None
        placeholders = ", ".join("?" for _ in NONTERMINAL_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks WHERE conversation_id = ? "
                f"AND status IN ({placeholders}) "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (cid, *NONTERMINAL_STATUSES),
            ).fetchall()
        return self._row_to_task(rows[0]) if rows else None

    def list_tasks(
        self,
        status: Optional[str] = None,
        session_key: Optional[str] = None,
        bot_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return task dicts ordered newest-first, optionally filtered."""
        clauses = []
        params: list = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if session_key is not None:
            clauses.append("session_key = ?")
            params.append(session_key)
        if bot_id is not None:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tasks{where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            return [self._row_to_task(r) for r in rows]

    def _row_to_task(self, row) -> dict:
        cols = [
            "task_id", "session_key", "claimed_by", "claimed_at", "bot_id",
            "bot_prefix", "rift", "chat_id",
            "executor_prefix", "repo_url", "task_text", "status", "run_id",
            "result", "error", "cancel_requested", "created_at",
            "started_at", "heartbeat_at", "finished_at", "updated_at",
            "conversation_id", "parent_delegation_id",
        ]
        task = {c: row[i] for i, c in enumerate(cols)}
        task["cancel_requested"] = bool(task["cancel_requested"])
        if task["result"]:
            try:
                import json
                task["result"] = json.loads(task["result"])
            except (json.JSONDecodeError, TypeError):
                pass  # leave as raw string if unparseable
        return task

    def status(self, task_id: str) -> Optional[str]:
        """Return the current status string for *task_id*, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            return row[0] if row else None

    # ── Atomic claiming ──────────────────────────────────────────────────

    def claim_next(self, worker_id: str) -> Optional[dict]:
        """Atomically claim the oldest queued task and mark it running.

        Serialises execution per session key: a task whose ``session_key``
        already has a task in ``running`` / ``awaiting_approval`` is skipped, so
        the same Bot (same session key) never runs two tasks at once — even
        across multiple worker processes.  Different-Bot tasks are claimable
        concurrently.

        Returns the claimed task dict, or ``None`` if nothing is claimable.
        """
        run_id = uuid.uuid4().hex
        now = _now_iso()
        with self._lock:
            # BEGIN IMMEDIATE takes the write lock so two processes cannot both
            # select the same candidate.  The NOT IN (active sessions) clause
            # enforces per-session serialisation.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT task_id FROM tasks
                    WHERE status = ?
                      AND cancel_requested = 0
                      AND session_key NOT IN (
                          SELECT DISTINCT session_key FROM tasks
                          WHERE status IN (?, ?)
                      )
                    ORDER BY created_at ASC, rowid ASC
                    LIMIT 1
                    """,
                    (STATUS_QUEUED, STATUS_RUNNING, STATUS_AWAITING_APPROVAL),
                ).fetchall()
                if not rows:
                    self._conn.execute("COMMIT")
                    return None
                candidate_id = rows[0][0]
                cur = self._conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, claimed_by = ?, claimed_at = ?,
                        run_id = ?, started_at = ?, heartbeat_at = ?, updated_at = ?
                    WHERE task_id = ? AND status = ?
                    """,
                    (
                        STATUS_RUNNING, worker_id, now, run_id, now, now, now,
                        candidate_id, STATUS_QUEUED,
                    ),
                )
                if cur.rowcount != 1:
                    # Lost the race (extremely unlikely under IMMEDIATE): bail.
                    self._conn.execute("COMMIT")
                    return None
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (candidate_id,)
                ).fetchone()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self.add_event(candidate_id, "claimed", {
            "worker_id": worker_id, "run_id": run_id,
        })
        return self._row_to_task(row)

    # ── State transitions ────────────────────────────────────────────────

    def set_status(self, task_id: str, status: str, **extra) -> None:
        """Update a task's status (with validation) and ``updated_at``."""
        if status not in VALID_STATUSES:
            raise TaskStoreError(f"invalid status {status!r}")
        now = _now_iso()
        fields = ["status = ?", "updated_at = ?"]
        params: list = [status, now]
        if status in TERMINAL_STATUSES and "finished_at" not in extra:
            fields.append("finished_at = ?")
            params.append(now)
        for k, v in extra.items():
            fields.append(f"{k} = ?")
            params.append(v)
        params.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE task_id = ?",
                params,
            )
            self._conn.commit()
        self.add_event(task_id, "status", {"status": status})

    def touch(self, task_id: str) -> None:
        """Refresh ``heartbeat_at`` for an active task lease."""
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET heartbeat_at = ?, updated_at = ? "
                "WHERE task_id = ? AND status IN (?, ?)",
                (now, now, task_id, STATUS_RUNNING, STATUS_AWAITING_APPROVAL),
            )
            self._conn.commit()

    def renew_task_lease(self, task_id: str, worker_id: str) -> int:
        """Renew ONE task's lease, and only while it is genuinely active.

        Per-task (not worker-wide) so a task whose executor thread has died or
        wedged stops being renewed: its ``heartbeat_at`` then ages out and
        ``recover_stale`` can reclaim it.  Still scoped to ``claimed_by`` and to
        the non-terminal active statuses, so a task that completed, was
        cancelled, or was re-claimed cannot be kept alive from here.

        Returns the number of rows renewed (0 when the task is no longer this
        worker's active task).
        """
        now = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET heartbeat_at = ?, updated_at = ? "
                "WHERE task_id = ? AND claimed_by = ? AND status IN (?, ?)",
                (now, now, task_id, worker_id,
                 STATUS_RUNNING, STATUS_AWAITING_APPROVAL),
            )
            self._conn.commit()
            return cur.rowcount

    def complete(self, task_id: str, result: dict) -> Optional[str]:
        """Persist a result and finalise the task.

        Atomically re-checks ``cancel_requested`` under the same lock as the
        terminal write: a cancellation recorded *before* finalisation wins
        over a captured result, so `request_cancel` accepted for a running
        task is never silently lost when the executor completes afterwards
        (the race: result captured -> cancel accepted -> finalisation marks
        done).  When no cancellation was requested the task is finalised as
        ``done``/``failed`` exactly as before.

        Returns the applied final status (``done``/``failed``/``cancelled``),
        or ``None`` if no such task exists.
        """
        import json
        result = result or {}
        status = STATUS_DONE
        if result.get("status") in _FAILED_EXECUTOR_STATUSES:
            status = STATUS_FAILED
        now = _now_iso()
        with self._lock:
            row = self._conn.execute(
                "SELECT status, cancel_requested FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            cur_status, cancel_requested = row[0], bool(row[1])
            if cur_status in TERMINAL_STATUSES:
                return cur_status
            if cancel_requested:
                # A cancellation is pending: it wins even though the executor
                # produced a result.  Do not persist the result as if the run
                # completed — the operator asked to stop.
                applied = STATUS_CANCELLED
                self._conn.execute(
                    "UPDATE tasks SET status = ?, error = ?, finished_at = ?, "
                    "updated_at = ? WHERE task_id = ?",
                    (STATUS_CANCELLED,
                     "cancelled during run (operator request)", now, now,
                     task_id),
                )
            else:
                applied = status
                self._conn.execute(
                    "UPDATE tasks SET status = ?, result = ?, finished_at = ?, "
                    "updated_at = ? WHERE task_id = ?",
                    (status, json.dumps(result, sort_keys=True), now, now,
                     task_id),
                )
            self._conn.commit()
        # Emit lifecycle events outside the lock (add_event re-acquires it).
        if applied == STATUS_CANCELLED:
            self.add_event(task_id, "cancelled",
                           {"reason": "cancelled during run (operator request)"})
        self.add_event(task_id, "status", {"status": applied})
        return applied

    def fail(self, task_id: str, error: str) -> None:
        """Persist a failure and move the task to ``failed``."""
        self.set_status(task_id, STATUS_FAILED, error=error or "unknown failure")

    def cancel_effective(self, task_id: str, reason: str = "cancelled") -> None:
        """Mark a task cancelled (used after a cancellation/rejection)."""
        self.set_status(task_id, STATUS_CANCELLED, error=reason)
        # Emit a "cancelled" lifecycle marker so every cancellation path
        # (queued cancel, mid-run cancel, approval cancel, complete-guard)
        # produces the same stream signal the API/flux views rely on.
        self.add_event(task_id, "cancelled", {"reason": reason})

    def is_cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return bool(row and row[0])

    def request_cancel(self, task_id: str) -> bool:
        """Request cancellation of a running task (best-effort interruption).

        Returns ``True`` if a cancellation was recorded or applied.  A queued
        task is cancelled immediately; a running task has its flag set and is
        cancelled by the worker at the next approval gate.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT status, cancel_requested FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            status, already = row[0], bool(row[1])
            if status in TERMINAL_STATUSES:
                return False
            if already:
                return True
            if status == STATUS_QUEUED:
                # Immediately cancellable.
                now = _now_iso()
                self._conn.execute(
                    "UPDATE tasks SET cancel_requested = 1, status = ?, "
                    "finished_at = ?, updated_at = ? WHERE task_id = ?",
                    (STATUS_CANCELLED, now, now, task_id),
                )
                self._conn.commit()
                event = ("cancelled", {"reason": "queued cancel"})
            else:
                # running / awaiting_approval: record the request; worker applies it.
                self._conn.execute(
                    "UPDATE tasks SET cancel_requested = 1, updated_at = ? "
                    "WHERE task_id = ?",
                    (_now_iso(), task_id),
                )
                self._conn.commit()
                event = ("cancel_requested", {})
        # Emit the lifecycle event *outside* the lock so we do not re-enter the
        # non-reentrant self._lock that add_event() also acquires (matches
        # set_status()).  The database/state update above is already committed.
        self.add_event(task_id, event[0], event[1])
        return True

    def cancel(self, task_id: str) -> bool:
        """Cancel a task (immediate for queued; flag for running)."""
        return self.request_cancel(task_id)

    # ── Durable approval requests ─────────────────────────────────────────

    def persist_approval_request(
        self,
        task_id: str,
        session_key: str,
        message_id: str,
        tier: int,
        token: str,
        summary: str,
        detail: str,
    ) -> str:
        """Record a durable, task_id-linked approval request."""
        approval_id = f"apr-{uuid.uuid4().hex[:10]}"
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, task_id, session_key, message_id, tier,
                    token, summary, detail, decision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    approval_id, task_id, session_key, str(message_id),
                    tier, token, summary, detail, now,
                ),
            )
            self._conn.commit()
        # Surface the transition in the task lifecycle too.
        if self.status(task_id) == STATUS_RUNNING:
            self.set_status(task_id, STATUS_AWAITING_APPROVAL)
        self.add_event(task_id, "approval_requested", {
            "tier": tier, "summary": summary, "approval_id": approval_id,
        })
        return approval_id

    def get_pending_approval(self, task_id: str) -> Optional[dict]:
        """Return the pending approval_request dict for *task_id*, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approval_requests WHERE task_id = ? "
                "AND decision = 'pending' ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return self._row_to_approval(row) if row else None

    def pending_approvals_for_task(self, task_id: str) -> list[dict]:
        """Return EVERY pending approval_request for *task_id* (newest first).

        Used by the host-side delegated-approve path to prove a task has
        EXACTLY ONE pending approval before resolving it. ``get_pending_approval``
        deliberately returns only the newest; callers that must fail closed on
        an ambiguous (zero-or-many) pending set need the full list.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approval_requests WHERE task_id = ? "
                "AND decision = 'pending' ORDER BY created_at DESC",
                (task_id,),
            ).fetchall()
            return [self._row_to_approval(row) for row in rows]

    def record_operator_reply(self, task_id: str, reply_text: str) -> bool:
        """Persist one raw operator reply for the task's pending approval.

        This is intentionally a durable handoff only.  The worker process later
        delivers the reply to the live ``serve`` approval handler, which remains
        responsible for waking the executor and resolving the approval.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approval_requests SET operator_reply = ? "
                "WHERE task_id = ? AND decision = 'pending' "
                "AND operator_reply IS NULL",
                (reply_text, task_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def pending_operator_replies(self, limit: int = 50) -> list[dict]:
        """Return pending approvals carrying an undelivered operator reply."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approval_requests "
                "WHERE decision = 'pending' AND operator_reply IS NOT NULL "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_approval(row) for row in rows]

    def clear_operator_reply(self, approval_id: str) -> bool:
        """Clear a reply after the live approval handler accepted it."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approval_requests SET operator_reply = NULL "
                "WHERE approval_id = ? AND operator_reply IS NOT NULL",
                (approval_id,),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def resolve_approval_request(
        self, task_id: str, message_id: str, decision: str
    ) -> None:
        """Mark an approval_request decided and return to running."""
        now = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE approval_requests SET decision = ?, resolved_at = ? "
                "WHERE task_id = ? AND message_id = ? AND decision = 'pending'",
                (decision, now, task_id, str(message_id)),
            )
            self._conn.commit()
        if cur.rowcount != 1:
            return
        if self.status(task_id) == STATUS_AWAITING_APPROVAL:
            self.set_status(task_id, STATUS_RUNNING)
        self.add_event(task_id, "approval_resolved", {"decision": decision})

    def _row_to_approval(self, row) -> dict:
        cols = [
            "approval_id", "task_id", "session_key", "chat_id", "message_id",
            "tier", "token", "summary", "detail", "decision",
            "created_at", "resolved_at", "operator_reply",
        ]
        return {c: row[i] for i, c in enumerate(cols)}

    # ── Events (durable, restart-safe stream) ─────────────────────────────

    def add_event(self, task_id: str, event_type: str, payload: dict) -> None:
        """Append a durable event to the task's event stream."""
        import json
        now = _now_iso()
        try:
            payload_json = json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError):
            payload_json = json.dumps({"unserialisable": True})
        with self._lock:
            self._conn.execute(
                "INSERT INTO task_events (task_id, type, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, event_type, payload_json, now),
            )
            self._conn.commit()

    def get_events(self, task_id: str, after_event_id: int = 0) -> list[dict]:
        """Return events for *task_id* (optionally only those after an id)."""
        import json
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, type, payload, created_at FROM task_events "
                "WHERE task_id = ? AND event_id > ? ORDER BY event_id ASC",
                (task_id, after_event_id),
            ).fetchall()
        events = []
        for event_id, etype, payload, created_at in rows:
            try:
                p = json.loads(payload) if payload else {}
            except (json.JSONDecodeError, TypeError):
                p = {}
            events.append({
                "event_id": event_id,
                "type": etype,
                "payload": p,
                "created_at": created_at,
            })
        return events

    # ── Worker liveness / recovery ────────────────────────────────────────

    def register_worker(self, worker_id: str) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO workers (worker_id, last_seen, started_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(worker_id) DO UPDATE SET last_seen = excluded.last_seen",
                (worker_id, now, now),
            )
            self._conn.commit()

    def heartbeat_worker(self, worker_id: str) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE workers SET last_seen = ? WHERE worker_id = ?",
                (now, worker_id),
            )
            self._conn.commit()

    def live_workers(self, heartbeat_timeout: int = 300) -> list[str]:
        """Return worker_ids seen within *heartbeat_timeout* seconds."""
        with self._lock:
            return self._live_workers_locked(heartbeat_timeout)

    def _live_workers_locked(self, heartbeat_timeout: int = 300) -> list[str]:
        """Like :meth:`live_workers` but assumes ``self._lock`` is held.

        Used by ``recover_stale`` to re-derive liveness *inside* the same
        critical section as the terminal UPDATE, so a worker that came back
        alive after the candidate scan is not treated as dead (the self._lock
        is non-reentrant, so the public method cannot be called there).
        """
        cutoff = time.time() - heartbeat_timeout
        rows = self._conn.execute(
            "SELECT worker_id, last_seen FROM workers"
        ).fetchall()
        live = []
        for worker_id, last_seen in rows:
            try:
                ts = datetime.fromisoformat(last_seen).timestamp()
            except (ValueError, TypeError):
                continue
            if ts >= cutoff:
                live.append(worker_id)
        return live

    def recover_stale(
        self,
        live_worker_ids: Optional[set] = None,
        heartbeat_timeout: int = 300,
        task_stale_after: int = TASK_STALE_AFTER,
        approval_stale_after: int = APPROVAL_STALE_AFTER,
        now: Optional[datetime] = None,
        protect_task_ids: Optional[set] = None,
    ) -> list[dict]:
        """Recover tasks orphaned by dead workers (restart-safe).

        A task left in ``running`` or ``awaiting_approval`` whose worker is no
        longer alive is recovered:
          * ``running``     -> ``failed``  (crash / interrupted)
          * ``awaiting_approval`` -> ``cancelled`` (interrupted approval)

        A task is recovered when its worker is dead OR its task lease has
        expired. Per-task leases are renewed ONLY while a task's executor
        thread is genuinely live, so a wedged/abandoned task is no longer kept
        alive by the worker's process heartbeat. ``task_stale_after`` and
        ``approval_stale_after`` are the respective lease durations.

        Race safety: the terminal decision is a single conditional UPDATE that
        rechecks AT WRITE TIME (a) that the task is still in the status the scan
        saw, (b) that the owning worker is still not live — liveness is
        re-derived inside the update's critical section rather than from the
        scan snapshot, so a worker that (re)registered after the scan is not
        treated as dead — and (c) that the lease is still expired. A lease
        renewed or a status changed after the scan therefore cancels recovery.

        ``protect_task_ids`` names tasks the caller knows it is actively
        executing; a worker must never recover its own fresh task.

        If *live_worker_ids* is ``None`` it is derived from the workers table
        using *heartbeat_timeout*. Returns the list of recovered task dicts.
        """
        protect = set(protect_task_ids or set())
        if live_worker_ids is None:
            live_worker_ids = set(self.live_workers(heartbeat_timeout))
        live_worker_ids = set(live_worker_ids or set())
        now = now or datetime.now(timezone.utc)
        running_cutoff = (now - timedelta(seconds=task_stale_after)).isoformat()
        approval_cutoff = (now - timedelta(seconds=approval_stale_after)).isoformat()

        recovered: list[dict] = []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?)
                  AND (
                    claimed_by IS NULL
                    OR claimed_by NOT IN ({live_workers})
                    OR (status = ? AND
                        (heartbeat_at IS NULL OR heartbeat_at <= ?))
                    OR (status = ? AND
                        (heartbeat_at IS NULL OR heartbeat_at <= ?))
                  )
                """.format(
                    live_workers=(", ".join("?" for _ in live_worker_ids)
                                  if live_worker_ids else "SELECT NULL WHERE 0")
                ),
                [STATUS_RUNNING, STATUS_AWAITING_APPROVAL,
                 *sorted(live_worker_ids), STATUS_RUNNING, running_cutoff,
                 STATUS_AWAITING_APPROVAL, approval_cutoff],
            ).fetchall()
            tasks = [self._row_to_task(r) for r in rows]

        for task in tasks:
            task_id = task["task_id"]
            # Never recover a task this caller is actively executing.
            if task_id in protect:
                continue
            status = task["status"]
            stale_cutoff = (
                approval_cutoff
                if status == STATUS_AWAITING_APPROVAL else running_cutoff
            )
            terminal = (
                STATUS_CANCELLED
                if status == STATUS_AWAITING_APPROVAL else STATUS_FAILED
            )
            reason = (
                "recovered after restart: approval interrupted"
                if status == STATUS_AWAITING_APPROVAL
                else "recovered after restart: worker not active"
            )
            # Final conditional UPDATE: recheck status, lease freshness, and
            # worker LIVENESS at write time. Liveness is re-derived inside this
            # critical section so a worker that (re)registered/heartbeated
            # after the scan is not treated as dead.
            with self._lock:
                live_now = set(self._live_workers_locked(heartbeat_timeout))
                if live_now:
                    live_clause = "claimed_by NOT IN (%s)" % ", ".join(
                        "?" for _ in live_now
                    )
                    live_params: list = sorted(live_now)
                else:
                    live_clause = "1 = 1"
                    live_params = []
                cur = self._conn.execute(
                    "UPDATE tasks SET status = ?, error = ?, finished_at = ?, "
                    "updated_at = ? WHERE task_id = ? AND status = ? AND "
                    f"({live_clause} OR heartbeat_at IS NULL OR heartbeat_at <= ?)",
                    (terminal, reason, _now_iso(), _now_iso(), task_id,
                     status, *live_params, stale_cutoff),
                )
                applied = cur.rowcount == 1
                if applied and status == STATUS_AWAITING_APPROVAL:
                    # Fold the approval-timeout write into the same transaction
                    # as the terminal UPDATE so the durable task row and its
                    # pending approval never disagree.
                    self._conn.execute(
                        "UPDATE approval_requests SET decision = 'timeout', "
                        "resolved_at = ? WHERE task_id = ? AND decision = 'pending'",
                        (_now_iso(), task_id),
                    )
                self._conn.commit()
            if not applied:
                # A live worker renewed the lease, or the task changed status,
                # between the scan and this write: leave valid work alone.
                continue
            self.add_event(task_id, "status", {"status": terminal})
            if status == STATUS_AWAITING_APPROVAL:
                self.add_event(task_id, "cancelled", {"reason": reason})
            recovered.append(self.get(task_id))
        return recovered

    # ── Approval routing (preserves the existing serve approval protocol) ──

    def respond(self, task_id: str, text: str) -> bool:
        """Route an operator reply to the pending approval for *task_id*.

        This preserves the existing approval protocol: it resolves the in-memory
        ``pending_approvals`` entry that ``serve.run_task`` created by calling
        ``serve.handle_approval_reply`` with the recorded ``(session_key,
        message_id)``.  Returns ``True`` if a pending approval was resolved.
        """
        approval = self.get_pending_approval(task_id)
        if approval is None:
            return False
        import serve  # local import to avoid a hard dependency at startup
        session_key = approval["session_key"]
        message_id = approval["message_id"]
        # The durable approval_requests row exists across processes, but
        # serve.pending_approvals is in-memory and process-local: run_task
        # executes in the worker process, so a reply from any other process
        # (e.g. the web API) has no live entry to signal.  Without this gate
        # handle_approval_reply consumes a plausible bare reply as a stale
        # reply and returns True — the operator is told "delivered" while the
        # worker's pending approval is never resolved and the executor times
        # out and denies.  Deliver only when the live entry exists in THIS
        # process; otherwise report undelivered so a reply is never silently
        # eaten and the durable approval stays pending for a real responder.
        if (session_key, message_id) not in serve.pending_approvals:
            return False
        # A reply must originate from the TASK's chat — the operator. For a
        # Bot-bound task (including a delegated one) the session key is the
        # Bot id while the chat is the owner, so passing the session key as
        # the chat would make serve.handle_approval_reply's cross-chat guard
        # reject every reply. Fall back to the session key for Telegram-style
        # tasks where the two coincide.
        task = self.get(task_id) or {}
        reply_chat_id = str(task.get("chat_id") or "").strip() or session_key
        # handle_approval_reply(chat_id, reply_text, reply_to_id, session_key)
        return serve.handle_approval_reply(
            chat_id=reply_chat_id,
            reply_text=text,
            reply_to_id=message_id,
            session_key=session_key,
        )

    def deliver_operator_replies(self, limit: int = 50) -> int:
        """Deliver durable replies to this worker process's live approvals.

        ``serve.handle_approval_reply`` remains the only resolver.  Replies
        that arrive before the executor has registered its in-memory approval
        are left durable for the next poll; accepted replies are cleared only
        after the handler returns true.  A repeated poll is harmless because
        the live handler no longer has a matching pending entry after it has
        consumed the reply.
        """
        import serve
        delivered = 0
        for approval in self.pending_operator_replies(limit):
            key = (approval["session_key"], approval["message_id"])
            if key not in serve.pending_approvals:
                continue
            # Reply chat = the TASK's chat (the operator), not the Bot session
            # key — see respond() for why a delegated/Bot-bound approval would
            # otherwise be unresolvable.
            task = self.get(approval["task_id"]) or {}
            reply_chat_id = (
                str(task.get("chat_id") or "").strip() or approval["session_key"]
            )
            accepted = serve.handle_approval_reply(
                chat_id=reply_chat_id,
                reply_text=approval["operator_reply"],
                reply_to_id=approval["message_id"],
                session_key=approval["session_key"],
            )
            if accepted and self.clear_operator_reply(approval["approval_id"]):
                delivered += 1
        return delivered

    # ── Bot-to-Bot delegation records ─────────────────────────────────────
    #
    # These methods own ONLY the durable delegation row. They never execute a
    # target (that is the existing worker's job) and never touch secrets: the
    # record holds identities, task text, lifecycle, timestamps, and a
    # sanitized result summary supplied by the caller.

    _DELEGATION_COLS = (
        "delegation_id", "owner", "coordinator_bot_id", "target_bot_id",
        "parent_conversation_id", "parent_task_id", "parent_delegation_id",
        "depth", "executor_prefix", "task_id", "task_text", "status",
        "result_summary", "error", "created_at", "updated_at", "finished_at",
        "relayed_at",
    )

    def _row_to_delegation(self, row) -> dict:
        rec = {c: row[i] for i, c in enumerate(self._DELEGATION_COLS)}
        try:
            rec["depth"] = int(rec.get("depth") or 1)
        except (TypeError, ValueError):
            rec["depth"] = 1
        return rec

    def create_delegation(
        self,
        *,
        owner: str,
        coordinator_bot_id: str,
        target_bot_id: str,
        task_text: str,
        parent_conversation_id: Optional[str] = None,
        parent_task_id: Optional[str] = None,
        parent_delegation_id: Optional[str] = None,
        depth: int = 1,
        executor_prefix: str = "repo",
        task_id: Optional[str] = None,
        status: str = STATUS_QUEUED,
        delegation_id: Optional[str] = None,
    ) -> str:
        """Create a durable delegation record and return its id.

        The row is created in the SAME store/transaction domain as tasks, so a
        delegation and the target task it spawns are always visible together.
        Only non-secret fields are accepted; callers must pass a sanitized
        summary via :meth:`set_delegation_result`.
        """
        owner = str(owner or "").strip()
        coordinator_bot_id = str(coordinator_bot_id or "").strip()
        target_bot_id = str(target_bot_id or "").strip()
        if not owner:
            raise TaskStoreError("delegation owner is required")
        if not coordinator_bot_id or not target_bot_id:
            raise TaskStoreError("delegation requires coordinator and target bot ids")
        if not task_text or not task_text.strip():
            raise TaskStoreError("delegation task_text is required")
        if status not in DELEGATION_STATUSES:
            raise TaskStoreError(f"invalid delegation status {status!r}")
        delegation_id = delegation_id or f"dlg-{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM delegations WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
            if existing:
                raise DuplicateTaskId(f"delegation_id {delegation_id!r} already exists")
            self._conn.execute(
                """
                INSERT INTO delegations (
                    delegation_id, owner, coordinator_bot_id, target_bot_id,
                    parent_conversation_id, parent_task_id, parent_delegation_id,
                    depth, executor_prefix, task_id, task_text, status,
                    result_summary, error, created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)
                """,
                (
                    delegation_id, owner, coordinator_bot_id, target_bot_id,
                    parent_conversation_id, parent_task_id, parent_delegation_id,
                    int(depth), executor_prefix, task_id, task_text, status,
                    now, now,
                ),
            )
            self._conn.commit()
        return delegation_id

    def get_delegation(self, delegation_id: str) -> Optional[dict]:
        """Return the delegation dict for *delegation_id*, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM delegations WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
        return self._row_to_delegation(row) if row is not None else None

    def get_delegation_for_task(self, task_id: str) -> Optional[dict]:
        """Return the delegation that spawned *task_id*, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM delegations WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return self._row_to_delegation(row) if row is not None else None

    def list_delegations(
        self,
        *,
        owner: Optional[str] = None,
        coordinator_bot_id: Optional[str] = None,
        target_bot_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return delegation dicts newest-first, optionally filtered."""
        clauses = []
        params: list = []
        if owner is not None:
            clauses.append("owner = ?")
            params.append(owner)
        if coordinator_bot_id is not None:
            clauses.append("coordinator_bot_id = ?")
            params.append(coordinator_bot_id)
        if target_bot_id is not None:
            clauses.append("target_bot_id = ?")
            params.append(target_bot_id)
        if parent_conversation_id is not None:
            clauses.append("parent_conversation_id = ?")
            params.append(parent_conversation_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM delegations{where} "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [self._row_to_delegation(r) for r in rows]

    def set_delegation_status(
        self,
        delegation_id: str,
        status: str,
        *,
        task_id: Optional[str] = None,
        error: Optional[str] = None,
        result_summary: Optional[str] = None,
    ) -> None:
        """Update a delegation's status (and optional links/summary)."""
        if status not in DELEGATION_STATUSES:
            raise TaskStoreError(f"invalid delegation status {status!r}")
        now = _now_iso()
        fields = ["status = ?", "updated_at = ?"]
        params: list = [status, now]
        if task_id is not None:
            fields.append("task_id = ?")
            params.append(task_id)
        if error is not None:
            fields.append("error = ?")
            params.append(error)
        if result_summary is not None:
            fields.append("result_summary = ?")
            params.append(result_summary)
        if status in DELEGATION_TERMINAL_STATUSES:
            fields.append("finished_at = ?")
            params.append(now)
        params.append(delegation_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE delegations SET {', '.join(fields)} WHERE delegation_id = ?",
                params,
            )
            self._conn.commit()

    def set_delegation_result(
        self, delegation_id: str, result_summary: str,
        *, status: str = STATUS_DONE,
    ) -> None:
        """Persist the sanitized final summary and finalize the delegation."""
        self.set_delegation_status(
            delegation_id, status, result_summary=str(result_summary or "")
        )

    def mark_delegation_relayed(
        self, delegation_id: str, *, at: Optional[str] = None,
    ) -> bool:
        """Record that a delegation's terminal result was announced once.

        Returns ``True`` only when THIS call performed the transition (the row
        was not already marked) — the durable compare-and-set that makes the
        result relay idempotent, so a terminal result is announced exactly once
        even across reloads, reconnects, and repeated polls. A second call is a
        no-op and returns ``False``.
        """
        now = at or _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE delegations SET relayed_at = ? "
                "WHERE delegation_id = ? AND relayed_at IS NULL",
                (now, delegation_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ── Browser Host dispatch bridge (durable cross-process handoff) ────
    #
    # The live host channel lives in the socket-owning process (FastAPI web
    # app); serve.run_task executes in the worker process. These methods are
    # the request/reply store — the same durable discipline as the approval
    # requests, in reverse: the worker creates a request and reads the
    # terminal result; the socket owner claims and fills it in. Every write
    # goes through BEGIN IMMEDIATE / conditional UPDATE so two processes
    # can never claim or finalize the same dispatch twice.

    def _row_to_browser_dispatch(self, row) -> dict:
        return {
            "dispatch_id": row["dispatch_id"],
            "task_id": row["task_id"],
            "owner": row["owner"],
            "bot_id": row["bot_id"],
            "profile_bot_id": row["profile_bot_id"] or row["bot_id"],
            "host_id": row["host_id"],
            "session_id": row["session_id"] or "",
            "task_text": row["task_text"],
            "status": row["status"],
            "claimed_by": row["claimed_by"],
            "claimed_at": row["claimed_at"],
            "result": row["result"],
            "error": row["error"],
            "cancel_requested": bool(row["cancel_requested"]),
            "progress": row["progress"],
            "created_at": row["created_at"],
        }

    def submit_browser_dispatch(
        self, *, task_id: str, owner: str, bot_id: str, host_id: Optional[str],
        task_text: str, session_id: str = "", timeout: int = 300,
        profile_bot_id: Optional[str] = None,
    ) -> str:
        """Create a pending owner-scoped, task-bound dispatch request."""
        dispatch_id = "bd-" + uuid.uuid4().hex[:16]
        now = _now_iso()
        try:
            deadline = (datetime.now(timezone.utc)
                        + timedelta(seconds=int(timeout))).isoformat()
        except Exception:
            deadline = now
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO browser_dispatches
                    (dispatch_id, task_id, owner, bot_id, host_id, session_id,
                     task_text, status, deadline_at, created_at, updated_at,
                     profile_bot_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (dispatch_id, task_id, owner, bot_id, host_id, session_id,
                 task_text, deadline, now, now,
                 str(profile_bot_id or bot_id)),
            )
            self._conn.commit()
        self.add_event(task_id, "browser_dispatch_submitted",
                       {"dispatch_id": dispatch_id, "host_id": host_id})
        return dispatch_id

    def _claim_browser_dispatch(
        self, claimant: str, *, limit: int, host_ids=None, exclude_host_ids=None,
    ) -> list[dict]:
        """Atomically claim up to ``limit`` pending dispatch requests.

        ``host_ids`` restricts claims to hosts the caller OWNS; when it is None
        every host is claimable. ``exclude_host_ids`` excludes the given hosts
        (used by a socket owner to find only requests it CANNOT serve). Each
        claim is a conditional UPDATE under BEGIN IMMEDIATE, so two processes
        (or the poller and a co-located worker) can never claim the same
        request twice — the loser's UPDATE matches zero rows.

        The scan is DEADLINE-AWARE inside the same transaction: a row whose
        deadline has passed is never selected and can never be claimed (the
        per-row UPDATE repeats the deadline predicate so a row that expires
        between scan and update is skipped too). Expired rows are left for
        :meth:`expire_browser_dispatches` to terminalize fail-closed.
        """
        claimed_ids: list[str] = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                scan_now = _now_iso()
                clauses = ["status = 'pending'",
                           "(deadline_at IS NULL OR deadline_at > ?)"]
                params: list = [scan_now]
                if host_ids is not None:
                    ids = [str(h) for h in host_ids if h]
                    if not ids:
                        self._conn.execute("COMMIT")
                        return []
                    clauses.append(
                        "host_id IN (%s)" % ", ".join("?" for _ in ids))
                    params.extend(ids)
                if exclude_host_ids is not None:
                    ids = [str(h) for h in exclude_host_ids if h]
                    if ids:
                        clauses.append(
                            "(host_id IS NULL OR host_id NOT IN (%s))"
                            % ", ".join("?" for _ in ids))
                        params.extend(ids)
                rows = self._conn.execute(
                    "SELECT dispatch_id FROM browser_dispatches "
                    "WHERE " + " AND ".join(clauses) +
                    " ORDER BY created_at ASC, rowid ASC LIMIT ?",
                    (*params, int(limit)),
                ).fetchall()
                for (dispatch_id,) in rows:
                    now = _now_iso()
                    # The deadline predicate is repeated HERE, inside the same
                    # transaction: a candidate whose deadline passed between
                    # the scan and this UPDATE is not claimed at all.
                    cur = self._conn.execute(
                        "UPDATE browser_dispatches "
                        "SET status = 'executing', claimed_by = ?, "
                        "    claim_token = ?, claimed_at = ?, updated_at = ? "
                        "WHERE dispatch_id = ? AND status = 'pending' "
                        "  AND (deadline_at IS NULL OR deadline_at > ?)",
                        (claimant, uuid.uuid4().hex, now, now, dispatch_id,
                         now),
                    )
                    if cur.rowcount == 1:
                        claimed_ids.append(dispatch_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        claimed = []
        for dispatch_id in claimed_ids:
            rec = self.get_browser_dispatch(dispatch_id)
            if rec is not None:
                claimed.append(rec)
        return claimed

    def claim_browser_dispatches(
        self, claimant: str, *, host_ids=None, limit: int = 4,
    ) -> list[dict]:
        """Claim pending requests this socket-owning process can serve."""
        return self._claim_browser_dispatch(
            claimant, limit=limit, host_ids=host_ids)

    def claim_one_browser_dispatch(self, dispatch_id: str, claimant: str) -> bool:
        """Claim one SPECIFIC request (the co-located fast path).

        Deadline-aware like the scan claim: an expired request is never
        claimed, so it can never reach the host.
        """
        now = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE browser_dispatches "
                "SET status = 'executing', claimed_by = ?, "
                "    claim_token = ?, claimed_at = ?, updated_at = ? "
                "WHERE dispatch_id = ? AND status = 'pending' "
                "  AND (deadline_at IS NULL OR deadline_at > ?)",
                (claimant, uuid.uuid4().hex, now, now, dispatch_id, now),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def expire_browser_dispatches(self) -> int:
        """Terminalize FAIL-CLOSED every dispatch whose deadline has passed.

        Single conditional UPDATE: rows currently ``pending`` (not yet claimed)
        or ``executing`` (claimed but never finished — e.g. the socket owner
        died mid-run, or the worker restarted) move to ``failed`` with the
        explicit expiry reason. Exactly-once by construction: the status
        predicate means an already-terminal row is never touched, and a second
        sweep finds nothing. Returns the number of rows terminalized.
        """
        now = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE browser_dispatches SET status = 'failed', error = ?, "
                "finished_at = ?, updated_at = ? "
                "WHERE status IN ('pending', 'executing') "
                "  AND deadline_at IS NOT NULL AND deadline_at <= ?",
                (BROWSER_DISPATCH_EXPIRED, now, now, now),
            )
            self._conn.commit()
        return cur.rowcount

    def browser_dispatch_admissible(self, dispatch_id: str) -> tuple[bool, str]:
        """Re-read durable state immediately before host dispatch.

        Returns ``(True, "")`` only when the request is still non-terminal,
        not expired, and not cancelled (neither the request nor its underlying
        task). Any other outcome returns ``(False, <fail-closed reason>)`` and
        the caller must NOT hand the work to a host. This closes the race
        between claiming (or a prior admission) and the actual dispatch: a
        timeout or an operator cancellation that lands in that window is
        honoured, never bypassed.
        """
        rec = self.get_browser_dispatch(dispatch_id)
        if rec is None:
            return False, "HostUnavailable: browser dispatch request not found"
        if rec["status"] not in ("pending", "executing"):
            return False, ("HostUnavailable: browser dispatch already "
                           "terminal (%s)" % rec["status"])
        if _deadline_passed(rec.get("deadline_at")):
            return False, BROWSER_DISPATCH_EXPIRED
        if rec.get("cancel_requested"):
            return False, BROWSER_DISPATCH_CANCELLED
        task_id = rec.get("task_id")
        if task_id:
            try:
                if self.is_cancel_requested(task_id):
                    return False, BROWSER_DISPATCH_CANCELLED
            except Exception:
                pass
        return True, ""

    def pending_browser_dispatches(
        self, *, exclude_host_ids=None, limit: int = 4,
    ) -> list[dict]:
        """Pending requests whose hosts the caller does NOT own."""
        with self._lock:
            clauses = ["status = 'pending'"]
            params: list = []
            if exclude_host_ids is not None:
                ids = [str(h) for h in exclude_host_ids if h]
                if ids:
                    clauses.append(
                        "(host_id IS NULL OR host_id NOT IN (%s))"
                        % ", ".join("?" for _ in ids))
                    params.extend(ids)
            rows = self._conn.execute(
                "SELECT dispatch_id FROM browser_dispatches "
                "WHERE " + " AND ".join(clauses) +
                " ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        out = []
        for (dispatch_id,) in rows:
            rec = self.get_browser_dispatch(dispatch_id)
            if rec is not None:
                out.append(rec)
        return out

    def get_browser_dispatch(self, dispatch_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM browser_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        if row is None:
            return None
        cols = [
            "dispatch_id", "task_id", "owner", "bot_id", "host_id",
            "session_id", "task_text", "status", "claimed_by", "claim_token",
            "claimed_at", "result", "error", "cancel_requested", "deadline_at",
            "progress", "created_at", "updated_at", "finished_at",
            "profile_bot_id",
        ]
        rec = {c: row[i] for i, c in enumerate(cols)}
        rec["cancel_requested"] = bool(rec["cancel_requested"])
        rec["session_id"] = rec["session_id"] or ""
        rec["profile_bot_id"] = rec["profile_bot_id"] or rec["bot_id"]
        return rec

    def record_browser_dispatch_progress(self, dispatch_id: str, note: dict) -> None:
        """Append one progress note to the request's durable progress log.

        A read-modify-write under the store lock: portable (no JSON1
        dependency) and still durable — every note survives into the result
        the worker relays.
        """
        import json
        with self._lock:
            row = self._conn.execute(
                "SELECT progress FROM browser_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
            progress: list = []
            if row is not None and row[0]:
                try:
                    loaded = json.loads(row[0])
                    if isinstance(loaded, list):
                        progress = loaded
                except (ValueError, TypeError):
                    progress = []
            progress.append(note)
            self._conn.execute(
                "UPDATE browser_dispatches SET progress = ?, updated_at = ? "
                "WHERE dispatch_id = ?",
                (json.dumps(progress, sort_keys=True), _now_iso(), dispatch_id),
            )
            self._conn.commit()

    def browser_dispatch_progress(
        self, dispatch_id: str, cursor: int = 0,
    ) -> tuple[list, int]:
        """Return ``(new_notes, new_cursor)`` for a dispatch request."""
        with self._lock:
            row = self._conn.execute(
                "SELECT progress FROM browser_dispatches WHERE dispatch_id = ?",
                (dispatch_id,),
            ).fetchone()
        progress = []
        if row is not None and row[0]:
            try:
                progress = json.loads(row[0])
            except (ValueError, TypeError):
                progress = []
        return progress[int(cursor):], len(progress)

    def _finalize_browser_dispatch(
        self, dispatch_id: str, *, column: str, value,
    ) -> bool:
        """The single non-terminal -> terminal transition (exactly once)."""
        now = _now_iso()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE browser_dispatches SET status = ?, %s = ?, "
                "finished_at = ?, updated_at = ? "
                "WHERE dispatch_id = ? AND status IN "
                "('pending', 'executing')" % column,
                ("done" if column == "result" else "failed", value, now, now,
                 dispatch_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def complete_browser_dispatch(self, dispatch_id: str, result: dict) -> bool:
        """Record the terminal successful result exactly once."""
        try:
            payload = json.dumps(result, sort_keys=True)
        except (ValueError, TypeError):
            payload = json.dumps({"result": str(result)})
        return self._finalize_browser_dispatch(
            dispatch_id, column="result", value=payload)

    def fail_browser_dispatch(self, dispatch_id: str, error: str) -> bool:
        """Record the terminal failure exactly once (fail closed)."""
        return self._finalize_browser_dispatch(
            dispatch_id, column="error", value=str(error or "unknown failure"))

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# TaskWorker — claims tasks from the store and runs them through the EXISTING
# serve.run_task execution path with thin integration callbacks.  It does not
# implement its own execution; it only orchestrates state + persistence.
# ═══════════════════════════════════════════════════════════════════════════

class TaskWorker:
    """Claims queued tasks and executes them via ``serve.run_task``.

    The worker:
      * registers itself and heartbeats on a background thread so crash-recovery
        can tell a live worker from a dead one across restarts/processes;
      * loops: recover stale tasks, claim the next one, dispatch it to a bounded
        execution pool (so different Bots run concurrently while the loop keeps
        claiming) — same-Bot serialisation stays in ``claim_next``;
      * executes via the injected *executor* (defaults to ``serve.run_task``),
        wiring integration callbacks that persist approval state, results,
        failures, and events.
    """

    def __init__(
        self,
        store: CloudTaskStore,
        worker_id: Optional[str] = None,
        executor=None,
        send=None,
        edit=None,
        heartbeat_interval: float = 5.0,
        idle_sleep: float = 0.2,
        max_workers: int = 8,
        shutdown_event: Optional[threading.Event] = None,
    ):
        self.store = store
        self.worker_id = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        # executor signature mirrors serve.run_task (with the lifecycle hooks).
        self._executor = executor
        # Optional notifier callbacks.  When provided, ``send``/``edit`` deliver
        # operator messages (progress, approval prompts, results) to the real
        # transport (e.g. Telegram) instead of only persisting an event.  When
        # ``None`` the worker logs events only and returns a synthetic message
        # id so the in-memory approval protocol still works (used by tests and
        # for transport-agnostic sessions such as the web UI).
        self._send = send
        self._edit = edit
        self.heartbeat_interval = heartbeat_interval
        self.idle_sleep = idle_sleep
        self.max_workers = max_workers
        self._shutdown = shutdown_event or threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._loop_thread: Optional[threading.Thread] = None
        # Bounded pool that runs execute_task *off* the claim loop so different
        # Bots execute concurrently inside this single worker process.  Same-Bot
        # serialization is enforced upstream by CloudTaskStore.claim_next (a
        # session already in running/awaiting_approval is skipped), NOT here;
        # the worker never has two tasks for one Bot in the pool at once.
        self._pool: Optional[ThreadPoolExecutor] = None
        self._pool_stopped = False
        # Tasks whose executor thread is CURRENTLY alive (running, or blocked in
        # an approval wait).  Only these tasks' leases are renewed, so a task
        # whose executor thread died/wedged stops being renewed and can be
        # reclaimed by recovery.  Guarded by its own lock (the heartbeat thread
        # reads it, pool threads mutate it).
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the heartbeat and claim loops in background daemon threads."""
        self.store.register_worker(self.worker_id)
        self._shutdown.clear()
        self._pool_stopped = False
        # Bounded pool: execute_task runs here, off the claim loop, so the loop
        # can keep claiming/dispatching while tasks execute concurrently.
        self._pool = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=f"exec-{self.worker_id}",
        )
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name=f"hb-{self.worker_id}"
        )
        self._loop_thread = threading.Thread(
            target=self._claim_loop, daemon=True, name=f"loop-{self.worker_id}"
        )
        self._hb_thread.start()
        self._loop_thread.start()

    def stop(self) -> None:
        """Signal shutdown and drain gracefully.

        Sets the shutdown flag (the claim loop exits on its next iteration),
        joins the background threads, then shuts the execution pool down with
        ``wait=True`` so in-flight tasks finish instead of being abandoned.
        Idempotent.
        """
        self._shutdown.set()
        if self._hb_thread is not None and self._hb_thread.is_alive():
            self._hb_thread.join(timeout=5)
        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=10)
        if self._pool is not None and not self._pool_stopped:
            # Graceful drain: let running tasks complete before closing.
            self._pool.shutdown(wait=True)
            self._pool_stopped = True

    def is_alive(self) -> bool:
        return bool(self._loop_thread and self._loop_thread.is_alive())

    def _heartbeat_loop(self) -> None:
        while not self._shutdown.is_set():
            # Renew only tasks whose executor thread is genuinely alive. A task
            # whose executor died/wedged is (or will be) absent here, so its
            # lease ages out and recover_stale can reclaim it — the process
            # heartbeat alone no longer pins an abandoned task.
            with self._inflight_lock:
                inflight = list(self._inflight)
            renewal_ok = True
            for task_id in inflight:
                try:
                    self.store.renew_task_lease(task_id, self.worker_id)
                except Exception as exc:
                    renewal_ok = False
                    print(
                        f"[worker {self.worker_id}] lease renewal failed for "
                        f"task {task_id}: {type(exc).__name__}: {exc}",
                        file=__import__("sys").stderr, flush=True,
                    )
            # Only advertise this worker live while its live tasks are actually
            # being renewed. A sustained renewal failure lets this worker look
            # dead so recovery (from another worker) can step in; the worker
            # still refuses to recover its OWN fresh tasks (protect_task_ids).
            if renewal_ok:
                try:
                    self.store.heartbeat_worker(self.worker_id)
                except Exception as exc:
                    print(
                        f"[worker {self.worker_id}] worker heartbeat failed: "
                        f"{type(exc).__name__}: {exc}",
                        file=__import__("sys").stderr, flush=True,
                    )
            self._shutdown.wait(self.heartbeat_interval)

    def _claim_loop(self) -> None:
        # Run recovery repeatedly: task leases, not worker liveness alone,
        # determine whether active work is still owned and making progress.
        next_recovery = 0.0
        while not self._shutdown.is_set():
            try:
                if time.monotonic() >= next_recovery:
                    # Never recover tasks this worker is actively executing,
                    # even if a transient renewal failure aged their lease.
                    with self._inflight_lock:
                        protect = set(self._inflight)
                    self.store.recover_stale(protect_task_ids=protect)
                    next_recovery = time.monotonic() + WORKER_RECOVERY_INTERVAL
                task = self.store.claim_next(self.worker_id)
                if task is not None:
                    print(
                        f"[worker {self.worker_id}] claimed task {task['task_id']} "
                        f"(session={task['session_key']})",
                        flush=True,
                    )
                    # Run the task *off* the claim loop so the loop can keep
                    # claiming and dispatching.  Different-Bot tasks land in
                    # separate pool threads; same-Bot tasks are never both
                    # dispatched because claim_next skips a session that is
                    # already running/awaiting_approval.
                    self._dispatch(task)
                else:
                    self._shutdown.wait(self.idle_sleep)
            except Exception as exc:
                print(f"[worker {self.worker_id}] claim loop error: {exc}",
                      file=__import__("sys").stderr)
                self._shutdown.wait(self.idle_sleep)

    def _dispatch(self, task: dict) -> None:
        """Hand a claimed task to the execution pool (or run it inline).

        The claim loop never blocks on task execution, so it can keep claiming
        and dispatching other (different-Bot) tasks while this one runs.  If the
        pool is unavailable (e.g. ``start()`` was not used), fall back to a
        synchronous inline run so the execution path is unchanged.
        """
        # Register as in-flight BEFORE dispatch: a claimed task may wait in the
        # pool queue (pool saturated) without its executor thread having started
        # yet, and it must still be renewed and protected from its own worker's
        # recovery. The _run_task_safe finally discards this on every path.
        task_id = task["task_id"]
        with self._inflight_lock:
            self._inflight.add(task_id)
        pool = self._pool
        if pool is None:
            self._run_task_safe(task)
            return
        try:
            pool.submit(self._run_task_safe, task)
        except Exception:
            # Submit failed (e.g. pool already shut down): drop the in-flight
            # registration so it cannot leak and pin a phantom lease.
            with self._inflight_lock:
                self._inflight.discard(task_id)
            raise

    def _run_task_safe(self, task: dict) -> None:
        """Execute a task, failing it explicitly if execution raises.

        ``execute_task`` already persists executor failures, but any error in
        finalisation (outside its inner try) is caught here so a task is never
        silently left in ``running`` and a pool thread never dies unnoticed.
        """
        task_id = task["task_id"]
        # Registration happens in _dispatch (before the task is queued, so a
        # pool-queued task is renewed/protected too). Cleanup below drops the
        # in-flight entry on every terminal path.
        print(f"[worker {self.worker_id}] execution started for task {task_id}", flush=True)
        try:
            try:
                self.execute_task(task)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                print(f"[worker {self.worker_id}] execution failed for task {task_id}: {error}",
                      file=__import__("sys").stderr, flush=True)
                try:
                    self.store.fail(task_id, error)
                except Exception:
                    pass
            else:
                status = self.store.status(task_id)
                if status == STATUS_DONE:
                    print(f"[worker {self.worker_id}] execution completed for task {task_id}",
                          flush=True)
                elif status == STATUS_FAILED:
                    print(f"[worker {self.worker_id}] execution failed for task {task_id}",
                          file=__import__("sys").stderr, flush=True)
                else:
                    print(f"[worker {self.worker_id}] execution ended for task {task_id} "
                          f"with status {status}", file=__import__("sys").stderr, flush=True)
        finally:
            with self._inflight_lock:
                self._inflight.discard(task_id)

    # ── Single-task execution ─────────────────────────────────────────────

    def claim_and_execute_once(self, timeout: float = 5.0) -> bool:
        """Claim and execute one task (blocking). Returns True if ran one."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.store.claim_next(self.worker_id)
            if task is not None:
                self.execute_task(task)
                return True
            time.sleep(0.05)
        return False

    def execute_task(self, task: dict) -> None:
        """Execute one claimed task via the injected/ default executor."""
        task_id = task["task_id"]
        session_key = task["session_key"]
        executor = self._executor
        if executor is None:
            import serve
            executor = serve.run_task
        # Register THIS worker's store as the browser-dispatch bridge's store,
        # so a browser task executed here creates a durable request instead of
        # calling this process's own (empty) HostManager. See
        # browser_host_bridge.set_active_store. Imported lazily inside the
        # body so importing task_store never imports the bridge.
        try:
            import browser_host_bridge as _bridge
            _bridge.set_active_store(self.store)
        except Exception:
            pass

        # Per-task execution state captured by the callbacks below.
        state = {
            "result": None,
            "result_captured": False,
            "final_error": None,
            "cancelled_via_approval": False,
        }

        def send_cb(chat_id, text):
            etype = "message"
            if text.startswith("⏳"):
                etype = "start"
            # Capture terminal error/timeout messages so the task can be marked
            # failed when the executor produces no parseable result.
            if text.startswith("⚠️") and "T1:" not in text and "T2:" not in text:
                state["final_error"] = text[:600]
            self.store.add_event(task_id, etype, {
                "text": text, "chat_id": chat_id,
            })
            # Deliver to the real transport when a notifier is wired; otherwise
            # return a synthetic, stable message id for approval keying.
            if self._send is not None:
                return self._send(chat_id, text)
            return f"msg-{uuid.uuid4().hex[:12]}"

        def edit_cb(chat_id, msg_id, text):
            self.store.add_event(task_id, "edit", {
                "text": text, "msg_id": str(msg_id),
            })
            if self._edit is not None:
                self._edit(chat_id, msg_id, text)

        def on_approval_cb(approval_msg_id, tier, token, summary, detail):
            self.store.persist_approval_request(
                task_id, session_key, approval_msg_id, tier, token, summary, detail
            )
            # Cancellation requested before/at approval: deny immediately so
            # the executor stops and the task is marked cancelled.
            if self.store.is_cancel_requested(task_id):
                import serve
                decision_text = "n" if tier == 1 else (token or "")
                serve.handle_approval_reply(
                    chat_id=session_key,
                    reply_text=decision_text,
                    reply_to_id=approval_msg_id,
                    session_key=session_key,
                )
                state["cancelled_via_approval"] = True

        def on_approval_resolved_cb(approval_msg_id, decision):
            self.store.resolve_approval_request(
                task_id, approval_msg_id, decision
            )

        def on_progress_cb(note):
            self.store.touch(task_id)
            self.store.add_event(task_id, "progress", note or {})

        def on_result_cb(result_json):
            state["result"] = result_json
            state["result_captured"] = True
            self.store.add_event(task_id, "result", result_json or {})

        try:
            executor(
                chat_id=task.get("chat_id") or session_key,
                repo_url=task.get("repo_url"),
                task_text=task["task_text"],
                executor_prefix=task.get("executor_prefix") or "repo",
                send=send_cb,
                edit=edit_cb,
                session_key=session_key,
                task_id=task_id,
                on_approval=on_approval_cb,
                on_approval_resolved=on_approval_resolved_cb,
                on_progress=on_progress_cb,
                on_result=on_result_cb,
                # The task row records its binding decision at submit time:
                # web tasks submit with resolution disabled (bot_id stays
                # None), so the executor must not resolve a Bot here either —
                # otherwise a GitHub username equal to a Bot id would bind the
                # web task to that Bot's Rift/policy during execution.
                resolve_bot=task.get("bot_id") is not None,
                # Durable conversation identity: the executor derives the
                # per-conversation engine session directory from it, so a
                # worker retry resumes exactly this conversation's history.
                conversation_id=task.get("conversation_id"),
            )
        except Exception as exc:
            self.store.fail(task_id, f"{type(exc).__name__}: {exc}")
            return

        # Finalise the task lifecycle based on what the executor produced.
        if state["cancelled_via_approval"]:
            self.store.cancel_effective(
                task_id, reason="cancelled at approval (operator request)"
            )
        elif state["result_captured"]:
            self.store.complete(task_id, state["result"])
        elif self.store.is_cancel_requested(task_id):
            # A cancel requested mid-run (no live approval gate) still wins:
            # the operator asked to stop and the executor produced nothing
            # durable we want to keep.
            self.store.cancel_effective(
                task_id, reason="cancelled during run (operator request)"
            )
        else:
            err = state["final_error"] or "no result produced by executor"
            self.store.fail(task_id, err)


# Convenience: a module-level default store for processes that want one.
_default_store: Optional[CloudTaskStore] = None


def default_store(db_path: Optional[str | Path] = None) -> CloudTaskStore:
    """Return a process-wide singleton store (lazily created)."""
    global _default_store
    if _default_store is None:
        _default_store = CloudTaskStore(db_path)
    return _default_store
