"""Tests for outcome-recording follow-up audit entries.

When the executor sends KYREX_RESULT_JSON after a human-approved operation,
serve.py writes a second audit entry recording the actual outcome. This file
tests that contract.

Run: python3 test_audit_outcome.py
"""
import io
import json
import os
import sys
import tempfile
import threading
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit
import policy
import serve

CHAT = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def fake_send(chat_id, text):
    return 4444


def fake_edit(chat_id, msg_id, text):
    pass


def run_outcome_test(
    stdout_lines: list[str],
    *,
    task_text="test task",
    executor_prefix="fs",
    session_key="outcome-test",
    extra_policy: dict | None = None,
    approval_timeout: int | None = None,
    approve_reply: tuple[str, int] | None = None,
) -> tuple[str, list[dict]]:
    """Run serve.run_task with a mocked subprocess.

    If *approve_reply* is set, a short-delay timer calls
    ``handle_approval_reply(reply_text, reply_to_id=approval_msg_id)``
    to simulate a human operator approving the pending approval.

    Returns (stdin_written, audit_entries) where audit_entries are the
    entries written during the task (newest first).
    """
    if extra_policy is not None:
        import bots as _bots

        _orig_bots_file = _bots.BOTS_FILE
        _bots_fd, _bots_tmp = tempfile.mkstemp(suffix="-bots.json")
        os.close(_bots_fd)
        _bots.BOTS_FILE = _bots_tmp

        _bots.save_bots(
            {
                session_key: {
                    "id": session_key,
                    "name": session_key,
                    "model": "test-model",
                    "rift": "/tmp/kyrex-test-rift",
                    "policy": extra_policy,
                    "created_at": "",
                    "status": "stopped",
                }
            }
        )

    orig_timeout = serve.APPROVAL_TIMEOUT
    if approval_timeout is not None:
        serve.APPROVAL_TIMEOUT = approval_timeout

    with patch("serve.subprocess.Popen") as mock_popen:
        proc_mock = mock_popen.return_value
        proc_mock.stdout = stdout_lines
        proc_mock.stderr = []
        proc_mock.returncode = 0
        proc_mock.stdin = io.StringIO()

        if approve_reply is not None:
            reply_text, reply_to_msg_id = approve_reply
            timer = threading.Timer(
                0.1,
                serve.handle_approval_reply,
                args=(CHAT, reply_text),
                kwargs={"reply_to_id": reply_to_msg_id, "session_key": session_key},
            )
            timer.start()

        serve.session_lock(session_key).acquire()
        try:
            serve.run_task(
                chat_id=CHAT,
                repo_url="https://example.com/repo.git",
                task_text=task_text,
                executor_prefix=executor_prefix,
                send=fake_send,
                edit=fake_edit,
                session_key=session_key,
            )
        finally:
            if serve.session_lock(session_key).locked():
                serve.session_lock(session_key).release()

    serve.APPROVAL_TIMEOUT = orig_timeout

    stdin_written = proc_mock.stdin.getvalue()
    entries = audit.read_entries()
    return stdin_written, entries


# ------------------------------------------------------------------
# Test 1: approved operation gets a follow-up entry whose outcome
#         matches the result status
# ------------------------------------------------------------------
print("Test 1: approved operation gets a follow-up entry whose outcome matches the result status")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op_line = json.dumps({
        "op": "fs.read",
        "target": "hello.txt",
        "summary": "read hello.txt",
    })
    approval_line = json.dumps({
        "summary": "read hello.txt",
        "token": "token-abc",
        "detail": "",
        "tier": 1,
    })

    stdin_written, entries = run_outcome_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
            f"KYREX_APPROVAL:{approval_line}\n",
            'KYREX_RESULT_JSON:{"status":"ok","final_response":"content"}\n',
        ],
        executor_prefix="fs",
        extra_policy={"fs:read": 1},
        session_key="outcome-approved",
        approval_timeout=30,
        approve_reply=("y", 4444),
    )

    check("three audit entries written (op + approval + follow-up)",
          len(entries) == 3,
          f"got {len(entries)} entries")

    if len(entries) >= 1:
        follow_up = entries[0]
        check("follow-up entry has op_id", "op_id" in follow_up,
              f"got keys={list(follow_up.keys())}")
        check("follow-up decision is 'approved'",
              follow_up.get("decision") == "approved",
              f"got {follow_up.get('decision')!r}")
        check("follow-up outcome matches result status",
              follow_up.get("outcome") == "ok",
              f"got {follow_up.get('outcome')!r}")
        check("follow-up operation is fs.read",
              follow_up.get("operation") == "fs.read",
              f"got {follow_up.get('operation')!r}")


