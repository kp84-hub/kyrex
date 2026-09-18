"""Deterministic tests for passive Jev tool-call observation."""

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kyrex.jev_shadow import (
    JevShadowObserver,
    SHADOW_QUESTIONS,
    _enabled_from_env,
    tool_metadata,
)


class FakeClient:
    def __init__(self, calls):
        self.calls = calls

    def decide(self, state, questions):
        self.calls.append((state, questions))
        return {
            "model": "jev-test",
            "answers": {
                "risk": {
                    "type": "choice",
                    "choice": "low",
                    "probabilities": {"low": 1.0},
                    "confidence": 1.0,
                }
            },
            "usage": {},
        }


def test_metadata_contains_no_argument_values(monkeypatch):
    monkeypatch.setenv("KYREX_SURFACE", "cloud")
    state = tool_metadata(
        "run_command",
        {"command": "git push SECRET", "path": "/private/repo", "content": "TOKEN"},
    )
    encoded = json.dumps(state)
    assert "git push SECRET" not in encoded
    assert "/private/repo" not in encoded
    assert "TOKEN" not in encoded
    assert state["argument_names"] == ["command", "content", "path"]
    assert state["has_command_argument"] is True
    assert state["surface"] == "cloud"


def test_disabled_observer_is_noop(tmp_path):
    observer = JevShadowObserver(enabled=False, log_path=tmp_path / "shadow.jsonl")
    assert observer.observe("read_local_file", {"path": "secret"}) is False
    assert not observer.log_path.exists()


def test_shadow_requires_explicit_flag_and_dedicated_key(monkeypatch):
    monkeypatch.delenv("KYREX_JEV_SHADOW", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert _enabled_from_env() is False
    monkeypatch.setenv("KYREX_JEV_SHADOW", "1")
    assert _enabled_from_env() is False
    monkeypatch.setenv("TYPESAFE_API_KEY", "typesafe-only")
    assert _enabled_from_env() is True


def test_observation_is_recorded_without_raw_values(tmp_path):
    calls = []
    log_path = tmp_path / "shadow.jsonl"
    observer = JevShadowObserver(
        enabled=True,
        log_path=log_path,
        client_factory=lambda: FakeClient(calls),
    )
    assert observer.observe("edit_file", {"path": "/secret", "content": "API_KEY"})
    observer.wait_for_idle_for_test()

    assert len(calls) == 1
    state, questions = calls[0]
    assert questions == SHADOW_QUESTIONS
    assert state["tool_name"] == "edit_file"
    record = json.loads(log_path.read_text(encoding="utf-8"))
    assert record["status"] == "observed"
    assert record["result"]["model"] == "jev-test"
    assert "/secret" not in log_path.read_text(encoding="utf-8")
    assert "API_KEY" not in log_path.read_text(encoding="utf-8")


def test_slow_worker_never_blocks_and_bounded_queue_drops(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class BlockingClient(FakeClient):
        def decide(self, state, questions):
            started.set()
            release.wait(timeout=2)
            return super().decide(state, questions)

    observer = JevShadowObserver(
        enabled=True,
        log_path=tmp_path / "shadow.jsonl",
        queue_size=1,
        client_factory=lambda: BlockingClient([]),
    )
    assert observer.observe("first", {}) is True
    assert started.wait(timeout=1)
    assert observer.observe("second", {}) is True
    assert observer.observe("dropped", {}) is False
    release.set()
    observer.wait_for_idle_for_test()

    records = observer.log_path.read_text(encoding="utf-8").splitlines()
    assert len(records) == 2


def test_observer_fault_never_escapes(monkeypatch, tmp_path):
    observer = JevShadowObserver(enabled=True, log_path=tmp_path / "shadow.jsonl")

    def boom():
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(observer, "_ensure_worker", boom)
    assert observer.observe("edit_file", {"content": "secret"}) is False
    assert not observer.log_path.exists()


def test_shadow_questions_are_advisory_only():
    assert set(SHADOW_QUESTIONS) == {"risk", "category", "needs_review"}
    assert not any(
        word in json.dumps(SHADOW_QUESTIONS).lower()
        for word in ("approve the operation", "deny the operation", "execute the operation")
    )


def test_core_observes_only_after_allowlist_and_before_execution():
    core = (Path(__file__).resolve().parents[1] / "kyrex" / "core.py").read_text()
    guard = core.index("if _allowed is not None and func_name not in _allowed:")
    observe = core.index("self.jev_shadow.observe(func_name, args)")
    tool_start = core.index("self._on_tool_start(func_name, args)")
    assert guard < observe < tool_start
