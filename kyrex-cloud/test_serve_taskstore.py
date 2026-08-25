"""Tests for serve.py ↔ taskstore.py integration.

Covers:
  A. launch() returns a task_id (str) and the task runs to "done"; result is
     captured.
  B. launch() returns False when the session is already busy.
  C. run_task still completes and reports when TaskStore.update raises
     (a TaskStore failure must never break execution).
  D. launch() still returns a task_id and runs when TaskStore.create raises.
  E. workspace_ref is captured from the Bot's rift (PR #55 binding preserved)
     and the Bot id is recorded; KYREX_FS_ROOT / --rift are still delivered.
  F. The full status lifecycle includes running -> awaiting_approval -> done.
  G. A task that emits no result JSON is recorded as "failed" with an error.

No real subprocess or Telegram calls. serve.subprocess.Popen is mocked. Because
launch() spawns a background thread, the Popen patch is started before launch()
and stopped only after the task thread has finished (otherwise the thread would
use the real Popen). audit writes to a temp file so it doesn't touch the real
registry.

Run: python3 test_serve_taskstore.py
"""
import io
import os
import sys
import tempfile
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve
import taskstore
import audit

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def _wait_for(task_id, statuses, timeout=8.0):
    """Poll the store until the task reaches one of *statuses* (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rec = taskstore.get(task_id)
        if rec is not None and rec.get("status") in statuses:
            return rec
        time.sleep(0.02)
    return taskstore.get(task_id)


def _wait_for_lock_free(skey, timeout=6.0):
    """Poll until the session lock is free (the task thread has finished)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not serve.session_lock(skey).locked():
            return True
        time.sleep(0.02)
    return False


def _fake_popen(lines, returncode=0):
    """A fake subprocess.Popen whose stdout yields *lines* (newline-terminated)."""
    class FakeProc:
        def __init__(self):
            self.stdout = iter(lines)
            self.stderr = iter([])
            self.stdin = io.StringIO()
            self.returncode = returncode
            self.pid = 1234

        def wait(self, *a, **kw):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            pass

        def communicate(self, *a, **kw):
            return ("", "")

    return FakeProc()


def _run_fake(lines, returncode=0):
    """Factory for a mocked serve.subprocess.Popen."""
    return lambda *a, **kw: _fake_popen(lines, returncode)


# ── Module-level fixtures ──────────────────────────────────────────────────
print("=== Setting up test environment ===")
tmpdir = tempfile.mkdtemp(prefix="serve_taskstore_test_")
audit.AUDIT_FILE = os.path.join(tmpdir, "audit.jsonl")
print("Setup complete.\n")


# ── Test A: launch returns task_id and runs to done ────────────────────────
print("Test A: launch returns task_id and runs to done")
with tempfile.TemporaryDirectory() as td:
    taskstore.TASKSTORE_FILE = os.path.join(td, "tasks.json")
    serve.APPROVAL_TIMEOUT = 2
    serve.TASK_TIMEOUT = 30
    send = MagicMock()
    # Keep the patch alive until the spawned task thread has finished.
    patcher = patch("serve.subprocess.Popen",
                    _run_fake(['KYREX_RESULT_JSON:{"status":"ok","final_response":"did it"}\n']))
    patcher.start()
    try:
        tid = serve.launch(111, "https://x/repo.git", "do it",
                           executor_prefix="repo", send=send, edit=MagicMock(),
                           session_key="sess-a")
        rec = _wait_for(tid, {"done", "failed"})
    finally:
        patcher.stop()
    check("launch returns a task_id str",
          isinstance(tid, str) and len(tid) == 32, repr(tid))
    check("task reached done", rec is not None and rec["status"] == "done", repr(rec))
    check("result captured on the record",
          rec.get("result") == {"status": "ok", "final_response": "did it"},
          repr(rec))
    check("final result reported via send",
          any("did it" in str(c.args) for c in send.call_args_list))


# ── Test B: launch returns False when busy ─────────────────────────────────
print("\nTest B: launch returns False when the session is busy")
with tempfile.TemporaryDirectory() as td:
    taskstore.TASKSTORE_FILE = os.path.join(td, "tasks.json")
    send = MagicMock()
    lock = serve.session_lock("busy-sess")
    lock.acquire()
    try:
        with patch("serve.subprocess.Popen", _run_fake([])):
            res = serve.launch(222, "https://x/repo.git", "task",
                               session_key="busy-sess", send=send, edit=MagicMock())
        check("launch returns False when busy", res is False, repr(res))
        check("busy message sent", send.called)
    finally:
        if lock.locked():
            lock.release()


# ── Test C: TaskStore.update failures must not break execution ─────────────
print("\nTest C: run_task survives a TaskStore.update failure")
with tempfile.TemporaryDirectory() as td:
    taskstore.TASKSTORE_FILE = os.path.join(td, "tasks.json")
    serve.APPROVAL_TIMEOUT = 2
    serve.TASK_TIMEOUT = 30
    send = MagicMock()
    lock = serve.session_lock("c-sess")
    lock.acquire()
    try:
        with patch("serve.subprocess.Popen",
                   _run_fake(['KYREX_RESULT_JSON:{"status":"ok"}\n'])), \
             patch("taskstore.update", side_effect=RuntimeError("boom")):
            serve.run_task(333, "https://x/repo.git", "t", executor_prefix="repo",
                           send=send, edit=MagicMock(), session_key="c-sess",
                           task_id="forced-c-id")
        check("run_task did NOT raise on TaskStore failure", True)
        check("final result reported via send", send.called)
    finally:
        if lock.locked():
            lock.release()