# ------------------------------------------------------------------
# Test 2: denied operation does NOT get a follow-up entry
# ------------------------------------------------------------------
print("\nTest 2: denied operation does not get a follow-up entry")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op_line = json.dumps({
        "op": "fs.read",
        "target": "hello.txt",
        "summary": "read hello.txt",
    })
    approval_line = json.dumps({
        "summary": "read hello.txt",
        "token": "token-abc",
        "detail": "",
        "tier": 1,
    })

    stdin_written, entries = run_outcome_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
            f"KYREX_APPROVAL:{approval_line}\n",
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ],
        executor_prefix="fs",
        extra_policy={"fs:read": 1},
        session_key="outcome-denied",
        approval_timeout=1,
    )

    check("two audit entries (no follow-up for denied)",
          len(entries) == 2,
          f"got {len(entries)} entries")

    if len(entries) >= 1:
        latest = entries[0]
        check("latest entry decision is 'timeout' (not 'approved')",
              latest.get("decision") == "timeout",
              f"got {latest.get('decision')!r}")


# ------------------------------------------------------------------
# Test 3: task with no operations writes no follow-up
# ------------------------------------------------------------------
print("\nTest 3: task with no operations writes no follow-up")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    stdin_written, entries = run_outcome_test(
        [
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ],
        executor_prefix="fs",
        session_key="outcome-no-ops",
    )

    check("zero audit entries", len(entries) == 0,
          f"got {len(entries)} entries")


# ------------------------------------------------------------------
# Test 4: follow-up shares the op_id of the operation it reports on
# ------------------------------------------------------------------
print("\nTest 4: follow-up shares the op_id of the operation it reports on")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op_line = json.dumps({
        "op": "fs.read",
        "target": "notes.md",
        "summary": "read notes.md",
    })
    approval_line = json.dumps({
        "summary": "read notes.md",
        "token": "token-abc",
        "detail": "",
        "tier": 1,
    })

    stdin_written, entries = run_outcome_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
            f"KYREX_APPROVAL:{approval_line}\n",
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ],
        executor_prefix="fs",
        extra_policy={"fs:read": 1},
        session_key="outcome-opid-share",
        approval_timeout=30,
        approve_reply=("y", 4444),
    )

    check("three audit entries", len(entries) == 3,
          f"got {len(entries)} entries")

    if len(entries) == 3:
        follow_up = entries[0]
        op_entry = entries[2]

        check("operation entry has op_id",
              "op_id" in op_entry,
              f"got keys={list(op_entry.keys())}")
        check("follow-up has op_id",
              "op_id" in follow_up,
              f"got keys={list(follow_up.keys())}")

        if "op_id" in op_entry and "op_id" in follow_up:
            check("follow-up shares op_id of operation",
                  follow_up["op_id"] == op_entry["op_id"],
                  f"op_entry.op_id={op_entry['op_id']!r} "
                  f"!= follow_up.op_id={follow_up['op_id']!r}")

        check("follow-up decision is 'approved'",
              follow_up.get("decision") == "approved",
              f"got {follow_up.get('decision')!r}")
        check("follow-up outcome is 'ok'",
              follow_up.get("outcome") == "ok",
              f"got {follow_up.get('outcome')!r}")


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)