"""Tests for op_id correlation between operation and approval audit entries.

Run: python3 test_audit_correlation.py
"""
import io
import json
import os
import sys
import tempfile
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


def run_operation_test(
    stdout_lines: list[str],
    *,
    task_text="test task",
    executor_prefix="fs",
    session_key="corr-test-session",
    extra_policy: dict | None = None,
    approval_timeout: int | None = None,
) -> tuple[str, list[dict]]:
    """Run serve.run_task with a mocked subprocess and return audit entries.

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

    # Shorten approval timeout so tests don't hang
    orig_timeout = serve.APPROVAL_TIMEOUT
    if approval_timeout is not None:
        serve.APPROVAL_TIMEOUT = approval_timeout

    with patch("serve.subprocess.Popen") as mock_popen:
        proc_mock = mock_popen.return_value
        proc_mock.stdout = stdout_lines
        proc_mock.stderr = []
        proc_mock.returncode = 0
        proc_mock.stdin = io.StringIO()

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
# Test 1: tier-0 operation writes one entry carrying an op_id
# ------------------------------------------------------------------
print("Test 1: tier-0 operation writes one entry carrying an op_id")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op_line = json.dumps({
        "op": "fs.read",
        "target": "hello.txt",
        "summary": "read hello.txt",
        "detail": "5 bytes",
    })

    stdin_written, entries = run_operation_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
            'KYREX_RESULT_JSON:{"status":"ok","final_response":"content"}\n',
        ],
        executor_prefix="fs",
        extra_policy={"fs:read": 0},
        session_key="tier0-opid",
    )

    # Auto-allowed operations record an outcome too (96a433c): the
    # pre-decision allow entry plus a follow-up outcome entry once the
    # executor's KYREX_RESULT_JSON arrives. Entries are newest first.
    check("allow + outcome entries written", len(entries) == 2,
          f"got {len(entries)} entries")
    if len(entries) == 2:
        pre_entry, outcome_entry = entries[1], entries[0]
        check("entry has op_id", "op_id" in pre_entry,
              f"got keys={list(pre_entry.keys())}")
        check("op_id is a non-empty string",
              isinstance(pre_entry.get("op_id"), str) and len(pre_entry["op_id"]) > 0,
              f"got op_id={pre_entry.get('op_id')!r}")
        check("both entries share the same op_id",
              pre_entry.get("op_id") == outcome_entry.get("op_id"),
              f"pre={pre_entry.get('op_id')!r} outcome={outcome_entry.get('op_id')!r}")
        check("decision is allow",
              pre_entry.get("decision") == "allow"
              and outcome_entry.get("decision") == "allow",
              f"got {pre_entry.get('decision')!r} / {outcome_entry.get('decision')!r}")
        check("operation is fs.read",
              pre_entry.get("operation") == "fs.read"
              and outcome_entry.get("operation") == "fs.read",
              f"got {pre_entry.get('operation')!r} / {outcome_entry.get('operation')!r}")
        check("outcome records the executor result",
              outcome_entry.get("outcome") == "ok",
              f"got {outcome_entry.get('outcome')!r}")


# ------------------------------------------------------------------
# Test 2: tier-1 operation writes two entries sharing the same op_id
# ------------------------------------------------------------------
print("\nTest 2: tier-1 operation writes two entries sharing the same op_id")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op_line = json.dumps({
        "op": "fs.write",
        "target": "notes.md",
        "summary": "write notes.md",
        "detail": "updates section 3",
    })

    approval_line = json.dumps({
        "summary": "write notes.md",
        "token": "token-abc",
        "detail": "updates section 3",
        "tier": 1,
    })

    # tier-1 needs human approval; the approval will time out (APPROVAL_TIMEOUT=1s)
    stdin_written, entries = run_operation_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
            f"KYREX_APPROVAL:{approval_line}\n",
        ],
        executor_prefix="fs",
        extra_policy={"fs:write": 1},
        session_key="tier1-opid",
        approval_timeout=1,
    )

    check("two audit entries written", len(entries) == 2,
          f"got {len(entries)} entries")
    if len(entries) == 2:
        # Entries are newest first, so entries[0] is the approval and
        # entries[1] is the operation.
        op_entry = entries[1]
        approval_entry = entries[0]

        check("operation entry has op_id",
              "op_id" in op_entry,
              f"got keys={list(op_entry.keys())}")
        check("approval entry has op_id",
              "op_id" in approval_entry,
              f"got keys={list(approval_entry.keys())}")
        if "op_id" in op_entry and "op_id" in approval_entry:
            check("both entries share the same op_id",
                  op_entry["op_id"] == approval_entry["op_id"],
                  f"op_entry.op_id={op_entry['op_id']!r} "
                  f"!= approval_entry.op_id={approval_entry['op_id']!r}")
        # fs.write derives tier1 on the host; the pre-decision operation
        # entry records that derived tier, the approval entry the same tier
        # the human decided on.
        check("operation entry is tier1",
              op_entry.get("tier") == "tier1",
              f"got {op_entry.get('tier')!r}")
        check("approval entry is tier1",
              approval_entry.get("tier") == "tier1",
              f"got {approval_entry.get('tier')!r}")


# ------------------------------------------------------------------
# Test 3: two separate operations get different op_ids
# ------------------------------------------------------------------
print("\nTest 3: two separate operations get different op_ids")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op1_line = json.dumps({
        "op": "fs.read",
        "target": "a.txt",
        "summary": "read a.txt",
    })
    op2_line = json.dumps({
        "op": "fs.read",
        "target": "b.txt",
        "summary": "read b.txt",
    })

    stdin_written, entries = run_operation_test(
        [
            f"KYREX_OPERATION:{op1_line}\n",
            f"KYREX_OPERATION:{op2_line}\n",
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ],
        executor_prefix="fs",
        extra_policy={"fs:read": 0},
        session_key="two-ops-diff-ids",
    )

    # Two auto-allowed operations plus one follow-up outcome entry (the
    # executor sent a single KYREX_RESULT_JSON, attributed to the last
    # operation). Entries are newest first.
    check("two operation entries + one outcome written", len(entries) == 3,
          f"got {len(entries)} entries")
    if len(entries) == 3:
        # Newest first: entries[0] is the outcome entry, entries[1] is op2,
        # entries[2] is op1.
        id1 = entries[2].get("op_id")
        id2 = entries[1].get("op_id")
        outcome_entry = entries[0]
        check("first operation has op_id",
              isinstance(id1, str) and len(id1) > 0,
              f"got {id1!r}")
        check("second operation has op_id",
              isinstance(id2, str) and len(id2) > 0,
              f"got {id2!r}")
        check("outcome shares the last operation's op_id",
              outcome_entry.get("op_id") == id2,
              f"outcome={outcome_entry.get('op_id')!r} op2={id2!r}")
        check("two operations get different op_ids",
              id1 != id2,
              f"both are {id1!r}")


# ------------------------------------------------------------------
# Test 4: both entries for one operation report the same op and target
# ------------------------------------------------------------------
print("\nTest 4: both entries for one operation report the same op and target")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op_line = json.dumps({
        "op": "fs.write",
        "target": "config.yaml",
        "summary": "update config.yaml",
        "detail": "change log level",
    })

    approval_line = json.dumps({
        "summary": "update config.yaml",
        "token": "tok-xyz",
        "detail": "change log level",
        "tier": 1,
    })

    stdin_written, entries = run_operation_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
            f"KYREX_APPROVAL:{approval_line}\n",
        ],
        executor_prefix="fs",
        extra_policy={"fs:write": 1},
        session_key="same-op-target",
        approval_timeout=1,
    )

    check("two audit entries written", len(entries) == 2,
          f"got {len(entries)} entries")
    if len(entries) == 2:
        op_entry = entries[1]
        approval_entry = entries[0]

        check("operation entry reports op=fs.write",
              op_entry.get("operation") == "fs.write",
              f"got {op_entry.get('operation')!r}")
        check("approval entry reports op=fs.write",
              approval_entry.get("operation") == "fs.write",
              f"got {approval_entry.get('operation')!r}")

        op_target = op_entry.get("detail", {}).get("target", "")
        approval_target = approval_entry.get("detail", {}).get("target", "")
        check("operation entry reports target=config.yaml",
              op_target == "config.yaml",
              f"got {op_target!r}")
        check("approval entry reports target=config.yaml",
              approval_target == "config.yaml",
              f"got {approval_target!r}")


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)