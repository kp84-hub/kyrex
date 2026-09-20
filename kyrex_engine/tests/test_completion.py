"""Agent-loop completion reliability tests.

Drive the real PlaneExecute.chat() loop with a stub provider and the real
ToolBox gate protocol (confirm_request / propose_edit) resolved the same way
core_bridge.stdin_thread resolves it — by writing to
toolbox._confirmation_results / _edit_results and signalling the pending
threading.Event.

Coverage required by the reliability fix approval:
- two consecutive tool-less rounds carrying real content terminate the turn
  naturally on its first meaningful tool-less response (no task_complete needed)
- explicit task_complete stays authoritative and immediately terminal
- a tool-less round followed by a real tool call continues normally and
  resets the fallback counter
- the native fallback never fabricates a "[Task Complete: …]" summary and
  never shows a false "assumed complete" success marker
- a denied confirmation followed by tool-less rounds terminates naturally
  without falsely completing, and its gate lifecycle stays settled
- deletion confirmations always require an explicit decision (never auto)
- the existing propose_edit approval flow still works end-to-end
- the existing loop detector still aborts
- the existing circuit breaker still aborts
- the bridge emits exactly one chat_done per turn, fallback or not
"""

import asyncio
import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

import kyrex.toolbox as toolbox
from kyrex.core import PlaneExecute, _content_is_meaningful
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


class TestNativeCompletionFallback:
    """Meaningful tool-less assistant content is a native terminal response."""

    def test_first_meaningful_toolless_round_terminates(self, engine, tmp_path, monkeypatch):
        provider = StubProvider([
            _text("The build is fixed and the tests pass."),
            _text("must never be requested"),
        ])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        res, _ = _run(engine)

        assert provider.calls == 1
        assert "The build is fixed and the tests pass." in res
        assert "must never be requested" not in res
        assert "Max recursion" not in res

    def test_explicit_task_complete_terminates_immediately(self, engine, tmp_path, monkeypatch):
        provider = StubProvider([
            _tool_call("task_complete", {"summary": "immediate"}),
        ])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        res, _ = _run(engine)

        assert provider.calls == 1
        assert "[Task Complete: immediate]" in res
        assert "Max recursion" not in res

    def test_tool_round_then_final_response_terminates(self, engine, tmp_path, monkeypatch):
        provider = StubProvider([
            _tool_call("search", {"pattern": "zzz_none", "path": "."}),
            _text("Search complete; here is the final answer."),
            _text("must never be requested"),
        ])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        res, _ = _run(engine)

        assert provider.calls == 2
        assert "Search complete; here is the final answer." in res
        assert "must never be requested" not in res

    def test_native_completion_shows_no_false_success_marker(self, engine, tmp_path, monkeypatch):
        provider = StubProvider([_text("All changes are in place.")])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        res, _ = _run(engine)

        assert "[Task Complete:" not in res
        assert "[Task assumed complete" not in res
        assert "assumed complete" not in res

    def test_denied_confirmation_then_final_response_terminates(self, engine, tmp_path, monkeypatch):
        target = tmp_path / "out.txt"
        provider = StubProvider([
            _tool_call("write_file_with_gate", {
                "path": str(target), "content": "hello"}),
            _text("The write was denied; nothing was changed."),
            _tool_call("task_complete", {"summary": "never reached"}),
        ])
        engine.provider = provider
        responder = GateResponder(confirm_approved=False)
        monkeypatch.setattr(sys, "stdout", responder)

        res, _ = _run(engine)

        assert not target.exists()
        assert responder.find("confirm_request")
        assert provider.calls == 2
        assert "The write was denied; nothing was changed." in res
        assert "[Task Complete:" not in res


