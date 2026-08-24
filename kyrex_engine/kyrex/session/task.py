# Phase 1a — Task and TaskStore.
#
# This module defines the core data model for race-mode tasks. It is deliberately
# stateless with respect to execution: no persistence, no scheduler, no runner.
# See KX_SERVE_DESIGN.md for the broader architecture.
#
# == Design decisions ==
#
#   - Task is a frozen dataclass so callers cannot accidentally mutate fields
#     after construction. The ``metadata`` dict is additionally wrapped in
#     ``types.MappingProxyType`` in ``__post_init__`` so that callers cannot
#     mutate a stored Task's metadata behind the store's back.
#     All mutations go through TaskStore.update(), which returns a new instance.
#
#   - Timestamp fields (created_at, updated_at) use explicit None-vs-0.0-vs-real
#     semantics. Passing 0.0 means "I explicitly want zero" and is preserved;
#     passing None triggers time.time().
#
#   - TaskStore uses reference-based return semantics: .update() returns the
#     *internal* stored reference, not a copy. Callers that mutate the returned
#     object will corrupt the store. See TaskStore.update() docstring.
#

import time
import types
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(Enum):
    """Lifecycle states for a single coding task.

    These are the *only* valid values for Task.status.  Any string passed to
    from_dict() that does not match one of these (case-sensitive) raises
    ValueError — silently defaulting to an invalid status would hide bugs.

    ┌──────────────┬────────────────────────────────────────────────────┐
    │ Value        │ Meaning                                            │
    ├──────────────┼────────────────────────────────────────────────────┤
    │ pending      │ Created but not yet dispatched to any model lane   │
    │ running      │ Dispatched; at least one lane is executing         │
    │ done         │ At least one lane completed with a valid result    │
    │ failed       │ All lanes terminated without a valid result        │
    │ killed       │ Explicitly cancelled (user interrupt or timeout)   │
    └──────────────┴────────────────────────────────────────────────────┘
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    KILLED = "killed"


_VALID_STATUSES = frozenset(s.value for s in TaskStatus)


def _coerce_timestamp(value: float | None) -> float:
    """Return *value* when it is not None, else time.time().

    The critical invariant: an explicit ``0.0`` is returned as-is and is *not*
    replaced by ``time.time()``.  Using ``value or time.time()`` would lose
    the explicit zero because ``0.0`` is falsy in Python.
    """
    return time.time() if value is None else value


@dataclass(frozen=True)
class Task:
    """An immutable coding task with validated status and timestamp handling.

    Parameters
    ----------
    id : str
        Opaque, caller-assigned identifier.  Must be non-empty.
    description : str
        Free-form task description (the prompt sent to the model).
    status : TaskStatus
        Validated lifecycle state.  Any value not in TaskStatus raises
        ValueError at construction time.
    created_at : float | None
        Unix timestamp.  ``None`` → ``time.time()`` at construction.
        ``0.0`` → preserved verbatim.
    updated_at : float | None
        Same semantics as *created_at*.
    metadata : dict
        Arbitrary key-value bag for extension (model name, lane count, …).
        Internally stored as ``types.MappingProxyType`` (read-only view);
        direct mutation after construction raises ``TypeError``.
    """

    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=lambda: time.time())
    updated_at: float = field(default_factory=lambda: time.time())
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        # Frozen=True means we cannot set attributes in __post_init__ via
        # normal assignment.  Use object.__setattr__ instead.
        if not self.id:
            raise ValueError("Task.id must be non-empty")

        # Validate description (must be non-empty, consistent with from_dict).
        if not self.description:
            raise ValueError("Task.description must be non-empty")

        # Validate status
        if not isinstance(self.status, TaskStatus):
            raise ValueError(
                f"Invalid Task.status: {self.status!r}. "
                f"Must be one of {_VALID_STATUSES}"
            )

        # Freeze metadata so callers cannot mutate a stored Task behind the
        # store's back.  MappingProxyType is a read-only view; any attempt to
        # assign t.metadata["key"] = val raises TypeError.
        # Guard against double-wrapping when dataclasses.replace() passes an
        # existing MappingProxyType from a previous __post_init__ call.
        if not isinstance(self.metadata, types.MappingProxyType):
            object.__setattr__(
                self,
                "metadata",
                types.MappingProxyType(self.metadata if self.metadata else {}),
            )

        # Coerce timestamps so None becomes time.time() but 0.0 stays 0.0.
        object.__setattr__(self, "created_at", _coerce_timestamp(self.created_at))
        object.__setattr__(self, "updated_at", _coerce_timestamp(self.updated_at))

    # ── from_dict ──────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        """Construct a Task from a plain dict with strict validation.

        *Status string enforcement*
            ``data["status"]`` (when present) must be one of the string values
            defined by ``TaskStatus`` — case-sensitive.  Unknown strings raise
            ``ValueError`` so that bugs are caught early.

        *Timestamp preservation*
            ``data["created_at"]`` / ``data["updated_at"]`` with value ``0.0``
            are preserved verbatim.  An absent key (or ``None`` value) is
            replaced by ``time.time()`` at construction time.  The check is
            ``key in data``, not truthiness, so ``0.0`` survives.

        *Missing fields*
            ``id`` and ``description`` are required.  Raises ``ValueError``
            when absent.
        """
        # --- required fields ---
        task_id = data.get("id")
        if not task_id:
            raise ValueError("from_dict: missing required field 'id'")
        description = data.get("description")
        if not description:
            if "description" in data:
                raise ValueError("from_dict: 'description' must be non-empty")
            raise ValueError("from_dict: missing required field 'description'")

        # --- status (optional, validated) ---
        raw_status = data.get("status", TaskStatus.PENDING.value)
        if isinstance(raw_status, str):
            if raw_status not in _VALID_STATUSES:
                raise ValueError(
                    f"from_dict: invalid status {raw_status!r}. "
                    f"Must be one of {sorted(_VALID_STATUSES)}"
                )
            status = TaskStatus(raw_status)
        elif isinstance(raw_status, TaskStatus):
            status = raw_status
        elif raw_status is None:
            status = TaskStatus.PENDING
        else:
            raise ValueError(
                f"from_dict: status must be a string or TaskStatus, "
                f"got {type(raw_status).__name__}"
            )

        # --- timestamps (preserve explicit 0.0) ---
        created_at = data["created_at"] if "created_at" in data else None
        updated_at = data["updated_at"] if "updated_at" in data else None

        # --- metadata ---
        metadata = data.get("metadata", {})

        return cls(
            id=task_id,
            description=description,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            metadata=metadata,
        )

    # ── Convenience accessors ──────────────────────────────────────────

    @property
    def is_terminal(self) -> bool:
        """True when the task is in an end state (done, failed, or killed)."""
        return self.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.KILLED)


# ═══════════════════════════════════════════════════════════════════════
# TaskStore
# ═══════════════════════════════════════════════════════════════════════

_ENTRY_NOT_FOUND = object()  # sentinel for get()


class TaskStore:
    """An in-memory, dict-backed store of Task objects.

    This store is *not* thread-safe.  It does not persist to disk
    (see KX_SERVE_DESIGN.md for the storage layer).

    **Mutability contract**
        - ``.update()`` returns the **internal stored reference** — it does
          **not** return a copy.  This is intentional: the caller that updates
          a task receives the same object that lives inside the store, so any
          subsequent field access on the return value is guaranteed to reflect
          the store's current state without an extra lookup.

          However, because ``Task`` is a frozen dataclass, the risk of
          accidental mutation is low.  The reference-sharing is an
          optimisation — it avoids a linear scan or a hash-map copy on every
          write — and callers *should not* depend on being able to mutate the
          returned object (they cannot, since it is frozen).

        - ``.get()`` also returns the internal reference, not a copy.

    In a future phase a persistent store may replace this class; the
    ``update()`` signature will remain the same but the return value may
    become a detached copy.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    # ── Reading ────────────────────────────────────────────────────────

    def get(self, task_id: str) -> Task | None:
        """Return the task with *task_id*, or ``None`` if absent.

        Returns the internal reference (not a copy).  See the mutability
        contract above.
        """
        return self._tasks.get(task_id)

    def all(self) -> list[Task]:
        """Return a snapshot list of every task currently in the store.

        The list itself is a copy (new allocation), but each element is an
        internal reference (not a deep copy).  Because the underlying dict
        is not locked, concurrent ``.put()`` / ``.update()`` / ``.delete()``
        during iteration will raise ``RuntimeError: dictionary changed size
        during iteration``.  This store is *not* thread-safe; callers in
        multi-threaded contexts must provide their own lock.
        """
        return list(self._tasks.values())

    def count(self) -> int:
        """Return the number of tasks in the store."""
        return len(self._tasks)

    def filter(self, *, status: TaskStatus | None = None) -> list[Task]:
        """Return tasks matching *status* (or all tasks when *status* is None).

        Returns a new list; elements are internal references.
        """
        if status is None:
            return self.all()
        return [t for t in self._tasks.values() if t.status == status]

    # ── Writing ────────────────────────────────────────────────────────

    def put(self, task: Task) -> Task:
        """Insert or overwrite *task* by its id.

        No validation beyond what ``Task`` already enforces at construction.
        Returns the task that was passed in (for chaining / fluency).
        """
        self._tasks[task.id] = task
        return task

    def update(
        self, task_id: str, **changes: Any
    ) -> Task:
        """Return the store-internal task updated with *changes*.

        This is the **only** sanctioned way to modify a stored task.
        Because ``Task`` is frozen, the store creates a new ``Task`` instance
        via ``dataclasses.replace()`` and replaces the internal reference.

        Parameters
        ----------
        task_id : str
            Identifier of the task to update.
        **changes : Any
            Field-value pairs to apply.  Passing ``status`` as a plain string
            is allowed and will be coerced via ``TaskStatus(value)`` — but the
            string must match exactly one of the ``TaskStatus`` enum values.

        Returns
        -------
        Task
            The **new** internal reference (replaces the old one in the store).

        Raises
        ------
        KeyError
            When *task_id* is not found.
        ValueError
            When a change field produces an invalid ``Task`` (e.g., bad status).

        Notes
        -----
        **Mutability semantics**: the returned ``Task`` is the store's
        internal reference.  In the current implementation the returned object
        *is* the object stored in ``_tasks[task_id]``.  Callers who hold a
        reference obtained *before* ``update()`` will see stale data (the old
        frozen instance is still reachable but no longer in the store).

        This is a deliberate choice for Phase 1a: it keeps the implementation
        simple and matches the expectation of single-threaded usage where no
        two callers race on the same task ID.
        """
        import dataclasses

        existing = self._tasks.get(task_id)
        if existing is None:
            raise KeyError(f"TaskStore.update: task {task_id!r} not found")

        # Coerce status string to TaskStatus enum before passing to replace().
        if "status" in changes:
            raw = changes["status"]
            if isinstance(raw, str):
                if raw not in _VALID_STATUSES:
                    raise ValueError(
                        f"TaskStore.update: invalid status {raw!r}. "
                        f"Must be one of {sorted(_VALID_STATUSES)}"
                    )
                changes["status"] = TaskStatus(raw)
            elif isinstance(raw, TaskStatus):
                pass  # already valid
            else:
                raise ValueError(
                    f"TaskStore.update: status must be a string or TaskStatus, "
                    f"got {type(raw).__name__}"
                )

        updated = dataclasses.replace(existing, **changes)
        # Overwrite updated_at when the caller did not explicitly set it.
        if "updated_at" not in changes:
            object.__setattr__(updated, "updated_at", time.time())

        self._tasks[task_id] = updated
        return updated

    # ── Removal ────────────────────────────────────────────────────────

    def delete(self, task_id: str) -> bool:
        """Remove *task_id* from the store.  Returns ``True`` if it existed."""
        if task_id in self._tasks:
            del self._tasks[task_id]
            return True
        return False

    # ── Bulk ───────────────────────────────────────────────────────────

    def put_all(self, tasks: list[Task]) -> None:
        """Insert or overwrite every task in *tasks*.  Invalid tasks raise."""
        for t in tasks:
            self._tasks[t.id] = t

    def clear(self) -> None:
        """Remove every task from the store."""
        self._tasks.clear()


__all__ = [
    "Task",
    "TaskStatus",
    "TaskStore",
    "_VALID_STATUSES",
    "_coerce_timestamp",
]