# ── Test D: launch survives a TaskStore.create failure ─────────────────────
print("\nTest D: launch survives a TaskStore.create failure")
with tempfile.TemporaryDirectory() as td:
    taskstore.TASKSTORE_FILE = os.path.join(td, "tasks.json")
    serve.APPROVAL_TIMEOUT = 2
    serve.TASK_TIMEOUT = 30
    send = MagicMock()
    patcher = patch("serve.subprocess.Popen",
                    _run_fake(['KYREX_RESULT_JSON:{"status":"ok"}\n']))
    patcher.start()
    try:
        with patch("taskstore.create", side_effect=RuntimeError("create boom")):
            tid = serve.launch(444, "https://x/repo.git", "t", session_key="d-sess",
                               send=send, edit=MagicMock())
        finished = _wait_for_lock_free("d-sess")
    finally:
        patcher.stop()
    check("launch returns a task_id even when create fails",
          isinstance(tid, str) and len(tid) == 32, repr(tid))
    check("task still executed (thread finished)", finished)
    check("result reported though store was down",
          any("ok" in str(c.args) for c in send.call_args_list))


# ── Test E: workspace_ref captured from Bot's rift (PR #55 binding) ─────────
print("\nTest E: workspace_ref captured from Bot's rift; binding preserved")
import bots
with tempfile.TemporaryDirectory() as td:
    taskstore.TASKSTORE_FILE = os.path.join(td, "tasks.json")
    orig_bots_file = bots.BOTS_FILE
    bots.BOTS_FILE = os.path.join(td, "bots.json")
    bots.add_bot(bot_id="wb-bot", name="WB", model="test:model",
                 rift="/workspace/wb", status="running")
    serve.APPROVAL_TIMEOUT = 2
    serve.TASK_TIMEOUT = 30
    send = MagicMock()
    captured_env = {}
    captured_args = {}

    def _capture_popen(*a, **kw):
        captured_env["env"] = kw.get("env")
        captured_args["args"] = list(a[0])
        return _fake_popen(['KYREX_RESULT_JSON:{"status":"ok"}\n'])

    patcher = patch("serve.subprocess.Popen", _capture_popen)
    patcher.start()
    try:
        tid = serve.launch(555, "https://x/repo.git", "t", session_key="wb-bot",
                           send=send, edit=MagicMock())
        rec = _wait_for(tid, {"done", "failed"})
    finally:
        patcher.stop()
        bots.BOTS_FILE = orig_bots_file
    check("task bot_id is the bot id", rec.get("bot_id") == "wb-bot", repr(rec))
    check("workspace_ref captured from rift",
          rec.get("workspace_ref") == "/workspace/wb", repr(rec))
    check("KYREX_FS_ROOT delivered to executor (PR #55 binding)",
          captured_env.get("env", {}).get("KYREX_FS_ROOT") == "/workspace/wb",
          repr(captured_env.get("env")))
    check("--rift passed to repo executor",
          "/workspace/wb" in captured_args.get("args", []),
          repr(captured_args.get("args")))


# ── Test F: full status lifecycle includes awaiting_approval ───────────────
print("\nTest F: status lifecycle running -> awaiting_approval -> done")
with tempfile.TemporaryDirectory() as td:
    taskstore.TASKSTORE_FILE = os.path.join(td, "tasks.json")
    serve.APPROVAL_TIMEOUT = 1  # short so the unresolved approval times out fast
    serve.TASK_TIMEOUT = 30
    real_update = taskstore.update
    seen = []

    def _spy(task_id, **fields):
        if "status" in fields:
            seen.append(fields["status"])
        return real_update(task_id, **fields)

    send = MagicMock(return_value=999)
    lock = serve.session_lock("f-sess")
    lock.acquire()
    try:
        with patch("serve.subprocess.Popen", _run_fake([
            'KYREX_APPROVAL:{"summary":"write file","tier":1,"token":"xyz"}\n',
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ])), patch("taskstore.update", side_effect=_spy):
            serve.run_task(666, "https://x/repo.git", "t", executor_prefix="repo",
                           send=send, edit=MagicMock(), session_key="f-sess",
                           task_id="forced-f-id")
        check("status 'running' was recorded", "running" in seen, repr(seen))
        check("status 'awaiting_approval' was recorded",
              "awaiting_approval" in seen, repr(seen))
        check("status 'done' was recorded", "done" in seen, repr(seen))
    finally:
        if lock.locked():
            lock.release()


# ── Test G: no-result task is recorded as failed ───────────────────────────
print("\nTest G: a task with no result JSON is recorded as failed")
with tempfile.TemporaryDirectory() as td:
    taskstore.TASKSTORE_FILE = os.path.join(td, "tasks.json")
    serve.APPROVAL_TIMEOUT = 2
    serve.TASK_TIMEOUT = 30
    send = MagicMock()
    patcher = patch("serve.subprocess.Popen", _run_fake([], returncode=1))
    patcher.start()
    try:
        tid = serve.launch(777, "https://x/repo.git", "t",
                           session_key="g-sess", send=send, edit=MagicMock())
        rec = _wait_for(tid, {"done", "failed"})
    finally:
        patcher.stop()
    check("no-result task is failed",
          rec is not None and rec["status"] == "failed", repr(rec))
    check("error captured on the record", bool(rec.get("error")), repr(rec))


# ── Cleanup ────────────────────────────────────────────────────────────────
print("\n=== Cleaning up ===")
import shutil
shutil.rmtree(tmpdir, ignore_errors=True)


# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