class TestMeaningfulToolLessRound:
    """Only real user-facing content is a native terminal response."""

    @pytest.mark.parametrize("content,expected", [
        (None, False),
        ("", False),
        ("   ", False),
        ("\n\t  \n", False),
        ("[continue] No tool calls this round and task_complete was not called.", False),
        ("[Task Complete: done]", False),
        ("[Task assumed complete after 2 empty rounds]", False),
        ("[Model produced reasoning but no display content. Check above output.]", False),
        ("[!] Task not verified complete — loop detected: repeating identical tool calls.", False),
        ("[!] Max recursion depth reached.", False),
        ("\n\n[continue] nudge\n[Task Complete: x]\n\n", False),
        ("Here is the answer.", True),
        ("\n  Real answer with surrounding whitespace.  \n", True),
        ("[continue] nudge\nHere is the answer.", True),
    ])
    def test_content_is_meaningful_predicate(self, content, expected):
        assert _content_is_meaningful(content) is expected

    def test_empty_round_does_not_finish_but_next_real_answer_does(
        self, engine, tmp_path, monkeypatch
    ):
        provider = StubProvider([_text(""), _text("real answer")])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        res, _ = _run(engine)

        assert provider.calls == 2
        assert "real answer" in res

    def test_reasoning_only_round_does_not_finish(self, engine, tmp_path, monkeypatch):
        def reasoning_only(messages):
            return {
                "role": "assistant",
                "content": "",
                "reasoning_content": "thinking about it, no answer yet",
            }

        provider = StubProvider([reasoning_only, _text("final answer")])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        res, _ = _run(engine)

        assert provider.calls == 2
        assert "final answer" in res

    def test_control_marker_only_round_does_not_finish(self, engine, tmp_path, monkeypatch):
        provider = StubProvider([
            _text("[continue] No tool calls this round and task_complete was not called."),
            _text("final answer"),
        ])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        res, _ = _run(engine)

        assert provider.calls == 2
        assert "final answer" in res

    def test_consecutive_tools_continue_until_final_content(
        self, engine, tmp_path, monkeypatch
    ):
        provider = StubProvider([
            _tool_call("search", {"pattern": "s1", "path": "."}),
            _tool_call("search", {"pattern": "s2", "path": "."}),
            _text("final after tools"),
        ])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        res, _ = _run(engine)

        assert provider.calls == 3
        assert "final after tools" in res


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


class TestProviderErrorTermination:
    """A provider error (e.g. OpenCode Go HTTP 429 usage/rate limit) must
    terminate the current task immediately.

    It is an explicit error result — NOT a tool-less assistant round. It must
    never increment the consecutive-tool-less-round counter, never reach the
    loop detector, and never trigger a re-prompt round.
    """

    def test_provider_429_terminates_immediately(self, engine, tmp_path, monkeypatch):
        # Provider always returns the explicit 429 usage-limit error dict.
        def provider_error(messages):
            return {
                "role": "assistant",
                "content": "[OpenAI Provider Error: GoUsageLimitError: 5-hour usage limit reached]",
                "tool_calls": None,
                "error": "Error code: 429 - GoUsageLimitError: 5-hour usage limit reached",
            }

        provider = StubProvider([provider_error])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)
        res, _ = _run(engine)

        # Terminates after EXACTLY one provider call — no retry loop, no
        # further reasoning rounds.
        assert provider.calls == 1
        # The useful provider message (Go usage/reset limit) is preserved.
        assert "Provider error" in res
        assert "GoUsageLimitError" in res
        assert "5-hour usage limit reached" in res
        # It must NOT be treated as tool-less rounds or a reasoning loop.
        assert "[continue]" not in res
        assert "loop detected" not in res
        # The meaningful tool-less streak and loop detector are untouched.
        assert engine._meaningful_toolless_streak == 0
        assert engine._loop_strike == 0


class TestBridgeChatDoneEmission:
    """core_bridge emits exactly ONE chat_done per turn.

    Drives the real bridge turn path (core_bridge._run_engine_turn) with the
    real engine + stub provider and asserts the protocol contract: a turn that
    ends via the native completion fallback emits exactly one chat_done (not
    zero, not two) followed by an IDLE phase sync, so the TUI returns to idle.
    """

    @staticmethod
    def _bridge():
        import importlib
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("core_bridge")

    def test_native_fallback_emits_single_chat_done(self, engine, tmp_path, monkeypatch):
        core_bridge = self._bridge()
        provider = StubProvider([
            _text("Here is the complete answer, part one."),
            _text("Here is the complete answer, part two."),
        ])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        asyncio.run(core_bridge._run_engine_turn(engine, "do the thing"))

        chat_done = responder.find("chat_done")
        assert len(chat_done) == 1, f"expected exactly one chat_done, got {len(chat_done)}"
        assert provider.calls == 1
        assert "part one" in chat_done[0]["content"]
        assert "part two" not in chat_done[0]["content"]
        idle = [m for m in responder.find("phase") if m.get("value") == "IDLE"]
        assert idle, "bridge must emit an IDLE phase sync after chat_done"

    def test_explicit_task_complete_also_emits_single_chat_done(self, engine, tmp_path, monkeypatch):
        core_bridge = self._bridge()
        provider = StubProvider([_tool_call("task_complete", {"summary": "done"})])
        engine.provider = provider
        responder = GateResponder()
        monkeypatch.setattr(sys, "stdout", responder)

        asyncio.run(core_bridge._run_engine_turn(engine, "finish it"))

        chat_done = responder.find("chat_done")
        assert len(chat_done) == 1, f"expected exactly one chat_done, got {len(chat_done)}"
        assert "[Task Complete: done]" in chat_done[0]["content"]