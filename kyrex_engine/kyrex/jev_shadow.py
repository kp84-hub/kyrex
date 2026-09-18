"""Passive, non-authoritative Jev observation of proposed tool calls.

The observer is deliberately outside Kyrex's policy and approval paths. It
accepts only metadata, submits work through a bounded nonblocking queue, and
writes results to a separate JSONL file. A result can never allow, deny,
modify, delay, or execute a tool call.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .decision import JevClient, JevError, RISK_QUESTION


_TRUE = frozenset({"1", "true", "yes", "on"})
DEFAULT_QUEUE_SIZE = 64

CATEGORY_QUESTION = {
    "type": "choice",
    "instructions": "Classify this proposed coding-agent tool operation.",
    "criteria": {
        "read": "Reads or searches existing information without changing it",
        "write": "Creates, edits, moves, or deletes data or code",
        "execute": "Runs a command, program, build, or test",
        "external": "Contacts or changes an external service or system",
        "other": "Does not clearly fit the other operation categories",
    },
}

REVIEW_QUESTION = {
    "type": "noul",
    "instructions": (
        "This proposed coding-agent operation warrants human review before "
        "execution because it may be destructive, difficult to reverse, or "
        "affect an external system."
    ),
}

SHADOW_QUESTIONS = {
    "risk": RISK_QUESTION,
    "category": CATEGORY_QUESTION,
    "needs_review": REVIEW_QUESTION,
}


def _enabled_from_env() -> bool:
    return (
        os.environ.get("KYREX_JEV_SHADOW", "").strip().lower() in _TRUE
        and bool(os.environ.get("TYPESAFE_API_KEY", "").strip())
    )


def _default_log_path() -> Path:
    session_dir = os.environ.get("KYREX_SESSION_DIR", "").strip()
    if session_dir:
        return Path(session_dir) / "jev_shadow.jsonl"
    configured = os.environ.get("KYREX_JEV_SHADOW_LOG", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".kyrex" / "jev_shadow.jsonl"


def tool_metadata(tool_name: str, args: Any) -> dict[str, Any]:
    """Return a value-free description of a proposed tool call."""
    safe_args = args if isinstance(args, dict) else {}
    names = sorted(str(name) for name in safe_args)
    return {
        "tool_name": str(tool_name),
        "surface": os.environ.get("KYREX_SURFACE", "terminal"),
        "argument_names": names,
        "argument_types": {
            str(name): type(value).__name__ for name, value in safe_args.items()
        },
        "has_path_argument": any(
            name in safe_args for name in ("path", "directory", "source", "destination")
        ),
        "has_command_argument": "command" in safe_args,
        "has_content_argument": any(
            name in safe_args for name in ("content", "text", "replacement")
        ),
        "has_url_argument": any(name in safe_args for name in ("url", "uri")),
    }


class JevShadowObserver:
    """Bounded background observer whose output is telemetry only."""

    def __init__(
        self,
        *,
        enabled: bool,
        log_path: Path | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        client_factory: Callable[[], JevClient] = JevClient,
    ) -> None:
        self.enabled = bool(enabled)
        self.log_path = Path(log_path) if log_path else _default_log_path()
        self._client_factory = client_factory
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(
            maxsize=max(1, int(queue_size))
        )
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "JevShadowObserver":
        return cls(enabled=_enabled_from_env())

    def observe(self, tool_name: str, args: Any) -> bool:
        """Queue one observation without waiting; return False when dropped."""
        if not self.enabled:
            return False
        try:
            item = {
                "observation_id": uuid.uuid4().hex,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "state": tool_metadata(tool_name, args),
            }
            self._ensure_worker()
            self._queue.put_nowait(item)
        except Exception:
            # This path is telemetry only. Resource exhaustion, thread startup,
            # serialization, or any other observer fault must be indistinguishable
            # from shadow mode being unavailable to the execution path.
            return False
        return True

    def _ensure_worker(self) -> None:
        if self._thread is not None:
            return
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="kyrex-jev-shadow",
                    daemon=True,
                )
                self._thread.start()

    def _run(self) -> None:
        client = None
        while True:
            item = self._queue.get()
            try:
                if client is None:
                    client = self._client_factory()
                item["result"] = client.decide(item["state"], SHADOW_QUESTIONS)
                item["status"] = "observed"
            except JevError as exc:
                item["status"] = "error"
                item["error"] = str(exc)
            except Exception as exc:  # telemetry failure must never escape
                item["status"] = "error"
                item["error"] = type(exc).__name__
            finally:
                self._append(item)
                self._queue.task_done()

    def _append(self, item: dict[str, Any]) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, sort_keys=True) + "\n")
                handle.flush()
        except OSError:
            pass

    def wait_for_idle_for_test(self) -> None:
        """Wait for queued observations; deterministic tests only."""
        self._queue.join()
