"""Tests for ExecutionContext and build_context in serve.py.

Covers:
  - Bound session → context carries that Bot's rift and policy.
  - Unbound session → context carries no rift and empty policy.
  - Bound session's executor receives its rift as KYREX_FS_ROOT.
  - Unbound session's executor sees the inherited KYREX_FS_ROOT unchanged.

Run: python3 test_execution_context.py
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
import audit

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_registry(bots_dict: dict, tmpdir: str) -> str:
    """Write *bots_dict* as JSON to a temp path and point bots.BOTS_FILE there."""
    path = os.path.join(tmpdir, "bots.json")
    with open(path, "w") as f:
        json.dump(bots_dict, f, indent=2)
    bots.BOTS_FILE = path
    return path


def _bot_dict(bot_id: str, rift: str | None = None, policy: dict | None = None) -> dict:
    return {
        "id": bot_id,
        "name": f"Bot {bot_id}",
        "model": "test:model",
        "rift": rift or f"/tmp/rift_{bot_id}",
        "policy": policy if policy is not None else {},
        "created_at": "2025-01-01T00:00:00+00:00",
        "status": "stopped",
    }


def build_context_direct(session_key: str, executor_prefix: str = "repo"):
    """Direct wrapper so tests call the module function cleanly."""
    return serve.build_context(session_key, executor_prefix)


# ── Test 1: Bound session produces context carrying Bot's rift and policy ──
print("\nTest 1: Bound session → context carries that Bot's rift and policy")

with tempfile.TemporaryDirectory() as td:
    _make_registry({
        "my-bot": _bot_dict("my-bot", rift="/custom/rift", policy={"fs:read": 1}),
    }, td)

    ctx = build_context_direct("my-bot")
    check("context is an ExecutionContext",
          hasattr(ctx, "rift_path") and hasattr(ctx, "policy") and hasattr(ctx, "bot_id"),
          f"type={type(ctx)}")
    check("rift_path matches Bot's rift",
          ctx.rift_path == "/custom/rift",
          f"got {ctx.rift_path!r}")
    check("policy matches Bot's policy",
          ctx.policy == {"fs:read": 1},
          f"got {ctx.policy!r}")
    check("bot_id matches Bot's id",
          ctx.bot_id == "my-bot",
          f"got {ctx.bot_id!r}")
    check("capabilities is an empty dict",
          ctx.capabilities == {},
          f"got {ctx.capabilities!r}")
    check("session_id matches session_key",
          ctx.session_id == "my-bot",
          f"got {ctx.session_id!r}")


# ── Test 2: Unbound session produces context with no rift and empty policy ─
print("\nTest 2: Unbound session → context with no rift and empty policy")

with tempfile.TemporaryDirectory() as td:
    _make_registry({}, td)  # empty registry — no bots

    ctx = build_context_direct("unknown-session", executor_prefix="fs")
    check("rift_path is None",
          ctx.rift_path is None,
          f"got {ctx.rift_path!r}")
    # Unbound sessions are not policy-less: they get the explicit, auditable
    # safe-reads-only grant (serve.UNBOUND_POLICY) so harmless reads work
    # while everything else still default-denies.
    check("policy is UNBOUND_POLICY (safe reads only)",
          ctx.policy == dict(serve.UNBOUND_POLICY),
          f"got {ctx.policy!r}")
    check("bot_id is executor prefix ('fs')",
          ctx.bot_id == "fs",
          f"got {ctx.bot_id!r}")
    check("capabilities is an empty dict",
          ctx.capabilities == {},
          f"got {ctx.capabilities!r}")
    check("session_id matches session_key",
          ctx.session_id == "unknown-session",
          f"got {ctx.session_id!r}")


# ── Test 3: Unbound context with default executor_prefix ───────────────────
print("\nTest 3: Unbound context defaults bot_id to 'repo'")

with tempfile.TemporaryDirectory() as td:
    _make_registry({}, td)

    ctx = build_context_direct("no-bot-here")
    check("bot_id defaults to 'repo'",
          ctx.bot_id == "repo",
          f"got {ctx.bot_id!r}")


# ── Test 4: Bound session's executor receives rift as KYREX_FS_ROOT ────────
print("\nTest 4: Bound session → executor env carries KYREX_FS_ROOT")

with tempfile.TemporaryDirectory() as td:
    _make_registry({
        "rift-bot": _bot_dict("rift-bot", rift="/workspace/alpha", policy={}),
    }, td)

    # Patch Popen and capture the env parameter
    captured_env = [None]

    def capture_popen(cmd, **kwargs):
        captured_env[0] = kwargs.get("env")
        # Return a fake Popen to avoid actually running anything
        class FakeProc:
            stdout = []
            stderr = []
            stdin = io.StringIO()
            returncode = 0
            pid = 999
            def poll(self): return 0
            def kill(self): pass
            def wait(self, *a, **kw): return 0
            def communicate(self, *a, **kw): return ("", "")
        return FakeProc()

    real_popen = serve.subprocess.Popen
    try:
        serve.subprocess.Popen = capture_popen
        # We need to run run_task, but we can also test by directly
        # patching and checking. Actually, let's test via run_task which
        # is what uses the subprocess with env.
        # Set short timeouts so the test doesn't hang on approval waits.
        serve.APPROVAL_TIMEOUT = 1
        serve.TASK_TIMEOUT = 5
        serve.session_lock("rift-bot").acquire()
        serve.run_task(
            chat_id=123,
            repo_url="https://example.com/repo.git",
            task_text="read file",
            executor_prefix="fs",
            send=lambda c, t: 42,
            edit=lambda c, m, t: None,
            session_key="rift-bot",
        )
    finally:
        serve.subprocess.Popen = real_popen

    check("env parameter was passed to Popen",
          captured_env[0] is not None,
          f"got {captured_env[0]!r}")
    if captured_env[0] is not None:
        check("KYREX_FS_ROOT is set to Bot's rift",
              captured_env[0].get("KYREX_FS_ROOT") == "/workspace/alpha",
              f"got {captured_env[0].get('KYREX_FS_ROOT')!r}")
        # Verify it overrides any inherited value
        check("KYREX_FS_ROOT overrides inherited value",
              captured_env[0].get("KYREX_FS_ROOT") == "/workspace/alpha",
              f"inherited would be {os.environ.get('KYREX_FS_ROOT')!r}")


# ── Test 5: Unbound session's executor inherits env unchanged ─────────────
print("\nTest 5: Unbound session → executor env is None (inherited)")

# Set a known value in the environment to confirm it's inherited, not set
# by our code.
import subprocess as real_subprocess
os.environ["KYREX_FS_ROOT"] = "/inherited/root"

with tempfile.TemporaryDirectory() as td:
    _make_registry({}, td)  # empty — no bots

    captured_env = [None]

    def capture_popen2(cmd, **kwargs):
        captured_env[0] = kwargs.get("env")
        class FakeProc:
            stdout = []
            stderr = []
            stdin = io.StringIO()
            returncode = 0
            pid = 998
            def poll(self): return 0
            def kill(self): pass
            def wait(self, *a, **kw): return 0
            def communicate(self, *a, **kw): return ("", "")
        return FakeProc()

    real_popen = serve.subprocess.Popen
    try:
        serve.subprocess.Popen = capture_popen2
        serve.session_lock("unbound-for-env").acquire()
        serve.run_task(
            chat_id=456,
            repo_url="https://example.com/repo.git",
            task_text="write test.txt <<< hello",
            executor_prefix="fs",
            send=lambda c, t: 43,
            edit=lambda c, m, t: None,
            session_key="unbound-for-env",
        )
    finally:
        serve.subprocess.Popen = real_popen

    check("env parameter is None (inherit)",
          captured_env[0] is None,
          f"got {captured_env[0]!r}")


# ── Cleanup ────────────────────────────────────────────────────────────────
# Restore any modified globals
if "KYREX_FS_ROOT" in os.environ:
    del os.environ["KYREX_FS_ROOT"]

print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)