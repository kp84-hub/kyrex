import time
import pytest

from kyrex.task import Task, TaskStore


class TestTask:
    """Test Task model creation and serialization."""

    def test_creates_with_description_only(self):
        """Should create a Task with just a description (auto-generated id/status/timestamps)."""
        task = Task("Do something")
        assert task.description == "Do something"
        assert task.status == "pending"
        assert len(task.id) == 12  # uuid4 hex[:12]
        assert task.created_at > 0
        assert task.updated_at == task.created_at

    def test_creates_with_explicit_id(self):
        """Should accept an explicit task_id."""
        task = Task("Explicit ID task", task_id="my-custom-id")
        assert task.id == "my-custom-id"

    def test_creates_with_explicit_status(self):
        """Should accept an explicit status."""
        task = Task("Failed task", status="failed")
        assert task.status == "failed"

    def test_creates_with_explicit_timestamps(self):
        """Should accept explicit created_at/updated_at."""
        now = 1234567890.0
        task = Task("Timestamped", created_at=now, updated_at=now + 10)
        assert task.created_at == now
        assert task.updated_at == now + 10

    def test_id_uniqueness(self):
        """Each auto-generated id should be unique."""
        t1 = Task("First")
        t2 = Task("Second")
        assert t1.id != t2.id

    def test_to_dict_roundtrip(self):
        """to_dict() should produce serializable dict and from_dict() should recover it."""
        original = Task("Roundtrip test", task_id="rt-001", status="running")
        data = original.to_dict()
        assert data["id"] == "rt-001"
        assert data["description"] == "Roundtrip test"
        assert data["status"] == "running"
        assert "created_at" in data
        assert "updated_at" in data

        recovered = Task.from_dict(data)
        assert recovered.id == original.id
        assert recovered.description == original.description
        assert recovered.status == original.status
        assert recovered.created_at == original.created_at
        assert recovered.updated_at == original.updated_at

    def test_equality_by_id(self):
        """Tasks with the same id should be equal."""
        t1 = Task("Same", task_id="abc")
        t2 = Task("Same", task_id="abc")
        assert t1 == t2

    def test_inequality_by_id(self):
        """Tasks with different ids should not be equal."""
        t1 = Task("A")
        t2 = Task("B")
        assert t1 != t2

    def test_equality_with_non_task(self):
        """Comparing Task to non-Task should return NotImplemented."""
        task = Task("Test")
        assert task.__eq__("not a task") is NotImplemented

    def test_repr(self):
        """__repr__ should include id, description, and status."""
        task = Task("My task", task_id="r01")
        r = repr(task)
        assert "r01" in r
        assert "My task" in r
        assert "pending" in r


class TestTaskStore:
    """Test TaskStore CRUD operations."""

    @pytest.fixture
    def store(self):
        """Fresh TaskStore for each test."""
        return TaskStore()

    @pytest.fixture
    def sample_task(self):
        """A reusable sample task."""
        return Task("Sample task", task_id="s1")

    def test_create_and_get(self, store, sample_task):
        """Should store a task and retrieve it by id."""
        store.create(sample_task)
        retrieved = store.get("s1")
        assert retrieved is not None
        assert retrieved.id == "s1"
        assert retrieved.description == "Sample task"

    def test_create_duplicate_raises(self, store, sample_task):
        """Creating a task with an existing id should raise KeyError."""
        store.create(sample_task)
        duplicate = Task("Duplicate", task_id="s1")
        with pytest.raises(KeyError, match="already exists"):
            store.create(duplicate)

    def test_get_nonexistent(self, store):
        """Getting a nonexistent task id should return None."""
        assert store.get("nonexistent") is None

    def test_list_empty(self, store):
        """An empty store should list zero tasks."""
        assert store.list() == []

    def test_list_with_tasks(self, store):
        """Should list all stored tasks."""
        store.create(Task("A", task_id="a1"))
        store.create(Task("B", task_id="b2"))
        tasks = store.list()
        assert len(tasks) == 2
        ids = {t.id for t in tasks}
        assert ids == {"a1", "b2"}

    def test_update(self, store, sample_task):
        """Should update a task and bump its updated_at."""
        store.create(sample_task)
        before = sample_task.updated_at
        time.sleep(0.01)  # ensure clock advances
        sample_task.status = "completed"
        store.update(sample_task)
        retrieved = store.get("s1")
        assert retrieved.status == "completed"
        assert retrieved.updated_at > before

    def test_update_nonexistent_raises(self, store):
        """Updating a task not in the store should raise KeyError."""
        task = Task("Orphan", task_id="orphan")
        with pytest.raises(KeyError, match="not found"):
            store.update(task)

    def test_delete_existing(self, store, sample_task):
        """Should delete an existing task and return True."""
        store.create(sample_task)
        assert store.delete("s1") is True
        assert store.get("s1") is None

    def test_delete_nonexistent(self, store):
        """Deleting a nonexistent task should return False."""
        assert store.delete("nonexistent") is False

    def test_clear(self, store):
        """Should remove all tasks."""
        store.create(Task("A", task_id="a1"))
        store.create(Task("B", task_id="b2"))
        store.clear()
        assert len(store) == 0
        assert store.list() == []

    def test_len(self, store):
        """len(store) should reflect task count."""
        assert len(store) == 0
        store.create(Task("A", task_id="a1"))
        assert len(store) == 1
        store.create(Task("B", task_id="b2"))
        assert len(store) == 2
        store.delete("a1")
        assert len(store) == 1