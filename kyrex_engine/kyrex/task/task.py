import time
import uuid
from typing import Optional


class Task:
    """Represents a single task to be tracked and executed.

    Phase 1a: model only — no scheduling or execution logic.
    Status lifecycle: pending -> running -> completed | failed
    """

    def __init__(
        self,
        description: str,
        *,
        task_id: Optional[str] = None,
        status: str = "pending",
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
    ) -> None:
        self.id: str = task_id or uuid.uuid4().hex[:12]
        self.description: str = description
        self.status: str = status
        self.created_at: float = created_at or time.time()
        self.updated_at: float = updated_at or self.created_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(
            description=data["description"],
            task_id=data.get("id"),
            status=data.get("status", "pending"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Task):
            return NotImplemented
        return self.id == other.id

    def __repr__(self) -> str:
        return f"Task(id={self.id!r}, desc={self.description!r}, status={self.status!r})"


class TaskStore:
    """In-memory store for Task objects.

    Phase 1a: no persistence, no deduplication beyond simple CRUD.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def create(self, task: Task) -> Task:
        if task.id in self._tasks:
            raise KeyError(f"Task with id {task.id!r} already exists")
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        return list(self._tasks.values())

    def update(self, task: Task) -> Task:
        if task.id not in self._tasks:
            raise KeyError(f"Task with id {task.id!r} not found")
        task.updated_at = time.time()
        self._tasks[task.id] = task
        return task

    def delete(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            return True
        return False

    def clear(self) -> None:
        self._tasks.clear()

    def __len__(self) -> int:
        return len(self._tasks)