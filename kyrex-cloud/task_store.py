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

import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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

# Executor result statuses that count as a *failed* task run (the agent did not
# complete successfully).  Everything else (including "no_changes") is "done".
_FAILED_EXECUTOR_STATUSES = frozenset({
    "agent_failed",
    "git_failed",
    "error",
})

DEFAULT_DB_NAME = "cloud_tasks.db"


class TaskStoreError(Exception):
    """Base class for task store errors."""


class DuplicateTaskId(TaskStoreError):
    """Raised when submitting a task_id that already exists."""


class TaskNotFound(TaskStoreError):
    """Raised when a task_id does not exist."""


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
        self._conn.execute("PRAGMA journal_mode=WAL")
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
                    updated_at      TEXT NOT NULL
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
                    resolved_at TEXT
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
    ) -> str:
        """Create a new queued task and return its stable task_id.

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
        # identity chain (bot_id -> session_key -> rift) durably.
        if bot_id is None or rift is None:
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
                    cancel_requested, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    task_id, session_key, bot_id, bot_prefix, rift, chat_id,
                    executor_prefix, repo_url, task_text, STATUS_QUEUED,
                    now, now,
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
        """Refresh ``heartbeat_at`` so crash-recovery does not misfire."""
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET heartbeat_at = ?, updated_at = ? "
                "WHERE task_id = ?",
                (now, now, task_id),
            )
            self._conn.commit()

    def complete(self, task_id: str, result: dict) -> None:
        """Persist a successful result and move the task to ``done``/``failed``."""
        import json
        result = result or {}
        status = STATUS_DONE
        if result.get("status") in _FAILED_EXECUTOR_STATUSES:
            status = STATUS_FAILED
        self.set_status(
            task_id, status,
            result=json.dumps(result, sort_keys=True),
        )

    def fail(self, task_id: str, error: str) -> None:
        """Persist a failure and move the task to ``failed``."""
        self.set_status(task_id, STATUS_FAILED, error=error or "unknown failure")

    def cancel_effective(self, task_id: str, reason: str = "cancelled") -> None:
        """Mark a task cancelled (used after a cancellation/rejection)."""
        self.set_status(task_id, STATUS_CANCELLED, error=reason)

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

    def resolve_approval_request(
        self, task_id: str, message_id: str, decision: str
    ) -> None:
        """Mark an approval_request decided and return to running."""
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE approval_requests SET decision = ?, resolved_at = ? "
                "WHERE task_id = ? AND message_id = ? AND decision = 'pending'",
                (decision, now, task_id, str(message_id)),
            )
            self._conn.commit()
        if self.status(task_id) == STATUS_AWAITING_APPROVAL:
            self.set_status(task_id, STATUS_RUNNING)
        self.add_event(task_id, "approval_resolved", {"decision": decision})

    def _row_to_approval(self, row) -> dict:
        cols = [
            "approval_id", "task_id", "session_key", "chat_id", "message_id",
            "tier", "token", "summary", "detail", "decision",
            "created_at", "resolved_at",
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
        cutoff = time.time() - heartbeat_timeout
        with self._lock:
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
    ) -> list[dict]:
        """Recover tasks orphaned by dead workers (restart-safe).

        A task left in ``running`` or ``awaiting_approval`` whose worker is no
        longer alive is recovered:
          * ``running``     -> ``failed``  (crash / interrupted)
          * ``awaiting_approval`` -> ``cancelled`` (interrupted approval)

        If *live_worker_ids* is ``None`` it is derived from the workers table
        using *heartbeat_timeout*.  Returns the list of recovered task dicts.
        """
        if live_worker_ids is None:
            live_worker_ids = set(self.live_workers(heartbeat_timeout))
        live_worker_ids = set(live_worker_ids or set())

        recovered: list[dict] = []
        with self._lock:
            if live_worker_ids:
                claim_clause = "AND claimed_by NOT IN (%s)" % (
                    ",".join("?" * len(live_worker_ids))
                )
                params = list(live_worker_ids)
            else:
                # No live workers at all: every non-terminal task is an
                # orphan and must be recovered rather than left invisible.
                claim_clause = ""
                params = []
            rows = self._conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE status IN (?, ?) {claim_clause}
                """,
                [STATUS_RUNNING, STATUS_AWAITING_APPROVAL] + params,
            ).fetchall()
            tasks = [self._row_to_task(r) for r in rows]

        for task in tasks:
            if task["status"] == STATUS_AWAITING_APPROVAL:
                self.cancel_effective(
                    task["task_id"],
                    reason="recovered after restart: approval interrupted",
                )
                # Mark any pending approval_request as timed out.
                with self._lock:
                    self._conn.execute(
                        "UPDATE approval_requests SET decision = 'timeout', "
                        "resolved_at = ? WHERE task_id = ? AND decision = 'pending'",
                        (_now_iso(), task["task_id"]),
                    )
                    self._conn.commit()
                recovered.append(self.get(task["task_id"]))
            else:
                self.fail(
                    task["task_id"],
                    "recovered after restart: worker not active",
                )
                recovered.append(self.get(task["task_id"]))
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
        # handle_approval_reply(chat_id, reply_text, reply_to_id, session_key)
        return serve.handle_approval_reply(
            chat_id=session_key,
            reply_text=text,
            reply_to_id=message_id,
            session_key=session_key,
        )

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
        idle_sleep: float = 1.0,
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
            try:
                self.store.heartbeat_worker(self.worker_id)
            except Exception:
                pass
            self._shutdown.wait(self.heartbeat_interval)

    def _claim_loop(self) -> None:
        # Recover orphaned tasks from any previously-dead worker first.
        try:
            self.store.recover_stale()
        except Exception as exc:
            print(f"[worker {self.worker_id}] recovery error: {exc}",
                  file=__import__("sys").stderr)
        while not self._shutdown.is_set():
            try:
                task = self.store.claim_next(self.worker_id)
                if task is not None:
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
        pool = self._pool
        if pool is None:
            self._run_task_safe(task)
            return
        pool.submit(self._run_task_safe, task)

    def _run_task_safe(self, task: dict) -> None:
        """Execute a task, failing it explicitly if execution raises.

        ``execute_task`` already persists executor failures, but any error in
        finalisation (outside its inner try) is caught here so a task is never
        silently left in ``running`` and a pool thread never dies unnoticed.
        """
        try:
            self.execute_task(task)
        except Exception as exc:
            try:
                self.store.fail(task["task_id"], f"{type(exc).__name__}: {exc}")
            except Exception:
                pass

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
