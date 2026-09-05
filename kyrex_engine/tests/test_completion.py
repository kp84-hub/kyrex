"""Agent-loop completion reliability tests.

Drive the real PlaneExecute.chat() loop with a stub provider and the real
ToolBox gate protocol (confirm_request / propose_edit) resolved the same way
core_bridge.stdin_thread resolves it — by writing to
toolbox._confirmation_results / _edit_results and signalling the pending
threading.Event.

Coverage required by the reliability fix approval:
- tool call → two tool-less rounds → eventual task_complete must complete
  (previously the loop stopped on the first tool-less round, or claimed
  "assumed complete" after two)
- a denied confirmation followed by tool-less rounds must NOT falsely
  complete; the loop keeps going until task_complete
- deletion confirmations always require an explicit decision (never auto)
- the existing propose_edit approval flow still works end-to-end
- the existing loop detector still aborts
- the existing circuit breaker still aborts
"""

import asyncio
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

import kyrex.toolbox as toolbox
from kyrex.core import PlaneExecute
from kyrex.providers.base import BaseProvider


# ── protocol helpers ───────────────────────────────────────────────────


def _tool_call(name, args, call_id="call_1", n=1):
    """Build an assistant response carrying n tool calls for *name*."""
    calls = []
    raw = args if isinstance(args, str) else json.dumps(args)
    for i in range(n):
        calls.append({
            "id": f"{call_id}_{i}",
            "type": "function",
            "function": {"name": name, "arguments": raw},
        })
    return {"role": "assistant", "content": "", "tool_calls": calls}


def _text(content):
    return {"role": "assistant", "content": content}


class StubProvider(BaseProvider):
    """Provider that replays a fixed script; the last step repeats."""

    name = "stub"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def chat(self, model, messages, tools=None, stream_callback=None,
                  reasoning_callback=None, interrupt_event=None,
                  final_round_callback=None):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return step(messages) if callable(step) else step


@pytest.fixture(autouse=True)
def auto_approve_gates():
    """Override conftest.auto_approve_gates.

    The suite-level conftest fixture patches the three blocking gate
    methods and _is_interactive so ordinary tests never hang. This module
    deliberately exercises the REAL gate protocol (confirm_request /
    propose_edit resolved through the shared _confirmation_results /
    _edit_results + threading.Event — exactly what core_bridge.stdin_thread
    does), so the blanket mocks must not apply here. Every test installs a
    GateResponder before any gate can fire.
    """
    yield


class GateResponder:
    """Wraps sys.stdout, captures protocol messages, resolves gates.

    Mirrors core_bridge.stdin_thread: confirm_request is resolved via
    _confirmation_results + _pending_confirmations, propose_edit via
    _edit_results + _pending_edits.
    """

    def __init__(self, confirm_approved=False, edit_accepted=True):
        self.buffer = io.StringIO()
        self.messages = []
        self.confirm_approved = confirm_approved
        self.edit_accepted = edit_accepted

    def write(self, text):
        self.buffer.write(text)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            self.messages.append(msg)
            mtype = msg.get("type")
            if mtype == "confirm_request":
                cid = msg.get("id")
                if cid is not None:
                    toolbox._confirmation_results[cid] = self.confirm_approved
                    event = toolbox._pending_confirmations.get(cid)
                    if event is not None:
                        event.set()
            elif mtype == "propose_edit":
                eid = msg.get("editId")
                if eid is not None:
                    toolbox._edit_results[eid] = self.edit_accepted
                    event = toolbox._pending_edits.get(eid)
                    if event is not None:
                        event.set()
        return len(text)

    def flush(self):
        pass

    def getvalue(self):
        return self.buffer.getvalue()

    def find(self, mtype):
        return [m for m in self.messages if m.get("type") == mtype]


# ── fixture ────────────────────────────────────────────────────────────


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Real PlaneExecute with a stub-config env and no disk writes."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("KYREX_API_KEY", "test-api-key-000000000000000000000000")
    monkeypatch.setenv("KYREX_PROVIDER", "openai")
    monkeypatch.setenv("KYREX_MODEL", "test-model")
    monkeypatch.setenv("KYREX_BASE_URL", "https://api.example.invalid/v1")
    engine = PlaneExecute()
    engine.session.save = lambda *a, **k: None
    engine.audit = SimpleNamespace(
        record_tool_call=lambda *a, **k: None,
        start_block=lambda *a, **k: None,
        flush=lambda *a, **k: None,
    )
    engine._max_recursion = 10
    engine._stream_handler = lambda chunk: None
    engine._reasoning_handler = None
    engine._final_round_handler = None
    return engine


def _run(engine, prompt="complete the task"):
    return asyncio.run(engine.chat(prompt))


