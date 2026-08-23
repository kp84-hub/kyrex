"""Tests for resolve_bot and session-to-Bot binding in serve.py.

Covers:
  - resolve_bot returns the Bot dict when a Bot with matching id exists
  - resolve_bot returns None when no Bot is bound to that session key
  - resolve_bot never falls back to another Bot's record
  - A corrupt registry returns None and prints to stderr
  - run_task uses the resolved Bot's policy and id when bound
  - run_task uses empty policy and executor prefix when unbound
  - run_task records "session unbound" in the audit detail when unbound

Run: python3 test_bot_binding.py
"""
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve
import bots
import audit

# Shorten timeouts so tests don't hang on approval waits.
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

def _make_registry(bots_dict: dict, tmpdir: str) -> str:
    """Write *bots_dict* as JSON to a temp path and point bots.BOTS_FILE there.
    Returns the path."""
    path = os.path.join(tmpdir, "bots.json")
    with open(path, "w") as f:
        json.dump(bots_dict, f, indent=2)
    bots.BOTS_FILE = path
    return path


def _bot_dict(bot_id: str, policy: dict | None = None) -> dict:
    return {
        "id": bot_id,
        "name": f"Bot {bot_id}",
        "model": "test:model",
        "rift": "/tmp/rift_" + bot_id,
        "policy": policy if policy is not None else {},
        "created_at": "2025-01-01T00:00:00+00:00",
        "status": "stopped",
    }


# ── Test 1: resolve_bot returns the Bot dict when id matches ──────────────
print("Test 1: resolve_bot returns Bot dict when session key matches bot id")

with tempfile.TemporaryDirectory() as td:
    _make_registry({"alpha": _bot_dict("alpha", policy={"fs:read": 1})}, td)
    result = serve.resolve_bot("alpha")
    check("returns a dict", isinstance(result, dict), f"got {type(result)}")
    check("returns the correct bot", result and result["id"] == "alpha",
          f"id={result.get('id') if result else None}")
    check("carries the bot policy",
          result and result.get("policy") == {"fs:read": 1},
          f"policy={result.get('policy') if result else None}")


# ── Test 2: resolve_bot returns None for unmatched session key ────────────
print("\nTest 2: resolve_bot returns None for unmatched session key")

with tempfile.TemporaryDirectory() as td:
    _make_registry({"alpha": _bot_dict("alpha")}, td)
    result = serve.resolve_bot("unknown-session")
    check("returns None", result is None, f"got {result!r}")


# ── Test 3: resolve_bot never returns another Bot's record ────────────────
print("\nTest 3: resolve_bot never returns another Bot's record")

with tempfile.TemporaryDirectory() as td:
    _make_registry({
        "alpha": _bot_dict("alpha"),
        "beta": _bot_dict("beta", policy={"*": 2}),
    }, td)
    result = serve.resolve_bot("alpha")
    check("returns a dict", isinstance(result, dict))
    check("returned bot is alpha, not beta",
          result and result["id"] == "alpha",
          f"id={result.get('id') if result else None}")
    # beta's policy should NOT be visible when resolving alpha
    check("alpha's policy is empty (not beta's policy)",
          result and result.get("policy") == {},
          f"policy={result.get('policy') if result else None}")


# ── Test 4: corrupt registry returns None and prints to stderr ────────────
print("\nTest 4: corrupt registry returns None and prints to stderr")

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "bots.json")
    with open(path, "w") as f:
        f.write("{ not json")
    bots.BOTS_FILE = path

    stderr_buf = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = stderr_buf
    try:
        result = serve.resolve_bot("any-session")
        check("returns None on corrupt registry",
              result is None, f"got {result!r}")
        err_text = stderr_buf.getvalue()
        check("prints error to stderr",
              "bot registry load failed" in err_text,
              f"stderr={err_text!r}")
    finally:
        sys.stderr = old_stderr


# ── Test 5: run_task uses resolved Bot's policy and id when bound ─────────
print("\nTest 5: run_task uses resolved Bot's policy and id when bound")

# We test this by patching the subprocess to emit a KYREX_APPROVAL line,
# then intercepting the audit.log call to verify the bot_id and policy
# used. We also need to stub out send/edit.

