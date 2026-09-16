"""test_agent_manual_fail_closed.py — manual_mode is a MANDATORY dependency.

Regression suite for the fail-closed import/initialization degradation of the
Browser Host agent's manual-control gate. Every requirement:

  1. missing manual_mode            -> task fails closed, ZERO executor
                                      constructions/starts (no operator
                                      subprocess is ever created);
  2. manual_mode import exception   -> same fail-closed refusal;
  3. lock initialization exception   -> same fail-closed refusal;
  4. lock contention (viewer owns)   -> ManualControlActive refusal, zero
                                      subprocesses;
  5. successful acquisition          -> lock held for the COMPLETE task and
                                      released afterward;
  6. exactly ONE bounded, redacted terminal error (no secret/path leakage);
  7. the deployed image still ships /host/manual_mode.py (delegates to the
     packaging regression).

Drive the REAL ``_handle_task`` from agent.py with the same scripted-Cloud
harness discipline as test_agent.py. No network, no daemon.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent as host_agent          # noqa: E402
import manual_mode as mm           # noqa: E402
import test_agent as ta            # noqa: E402 — reuse the scripted fakes

SECRET = ta._config().secret  # make sure "no leakage" is testable


class CountingExecutor(ta.FakeExecutor):
    """FakeExecutor that registers every CONSTRUCTION (the subprocess point)."""

    created: list = []


def _agent(monkeypatch, tmp_path, *, allowlist_off=False):
    """A HostAgent wired to a counting executor factory + scripted conn."""
    monkeypatch.setenv(mm.STATE_DIR_ENV, str(tmp_path))
    created: list = []

    def factory(task_text, env):
        ex = ta.FakeExecutor([
            {"kind": "operation", "op": "browser.submit",
             "target": "https://example.com/x", "summary": "submit",
             "detail": "d"},
            {"kind": "result", "result": {"status": "ok"}},
        ])
        ex.started = False
        created.append(ex)
        return ex

    agent = host_agent.HostAgent(
        ta._config(profiles_root=str(tmp_path)), connect=lambda: None,
        executor_factory=factory)
    agent._authed = True  # commands only honored on an authed channel
    return agent, created


def _conn() -> ta.ScriptedConn:
    return ta.ScriptedConn([], raise_when_empty=False)


def _result_errors(conn) -> list:
    return [f["payload"]["result"]["errors"]
            for f in conn.sent if f["type"] == "result"]


# ── 1. missing manual_mode → refuse, zero subprocesses ────────────────

def test_missing_manual_mode_fails_closed(monkeypatch, tmp_path):
    agent, created = _agent(monkeypatch, tmp_path)
    monkeypatch.setattr(host_agent, "manual_mode", None)
    monkeypatch.setattr(host_agent, "MANUAL_MODE_ERROR", "")
    conn = _conn()
    agent._handle_task(conn, ta._task(ta.CAND).get("payload"))
    assert created == []                      # ZERO executor constructions
    errors = _result_errors(conn)
    assert len(errors) == 1
    assert manual_marker(errors[0][0])


# ── 2. manual_mode import exception → refuse, zero subprocesses ───────

def test_broken_manual_mode_import_fails_closed(monkeypatch, tmp_path):
    agent, created = _agent(monkeypatch, tmp_path)
    monkeypatch.setattr(host_agent, "manual_mode", None)
    monkeypatch.setattr(host_agent, "MANUAL_MODE_ERROR", "SyntaxError")
    conn = _conn()
    agent._handle_task(conn, ta._task(ta.CAND).get("payload"))
    assert created == []
    errors = _result_errors(conn)
    assert len(errors) == 1
    err = errors[0][0]
    assert manual_marker(err)
    assert "SyntaxError" in err               # exception TYPE only


# ── 3. initialization/acquisition exception → fail closed ─────────────

def test_acquisition_exception_fails_closed(monkeypatch, tmp_path):
    agent, created = _agent(monkeypatch, tmp_path)

    class Boom:
        ManualControlActive = mm.ManualControlActive
        KIND_MANUAL = mm.KIND_MANUAL

        @staticmethod
        def state_dir(**_):
            raise RuntimeError("state root exploded")

    monkeypatch.setattr(host_agent, "manual_mode", Boom)
    conn = _conn()
    agent._handle_task(conn, ta._task(ta.CAND).get("payload"))
    assert created == []
    errors = _result_errors(conn)
    assert len(errors) == 1 and manual_marker(errors[0][0])


# ── 4. lock contention → ManualControlActive, zero subprocesses ───────

def test_lock_contention_refuses_due_to_viewer_kernel_lock(monkeypatch,
                                                          tmp_path):
    agent, created = _agent(monkeypatch, tmp_path)
    viewer = mm.acquire("owner-1", "bot-1", kind=mm.KIND_MANUAL, ttl=600,
                        root=str(tmp_path))
    try:
        conn = _conn()
        agent._handle_task(conn, ta._task(ta.CAND).get("payload"))
        assert created == []
        errors = _result_errors(conn)
        assert len(errors) == 1 and "ManualControlActive" in errors[0][0]
    finally:
        viewer.release()


# ── 5. full-lifetime lock: held through the task, released after ──────

def test_acquisition_holds_lock_for_full_task_and_releases(monkeypatch,
                                                           tmp_path):
    agent, created = _agent(monkeypatch, tmp_path)
    probe: dict = {}
    real = mm.acquire

    def spy(owner, bot, **kw):
        s = real(owner, bot, **kw)          # kw already carries kind/ttl/root
        probe["lock_fd"] = s._lock_fd
        probe["owner"] = str(owner)
        probe["bot"] = str(bot)
        return s

    monkeypatch.setattr(host_agent.manual_mode, "acquire", spy)
    # Pre-seed the decision the operation frame will trigger, so the task
    # terminates deterministically: operation -> ALLOW verdict -> result.
    conn = ta.ScriptedConn([
        ta._frame("verdict", {"task_id": "t1", "decision": "ALLOW"}),
    ], raise_when_empty=True)
    agent._handle_task(conn, ta._task(ta.CAND).get("payload"))
    ex = created[0]
    assert ex.started is True and ex.decisions == ["ALLOW"]  # ran the task
    assert "result" in [f["type"] for f in conn.sent]
    assert mm.active(probe["owner"], probe["bot"],
                     root=str(tmp_path)) is None      # released AFTERWARD
    assert probe.get("lock_fd") is not None


# ── 6. exactly one bounded redacted refusal; no leakage ───────────────

def manual_marker(err: str) -> bool:
    """Either terminal fail-closed marker of the manual-control boundary."""
    return "fail-closed" in err and (
        "manual-control safety subsystem unavailable" in err
        or "manual-control state unavailable" in err)


def test_refusal_is_bounded_and_leak_free(monkeypatch, tmp_path):
    agent, created = _agent(monkeypatch, tmp_path)
    monkeypatch.setattr(host_agent, "manual_mode", None)
    conn = _conn()
    agent._handle_task(conn, ta._task(ta.CAND).get("payload"))
    results = [f for f in conn.sent if f["type"] == "result"]
    assert len(results) == 1                        # exactly ONE terminal err
    blob = ta.json.dumps(results[0])
    assert str(tmp_path) not in blob                # no absolute path leak
    assert ta._config().secret not in blob          # no secret leak
    assert "site-packages" not in blob and "browser-host" not in blob
    assert len(blob) < 2000                         # bounded


# ── 7. deployed image still copies /host/manual_mode.py ───────────────

def test_deployed_image_still_ships_the_mandatory_dependency():
    import test_agent_packaging as pak

    dockerfile, _ctx, _wd = pak._cloud_compose_service()
    pak._imports_ok(dockerfile.read_text())  # fails loudly if the COPY leaves


def test_real_missing_import_records_rule_and_fails_closed(tmp_path):
    """Genuine missing-file scenario: import agent from a /host layout that
    lacks manual_mode.py — exactly the pre-fix image filesystem."""
    import shutil
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("agent.py", "host_allowlist.py", "profiles.py"):
        shutil.copy(os.path.join(here, name), host_dir / name)
    code = "import agent\nprint(agent.MANUAL_MODE_ERROR)\n"
    out = subprocess.run([sys.executable, "-c", code], cwd=str(host_dir),
                         capture_output=True, text=True)
    assert out.stdout.strip() == "ModuleNotFoundError", (
        "a missing manual_mode must be recorded as an import failure, "
        f"got: {out.stdout.strip()!r}")