# ── tests ──────────────────────────────────────────────────────────────


class TestPrematureTermination:
    """Tool-less rounds must never terminate a turn or imply completion."""

    def test_tool_then_two_toolless_rounds_then_task_complete(self, engine, tmp_path, monkeypatch):
        script = [
            _tool_call("search", {"pattern": "zzz_not_found", "path": "."}),
            _text("still working, no tools needed yet"),
            _text("still working, no tools needed yet"),
            _tool_call("task_complete", {"summary": "done"}),
        ]
        engine.provider = StubProvider(script)
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)
        res, _ = _run(engine)
        # The turn must survive tool-less rounds and end at task_complete.
        assert "[Task Complete: done]" in res
        # It must never claim completion on its own.
        assert "assumed complete" not in res
        # The soft nudge fired instead of a hard stop.
        assert "[continue]" in res

    def test_denied_confirmation_plus_toolless_rounds_does_not_false_complete(self, engine, tmp_path, monkeypatch):
        target = tmp_path / "out.txt"
        script = [
            _tool_call("write_file_with_gate", {"path": str(target), "content": "hello"}),
            _text("nothing more to add yet"),
            _text("nothing more to add yet"),
            _tool_call("task_complete", {"summary": "wrapped up"}),
        ]
        engine.provider = StubProvider(script)
        responder = GateResponder(confirm_approved=False)  # deny the write gate
        monkeypatch.setattr(sys, "stdout", responder)
        res, _ = _run(engine)
        assert "[Task Complete: wrapped up]" in res
        assert "assumed complete" not in res
        assert "[continue]" in res
        # Denied write must not reach the disk.
        assert not target.exists()
        confirms = responder.find("confirm_request")
        assert confirms, "write gate must emit confirm_request"
        assert confirms[0]["type"] == "confirm_request"


class TestExplicitApprovalOnly:
    """Deletions are never auto-approved; propose_edit still resolves."""

    def test_deletion_gate_requires_explicit_decision(self, engine, tmp_path, monkeypatch):
        victim = tmp_path / "victim.txt"
        victim.write_text("x")
        script = [
            _tool_call("run_command", {"command": f"rm {victim}"}),
            _text("deletion reviewed"),
            _tool_call("task_complete", {"summary": "done"}),
        ]
        engine.provider = StubProvider(script)
        responder = GateResponder(confirm_approved=False)  # DENY deletion
        monkeypatch.setattr(sys, "stdout", responder)
        monkeypatch.setattr("kyrex.toolbox._is_interactive", lambda: True)
        res, _ = _run(engine)
        # Denied deletion must never execute.
        assert victim.exists(), "denied deletion must not execute"
        deletions = [m for m in responder.find("confirm_request") if m.get("value") == "deletion"]
        assert deletions, "rm must emit a deletion confirm_request"
        assert deletions[0]["paths"] == [str(victim.resolve())]
        assert "[Task Complete: done]" in res
        assert "assumed complete" not in res

    def test_propose_edit_approval_still_works(self, engine, tmp_path, monkeypatch):
        target = tmp_path / "edited.txt"
        script = [
            _tool_call("write_file_with_gate", {"path": str(target), "content": "new content"}),
            _text("edit applied"),
            _tool_call("task_complete", {"summary": "done"}),
        ]
        engine.provider = StubProvider(script)
        responder = GateResponder(edit_accepted=True)  # accept the proposed edit
        monkeypatch.setenv("KYREX_VSCODE", "1")
        monkeypatch.setattr(sys, "stdout", responder)
        res, _ = _run(engine)
        assert target.read_text() == "new content", "accepted propose_edit must write the file"
        edits = responder.find("propose_edit")
        assert edits and edits[0]["type"] == "propose_edit"
        assert "[Task Complete: done]" in res


class TestSafeguards:
    """Existing hard guards still terminate the turn."""

    def test_loop_detector_still_aborts(self, engine, tmp_path, monkeypatch):
        repeated = _tool_call("search", {"pattern": "loop_zzz", "path": "."})

        def always_same(messages):
            return repeated

        engine.provider = StubProvider([always_same])
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)
        res, _ = _run(engine)
        assert "loop detected" in res
        assert "Task Complete" not in res

    def test_circuit_breaker_still_aborts(self, engine, tmp_path, monkeypatch):
        # One round with three tool calls, all with malformed args: the
        # circuit breaker fires inside that round.
        malformed = _tool_call("write_file_with_gate", "not-json{", n=3)
        engine.provider = StubProvider([malformed])
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)
        res, _ = _run(engine)
        assert "circuit breaker" in res
        assert "Task Complete" not in res