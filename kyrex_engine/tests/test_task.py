# Phase 1a — Task / TaskStore invariants.
#
# These tests verify the contract guarantees described in task.py:
#
#   - Explicit 0.0 timestamps are never replaced by time.time()
#   - from_dict() raises ValueError for invalid status strings
#   - TaskStore.update() returns the new internal reference (not a copy)
#   - The frozen dataclass prevents accidental field mutation
#

import time
import dataclasses

import pytest

from kyrex.session.task import (
    Task,
    TaskStatus,
    TaskStore,
    _VALID_STATUSES,
    _coerce_timestamp,
)


# ======================================================================
# _coerce_timestamp
# ======================================================================

class TestCoerceTimestamp:
    """Unit coverage for the helper that underpins all timestamp logic."""

    def test_preserves_explicit_zero(self):
        # 0.0 is falsy in Python — this is the core bug we are fixing.
        assert _coerce_timestamp(0.0) == 0.0

    def test_negative_values_preserved(self):
        assert _coerce_timestamp(-1.0) == -1.0

    def test_none_becomes_time(self):
        before = time.time()
        result = _coerce_timestamp(None)
        after = time.time()
        assert before <= result <= after

    def test_positive_value_preserved(self):
        val = 1_234_567.89
        assert _coerce_timestamp(val) == val


# ======================================================================
# Task construction — timestamp preservation
# ======================================================================

class TestTaskTimestampPreservation:
    """The bug: `value or time.time()` destroys explicit 0.0."""

    def test_default_no_args(self):
        t = Task(id="t1", description="test")
        now = time.time()
        assert t.created_at <= now + 0.01
        assert t.updated_at <= now + 0.01

    def test_explicit_zero_preserved_in_constructor(self):
        t = Task(id="t1", description="test", created_at=0.0, updated_at=0.0)
        assert t.created_at == 0.0
        assert t.updated_at == 0.0

    def test_explicit_zero_created_at_only(self):
        t = Task(id="t1", description="test", created_at=0.0)
        assert t.created_at == 0.0
        # updated_at should be set to time.time()
        assert t.updated_at > 0.0

    def test_explicit_zero_updated_at_only(self):
        t = Task(id="t1", description="test", updated_at=0.0)
        assert t.updated_at == 0.0
        assert t.created_at > 0.0

    def test_explicit_none_uses_time(self):
        t = Task(id="t1", description="test", created_at=None, updated_at=None)
        now = time.time()
        assert t.created_at > 0.0
        assert t.created_at <= now + 0.01
        assert t.updated_at > 0.0
        assert t.updated_at <= now + 0.01

    def test_from_dict_omitted_timestamp_uses_time(self):
        t = Task.from_dict({"id": "t1", "description": "test"})
        now = time.time()
        assert t.created_at > 0.0
        assert t.created_at <= now + 0.01
        assert t.updated_at > 0.0
        assert t.updated_at <= now + 0.01

    def test_from_dict_explicit_zero_created_at(self):
        t = Task.from_dict({"id": "t1", "description": "test", "created_at": 0.0})
        assert t.created_at == 0.0

    def test_from_dict_explicit_zero_updated_at(self):
        t = Task.from_dict({"id": "t1", "description": "test", "updated_at": 0.0})
        assert t.updated_at == 0.0

    def test_from_dict_both_timestamps_zero_preserved(self):
        t = Task.from_dict({
            "id": "t1",
            "description": "test",
            "created_at": 0.0,
            "updated_at": 0.0,
        })
        assert t.created_at == 0.0
        assert t.updated_at == 0.0

    def test_from_dict_explicit_none_triggers_time(self):
        t = Task.from_dict({
            "id": "t1",
            "description": "test",
            "created_at": None,
            "updated_at": None,
        })
        now = time.time()
        assert t.created_at > 0.0
        assert t.created_at <= now + 0.01
        assert t.updated_at > 0.0
        assert t.updated_at <= now + 0.01

    def test_from_dict_0_0_not_overwritten_by_explicit_none(self):
        """Both keys present: explicit 0.0 beats None."""
        t = Task.from_dict({
            "id": "t1",
            "description": "test",
            "created_at": 0.0,
            "updated_at": 0.0,
        })
        assert t.created_at == 0.0
        assert t.updated_at == 0.0


# ======================================================================
# Task construction — status validation
# ======================================================================

