"""Tests that a Bot bound to a persistent Rift hands that Rift to the repo
executor (git_workflow.py) via ``--rift``, while an unbound session does NOT
pass ``--rift`` and keeps its existing behaviour.

This is Step 1 of the KBot milestone: serve.run_task() must pass the bound
Bot's Rift explicitly to the repo executor.

Run: python3 test_repo_rift_binding.py
"""
import io
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve
import bots

# Shorten timeouts so the patched run_task doesn't hang on approval waits.
serve.APPROVAL_TIMEOUT = 1
serve.TASK_TIMEOUT = 5

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_registry(bots_dict, tmpdir):
    """Write *bots_dict* as JSON to a temp path and point bots.BOTS_FILE there."""
    path = os.path.join(tmpdir, "bots.json")
    with open(path, "w") as f:
        json.dump(bots_dict, f, indent=2)
    bots.BOTS_FILE = path
    return path


def _bot_dict(bot_id, rift, policy=None):
    return {
        "id": bot_id,
        "name": f"Bot {bot_id}",
        "model": "test:model",
        "rift": rift,
        "policy": policy if policy is not None else {},
        "created_at": "2025-01-01T00:00:00+00:00",
        "status": "stopped",
    }


def _capture_popen(captured):
    """Return a fake Popen that records the command and env it was called with."""
    class FakeProc:
        stdout = []
        stderr = []
        stdin = io.StringIO()
        returncode = 0
        pid = 1

        def poll(self):
            return 0

        def kill(self):
            pass

        def wait(self, *a, **kw):
            return 0

        def communicate(self, *a, **kw):
            return ("", "")

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        return FakeProc()

    return fake_popen


# ── Test 1: Bound Bot + repo executor passes --rift <bot-rift> ─────────────
print("Test 1: Bound Bot + executor_prefix='repo' passes --rift <bot-rift>")

with tempfile.TemporaryDirectory() as td:
    rift = os.path.join(td, "bot-rift")
    os.makedirs(rift)
    _make_registry({"b1": _bot_dict("b1", rift=rift)}, td)

    captured = {}
    serve.session_lock("b1").acquire()
    try:
        with patch("serve.subprocess.Popen", _capture_popen(captured)):
            serve.run_task(
                chat_id=1,
                repo_url="https://example.com/repo.git",
                task_text="do the thing",
                executor_prefix="repo",
                send=lambda c, t: 1,
                edit=lambda c, m, t: None,
                session_key="b1",
            )
    finally:
        pass

    cmd = captured.get("cmd", [])
    env = captured.get("env")
    check("executor is git_workflow.py",
          len(cmd) >= 2 and cmd[1].endswith("git_workflow.py"),
          f"cmd={cmd}")
    # The repo executor gets the Rift so the workspace is reused.
    check("command includes --rift",
          "--rift" in cmd,
          f"cmd={cmd}")
    if "--rift" in cmd:
        idx = cmd.index("--rift")
        check("--rift value equals the Bot's rift",
              cmd[idx + 1] == rift,
              f"got {cmd[idx + 1]!r} expected {rift!r}")
    # The same Rift is also delivered as KYREX_FS_ROOT, as before.
    check("KYREX_FS_ROOT env equals the Bot's rift",
          env is not None and env.get("KYREX_FS_ROOT") == rift,
          f"env={env}")
    check("--repo-url is still passed (needed to clone an empty rift)",
          "--repo-url" in cmd,
          f"cmd={cmd}")


# ── Test 2: Unbound session does NOT pass --rift; behaviour unchanged ───────
print("\nTest 2: Unbound session does NOT pass --rift; existing behaviour unchanged")

with tempfile.TemporaryDirectory() as td:
    _make_registry({}, td)  # empty registry — no bots bound

    captured = {}
    serve.session_lock("unbound-session").acquire()
    try:
        with patch("serve.subprocess.Popen", _capture_popen(captured)):
            serve.run_task(
                chat_id=2,
                repo_url="https://example.com/repo.git",
                task_text="do the thing",
                executor_prefix="repo",
                send=lambda c, t: 2,
                edit=lambda c, m, t: None,
                session_key="unbound-session",
            )
    finally:
        pass

    cmd = captured.get("cmd", [])
    env = captured.get("env")
    check("unbound command does NOT include --rift",
          "--rift" not in cmd,
          f"cmd={cmd}")
    check("unbound command still targets git_workflow.py",
          len(cmd) >= 2 and cmd[1].endswith("git_workflow.py"),
          f"cmd={cmd}")
    check("unbound command still includes --repo-url",
          "--repo-url" in cmd,
          f"cmd={cmd}")
    check("unbound command still includes --base",
          "--base" in cmd,
          f"cmd={cmd}")
    # Unbound: env should be inherited (None), not overridden with a rift.
    check("unbound executor env is None (inherit, no rift)",
          env is None,
          f"env={env!r}")


# ── Test 3: Non-repo executor is unaffected by a bound Bot's rift ───────────
print("\nTest 3: Non-repo executor (fs) is unaffected by a bound Bot's rift")

with tempfile.TemporaryDirectory() as td:
    rift = os.path.join(td, "fs-bot-rift")
    os.makedirs(rift)
    _make_registry({"fsbot": _bot_dict("fsbot", rift=rift)}, td)

    captured = {}
    serve.session_lock("fsbot").acquire()
    try:
        with patch("serve.subprocess.Popen", _capture_popen(captured)):
            serve.run_task(
                chat_id=3,
                repo_url="https://example.com/repo.git",
                task_text="write file",
                executor_prefix="fs",
                send=lambda c, t: 3,
                edit=lambda c, m, t: None,
                session_key="fsbot",
            )
    finally:
        pass

    cmd = captured.get("cmd", [])
    env = captured.get("env")
    check("fs executor command does NOT include --rift",
          "--rift" not in cmd,
          f"cmd={cmd}")
    # The fs executor still receives KYREX_FS_ROOT (unrelated to this change).
    check("fs executor env still carries KYREX_FS_ROOT",
          env is not None and env.get("KYREX_FS_ROOT") == rift,
          f"env={env!r}")


print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