with tempfile.TemporaryDirectory() as td:
    _make_registry({
        "bound-session": _bot_dict("bound-session", policy={"*": 1}),
    }, td)

    captured = {}

    def fake_send(chat_id, text):
        return 42  # message_id

    def fake_edit(chat_id, msg_id, text):
        pass

    with patch("serve.subprocess.Popen") as mock_popen:
        # Simulate a process that emits an approval line then exits.
        proc_mock = mock_popen.return_value
        proc_mock.stdout = [
            'KYREX_APPROVAL:{"summary":"read file","tier":1,"token":"abc"}\n',
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ]
        proc_mock.stderr = []
        proc_mock.returncode = 0
        proc_mock.stdin = io.StringIO()

        # Use a fresh AUDIT_FILE per test run so reads don't see old data
        audit_file = os.path.join(td, "audit.jsonl")
        audit.AUDIT_FILE = audit_file

        # run_task expects the lock to already be held (acquired by launch())
        serve.session_lock("bound-session").acquire()
        serve.run_task(
            chat_id=123,
            repo_url="https://example.com/repo.git",
            task_text="read file",
            executor_prefix="repo",
            send=fake_send,
            edit=fake_edit,
            session_key="bound-session",
        )

    # Read the audit entry and verify.
    entries = audit.read_entries()
    bound_entry = next((e for e in entries if e.get("bot_id") == "bound-session"), None)
    check("audit entry written with bound bot_id",
          bound_entry is not None,
          f"entries: {[(e.get('bot_id'), e.get('operation')) for e in entries]}")
    if bound_entry:
        check("audit bot_id is the bot's id, not executor prefix",
              bound_entry["bot_id"] == "bound-session",
              f"got {bound_entry['bot_id']!r}")
        check("audit detail does NOT contain 'session unbound' note",
              bound_entry.get("detail", {}).get("note") != "session unbound",
              f"detail={bound_entry.get('detail')}")
        # The policy was non-empty, so policy_info should be present
        check("audit detail contains policy info when policy is non-empty",
              "policy" in (bound_entry.get("detail") or {}),
              f"detail={bound_entry.get('detail')}")


# ── Test 6: run_task uses empty policy and executor prefix when unbound ───
print("\nTest 6: run_task uses empty policy and executor prefix when unbound")

with tempfile.TemporaryDirectory() as td:
    # No bots at all in the registry
    _make_registry({}, td)

    captured = {}

    def fake_send2(chat_id, text):
        return 43

    def fake_edit2(chat_id, msg_id, text):
        pass

    with patch("serve.subprocess.Popen") as mock_popen:
        proc_mock = mock_popen.return_value
        proc_mock.stdout = [
            'KYREX_APPROVAL:{"summary":"write file","tier":1,"token":"xyz"}\n',
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ]
        proc_mock.stderr = []
        proc_mock.returncode = 0
        proc_mock.stdin = io.StringIO()

        audit_file = os.path.join(td, "audit2.jsonl")
        audit.AUDIT_FILE = audit_file

        serve.session_lock("unbound-session").acquire()
        serve.run_task(
            chat_id=456,
            repo_url="https://example.com/repo.git",
            task_text="write file",
            executor_prefix="fs",
            send=fake_send2,
            edit=fake_edit2,
            session_key="unbound-session",
        )

    entries = audit.read_entries()
    unbound_entry = next((e for e in entries if e.get("bot_id") == "fs"), None)
    check("audit entry written with executor prefix as bot_id",
          unbound_entry is not None,
          f"entries: {[(e.get('bot_id'), e.get('operation')) for e in entries]}")
    if unbound_entry:
        check("unbound audit bot_id is executor prefix ('fs')",
              unbound_entry["bot_id"] == "fs",
              f"got {unbound_entry['bot_id']!r}")
        check("unbound audit detail contains 'session unbound' note",
              unbound_entry.get("detail", {}).get("note") == "session unbound",
              f"detail={unbound_entry.get('detail')}")


# ── Test 7: corrupt registry leaves task running unbound ──────────────────
print("\nTest 7: corrupt registry leaves task running unbound")

with tempfile.TemporaryDirectory() as td:
    # Write corrupt JSON
    path = os.path.join(td, "bots.json")
    with open(path, "w") as f:
        f.write("{ invalid json")
    bots.BOTS_FILE = path

    def fake_send3(chat_id, text):
        return 44

    def fake_edit3(chat_id, msg_id, text):
        pass

    with patch("serve.subprocess.Popen") as mock_popen:
        proc_mock = mock_popen.return_value
        proc_mock.stdout = [
            'KYREX_APPROVAL:{"summary":"delete file","tier":1,"token":"abc"}\n',
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ]
        proc_mock.stderr = []
        proc_mock.returncode = 0
        proc_mock.stdin = io.StringIO()

        stderr_buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = stderr_buf
        try:
            audit_file = os.path.join(td, "audit3.jsonl")
            audit.AUDIT_FILE = audit_file

            serve.session_lock("corrupt-session").acquire()
            serve.run_task(
                chat_id=789,
                repo_url="https://example.com/repo.git",
                task_text="delete file",
                executor_prefix="repo",
                send=fake_send3,
                edit=fake_edit3,
                session_key="corrupt-session",
            )
            err_text = stderr_buf.getvalue()
            check("task completes despite corrupt registry", True)
            check("stderr contains registry load failure",
                  "bot registry load failed" in err_text,
                  f"stderr={err_text!r}")
        finally:
            sys.stderr = old_stderr

    entries = audit.read_entries()
    corrupt_entry = next((e for e in entries if e.get("operation") == "delete file"), None)
    check("audit entry exists despite corrupt registry",
          corrupt_entry is not None,
          f"entries: {[(e.get('bot_id'), e.get('operation')) for e in entries]}")
    if corrupt_entry:
        check("corrupt-registry audit uses executor prefix as bot_id",
              corrupt_entry["bot_id"] == "repo",
              f"got {corrupt_entry['bot_id']!r}")
        check("corrupt-registry audit has 'session unbound' note",
              corrupt_entry.get("detail", {}).get("note") == "session unbound",
              f"detail={corrupt_entry.get('detail')}")


# ── Summary ────────────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)