class TestTaskStatusValidation:
    """from_dict() must not silently create Tasks with invalid statuses."""

    def test_default_status_is_pending(self):
        t = Task(id="t1", description="test")
        assert t.status == TaskStatus.PENDING

    def test_from_dict_default_status_is_pending(self):
        t = Task.from_dict({"id": "t1", "description": "test"})
        assert t.status == TaskStatus.PENDING

    @pytest.mark.parametrize("valid", ["pending", "running", "done", "failed", "killed"])
    def test_valid_status_strings(self, valid):
        t = Task.from_dict({"id": "t1", "description": "test", "status": valid})
        assert t.status.value == valid

    @pytest.mark.parametrize("invalid", [
        "", "in_progress", "completed", "error", "cancelled",
        "PENDING", "Running", "DONE", "  pending  ",
        "unknown", "terminated", "aborted",
    ])
    def test_invalid_status_string_raises(self, invalid):
        with pytest.raises(ValueError, match="invalid status"):
            Task.from_dict({"id": "t1", "description": "test", "status": invalid})

    def test_invalid_status_type_raises(self):
        with pytest.raises(ValueError, match="status must be a string"):
            Task.from_dict({"id": "t1", "description": "test", "status": 42})

    def test_constructor_accepts_taskstatus_enum(self):
        t = Task(id="t1", description="test", status=TaskStatus.RUNNING)
        assert t.status == TaskStatus.RUNNING

    def test_constructor_rejects_bad_string(self):
        with pytest.raises(ValueError, match="Invalid Task.status"):
            Task(id="t1", description="test", status="garbage")

    def test_constructor_rejects_bad_type(self):
        with pytest.raises(ValueError, match="Invalid Task.status"):
            Task(id="t1", description="test", status=99)

    def test_is_terminal(self):
        assert Task(id="t1", description="x", status=TaskStatus.DONE).is_terminal is True
        assert Task(id="t1", description="x", status=TaskStatus.FAILED).is_terminal is True
        assert Task(id="t1", description="x", status=TaskStatus.KILLED).is_terminal is True
        assert Task(id="t1", description="x", status=TaskStatus.PENDING).is_terminal is False
        assert Task(id="t1", description="x", status=TaskStatus.RUNNING).is_terminal is False

    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            Task(id="", description="test")

    def test_from_dict_missing_id_raises(self):
        with pytest.raises(ValueError, match="missing required field.*id"):
            Task.from_dict({"description": "test"})

    def test_from_dict_missing_description_raises(self):
        with pytest.raises(ValueError, match="missing required field.*description"):
            Task.from_dict({"id": "t1"})

    def test_constructor_rejects_empty_description(self):
        """Consistency with from_dict: empty description must be rejected."""
        with pytest.raises(ValueError, match="description must be non-empty"):
            Task(id="t1", description="")

    def test_from_dict_rejects_empty_description(self):
        with pytest.raises(ValueError, match="description.*must be non-empty"):
            Task.from_dict({"id": "t1", "description": ""})


# ======================================================================
# Task immutability (frozen dataclass)
# ======================================================================

class TestTaskImmutability:
    """Task instances must be frozen to prevent accidental corruption."""

    def test_cannot_set_field_directly(self):
        t = Task(id="t1", description="test")
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.status = TaskStatus.DONE

    def test_cannot_reassign_field_directly(self):
        """FrozenInstanceError on field reassignment (not dict mutation)."""
        t = Task(id="t1", description="test", metadata={"key": "val"})
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.metadata = {"new_key": "new_value"}

    def test_metadata_is_immutable_after_construction(self):
        """MappingProxyType prevents mutation behind the store's back."""
        t = Task(id="t1", description="test", metadata={"key": "val"})
        with pytest.raises(TypeError):
            t.metadata["new_key"] = "new_value"  # mappingproxy is read-only
        # Callers must use TaskStore.update() to change metadata.

    def test_metadata_defensive_copy(self):
        """External mutation of the original dict must not reach Task."""
        d = {"key": "original"}
        t = Task(id="t1", description="test", metadata=d)
        d["key"] = "hacked"  # mutate the caller's original dict
        assert t.metadata["key"] == "original"  # Task's copy must be unchanged

    def test_from_dict_metadata_defensive_copy(self):
        """from_dict must also copy the metadata dict, not just wrap it."""
        d = {"key": "original"}
        t = Task.from_dict({"id": "t1", "description": "test", "metadata": d})
        d["key"] = "hacked"
        assert t.metadata["key"] == "original"

    def test_replace_creates_new_instance(self):
        t1 = Task(id="t1", description="test")
        t2 = dataclasses.replace(t1, status=TaskStatus.DONE)
        assert t2 is not t1
        assert t2.status == TaskStatus.DONE
        assert t1.status == TaskStatus.PENDING  # original unchanged


# ======================================================================
# TaskStore
# ======================================================================

class TestTaskStoreBasics:
    """Basic put / get / delete / count / clear."""

    def test_put_and_get(self):
        store = TaskStore()
        t = Task(id="t1", description="test")
        store.put(t)
        assert store.get("t1") is t  # reference-based

    def test_get_missing_returns_none(self):
        store = TaskStore()
        assert store.get("nonexistent") is None

    def test_put_overwrites(self):
        store = TaskStore()
        t1 = Task(id="t1", description="first")
        t2 = Task(id="t1", description="second")
        store.put(t1)
        store.put(t2)
        assert store.get("t1") is t2

    def test_delete_existing(self):
        store = TaskStore()
        store.put(Task(id="t1", description="test"))
        assert store.delete("t1") is True
        assert store.get("t1") is None

    def test_delete_missing(self):
        store = TaskStore()
        assert store.delete("nonexistent") is False

    def test_count(self):
        store = TaskStore()
        assert store.count() == 0
        store.put(Task(id="t1", description="a"))
        store.put(Task(id="t2", description="b"))
        assert store.count() == 2

    def test_all_returns_snapshot(self):
        store = TaskStore()
        store.put(Task(id="t1", description="a"))
        store.put(Task(id="t2", description="b"))
        snapshot = store.all()
        assert len(snapshot) == 2
        store.put(Task(id="t3", description="c"))
        assert len(snapshot) == 2  # not affected by subsequent puts

    def test_clear(self):
        store = TaskStore()
        store.put(Task(id="t1", description="test"))
        store.clear()
        assert store.count() == 0

    def test_put_all(self):
        store = TaskStore()
        tasks = [Task(id="t1", description="a"), Task(id="t2", description="b")]
        store.put_all(tasks)
        assert store.count() == 2


# ======================================================================
# TaskStore.update() — contract & mutability semantics
# ======================================================================

class TestTaskStoreUpdate:
    """TaskStore.update() returns the *new* internal reference.

    This is intentional: the caller receives an object that is guaranteed to
    be the store's current value for that task ID.  A regression test below
    verifies that a stale reference (obtained before an update) is no longer
    the store's canonical object.
    """

    def test_update_returns_different_reference(self):
        store = TaskStore()
        t = Task(id="t1", description="original")
        store.put(t)
        updated = store.update("t1", description="modified")
        assert updated is not t          # new object
        assert updated.description == "modified"
        assert t.description == "original"  # old frozen instance unchanged

    def test_update_store_contains_new_reference(self):
        store = TaskStore()
        t = Task(id="t1", description="original")
        store.put(t)
        updated = store.update("t1", description="modified")
        assert store.get("t1") is updated   # store holds the new one
        assert store.get("t1") is not t     # old one is gone from store

    def test_update_missing_raises_keyerror(self):
        store = TaskStore()
        with pytest.raises(KeyError, match="not found"):
            store.update("nonexistent", description="x")

    def test_update_status_string_coerces(self):
        store = TaskStore()
        store.put(Task(id="t1", description="test"))
        updated = store.update("t1", status="done")
        assert updated.status == TaskStatus.DONE

    def test_update_invalid_status_raises(self):
        store = TaskStore()
        store.put(Task(id="t1", description="test"))
        with pytest.raises(ValueError, match="invalid status"):
            store.update("t1", status="garbage")

    def test_update_bad_status_type_raises(self):
        store = TaskStore()
        store.put(Task(id="t1", description="test"))
        with pytest.raises(ValueError, match="status must be a string"):
            store.update("t1", status=99)

    def test_update_updated_at_auto_refreshed(self):
        store = TaskStore()
        store.put(Task(id="t1", description="test", updated_at=0.0))
        updated = store.update("t1", description="modified")
        # updated_at should be set to current time (not 0.0)
        assert updated.updated_at > 0.0

    def test_update_updated_at_explicitly_set(self):
        store = TaskStore()
        store.put(Task(id="t1", description="test"))
        target = 12345.0
        updated = store.update("t1", description="modified", updated_at=target)
        assert updated.updated_at == target

    def test_update_updated_at_zero_explicitly(self):
        """Caller can explicitly set updated_at=0.0 via update()."""
        store = TaskStore()
        store.put(Task(id="t1", description="test"))
        updated = store.update("t1", description="modified", updated_at=0.0)
        assert updated.updated_at == 0.0

    def test_stale_reference_stale_after_update(self):
        """Regression: pre-update references must not be the store's value."""
        store = TaskStore()
        t = store.put(Task(id="t1", description="original"))
        _ = store.update("t1", description="modified")
        # t was the original stored object; it is no longer in the store.
        assert store.get("t1") is not t
        assert store.get("t1").description == "modified"

    def test_filter_returns_matching(self):
        store = TaskStore()
        t1 = Task(id="t1", description="a", status=TaskStatus.DONE)
        t2 = Task(id="t2", description="b", status=TaskStatus.PENDING)
        store.put(t1)
        store.put(t2)
        done = store.filter(status=TaskStatus.DONE)
        assert len(done) == 1
        assert done[0] is t1

    def test_filter_none_returns_all(self):
        store = TaskStore()
        store.put(Task(id="t1", description="a"))
        store.put(Task(id="t2", description="b"))
        assert len(store.filter()) == 2


# ======================================================================
# _VALID_STATUSES consistency
# ======================================================================

class TestValidStatuses:
    """The frozen set must stay in sync with the enum."""

    def test_covers_all_enum_values(self):
        enum_values = {s.value for s in TaskStatus}
        assert _VALID_STATUSES == enum_values

    def test_no_extra_values(self):
        assert len(_VALID_STATUSES) == 5  # pending, running, done, failed, killed