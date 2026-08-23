"""Regression tests for the KYREX_OPERATION: protocol in serve.py.

Covers:
  - tier-0 gets ALLOW with one audit entry and no approval prompt
  - tier-2 gets APPROVE
  - a policy-denied op gets DENY and is audited as blocked
  - a malformed JSON line gets DENY
  - an unknown op gets DENY
  - an executor-supplied tier field is ignored

Run: python3 test_operation_protocol.py
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
patched_audit_file = None


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def fake_send(chat_id, text):
    """Stub send — returns a dummy message id."""
    return 4444


def fake_edit(chat_id, msg_id, text):
    """Stub edit — no-op."""
    pass


def run_operation_test(
    stdout_lines: list[str],
    *,
    task_text="test task",
    executor_prefix="fs",
    session_key="op-test-session",
    extra_policy: dict | None = None,
) -> tuple[str, list[dict]]:
    """Run serve.run_task with a mocked subprocess and capture results.

    Returns (stdin_written, audit_entries) where stdin_written is the
    concatenation of everything the host wrote to the child's stdin, and
    audit_entries are the audit.log entries (newest first).
    """
    # Build context policy
    if extra_policy is not None:
        # Register a temporary bot with the given policy via bots module.
        import bots as _bots

        _bots.save_bots(
            {
                session_key: {
                    "id": session_key,
                    "rift": None,
                    "policy": extra_policy,
                }
            }
        )

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
            # Ensure lock is released even if run_task raises
            if serve.session_lock(session_key).locked():
                serve.session_lock(session_key).release()

    stdin_written = proc_mock.stdin.getvalue()
    entries = audit.read_entries()
    return stdin_written, entries


def audit_policy(policy: dict, operation: str, derived_tier: int) -> dict:
    """Helper to call policy.evaluate for test assertions."""
    return policy.evaluate(policy, operation, derived_tier)


# ------------------------------------------------------------------
# Test 1: tier-0 gets ALLOW with one audit entry and no prompt
# ------------------------------------------------------------------
print("Test 1: tier-0 gets ALLOW with one audit entry and no prompt")

original_mode = policy.MODE
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
            # After ALLOW, executor proceeds to emit result
            'KYREX_RESULT_JSON:{"status":"ok","final_response":"content"}\n',
        ],
        executor_prefix="fs",
    )

    check("stdin received ALLOW", "ALLOW\n" in stdin_written,
          f"got stdin={stdin_written!r}")
    check("one audit entry written", len(entries) == 1,
          f"got {len(entries)} entries")
    if entries:
        e = entries[0]
        check("decision is allow", e.get("decision") == "allow",
              f"got {e.get('decision')!r}")
        check("outcome is auto", e.get("outcome") == "auto",
              f"got {e.get('outcome')!r}")
        check("operation is fs.read", e.get("operation") == "fs.read",
              f"got {e.get('operation')!r}")
        check("detail has target", e.get("detail", {}).get("target") == "hello.txt",
              f"got detail={e.get('detail')!r}")
        check("detail has policy_rule", "policy_rule" in (e.get("detail") or {}),
              f"got detail={e.get('detail')!r}")

policy.MODE = original_mode


# ------------------------------------------------------------------
# Test 2: tier-2 gets APPROVE
# ------------------------------------------------------------------
print("\nTest 2: tier-2 (destructive verb) gets APPROVE")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op_line = json.dumps({
        "op": "fs.delete",
        "target": "important.txt",
        "summary": "delete important.txt",
        "detail": "1000 bytes\nThis is important data.",
    })

    stdin_written, entries = run_operation_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
            # After APPROVE, the executor would send KYREX_APPROVAL,
            # but this test only verifies the operation protocol response.
            'KYREX_RESULT_JSON:{"status":"no_changes","final_response":"done"}\n',
        ],
        executor_prefix="fs",
    )

    check("stdin received APPROVE", "APPROVE\n" in stdin_written,
          f"got stdin={stdin_written!r}")
    check("one audit entry written", len(entries) == 1,
          f"got {len(entries)} entries")
    if entries:
        e = entries[0]
        check("decision is approval_required", e.get("decision") == "approval_required",
              f"got {e.get('decision')!r}")
        check("outcome is auto", e.get("outcome") == "auto",
              f"got {e.get('outcome')!r}")
        check("tier is tier2", e.get("tier") == "tier2",
              f"got {e.get('tier')!r}")

policy.MODE = original_mode


# ------------------------------------------------------------------
# Test 3: policy-denied op gets DENY and is audited as blocked
# ------------------------------------------------------------------
print("\nTest 3: policy-denied op gets DENY and is audited as blocked")

policy.MODE = "enforce"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op_line = json.dumps({
        "op": "fs.write",
        "target": "secret.txt",
        "summary": "write secret.txt (42 bytes)",
        "detail": "diff...",
    })

    # Empty policy with enforce mode → no matching rule → effective deny
    stdin_written, entries = run_operation_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
        ],
        executor_prefix="fs",
        session_key="deny-test-session",
    )

    check("stdin received DENY", "DENY\n" in stdin_written,
          f"got stdin={stdin_written!r}")
    check("one audit entry written", len(entries) == 1,
          f"got {len(entries)} entries")
    if entries:
        e = entries[0]
        check("decision is deny", e.get("decision") == "deny",
              f"got {e.get('decision')!r}")
        check("outcome is blocked", e.get("outcome") == "blocked",
              f"got {e.get('outcome')!r}")
        check("operation is fs.write", e.get("operation") == "fs.write",
              f"got {e.get('operation')!r}")

policy.MODE = original_mode


# ------------------------------------------------------------------
# Test 4: malformed JSON line gets DENY
# ------------------------------------------------------------------
print("\nTest 4: malformed JSON line gets DENY")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    stdin_written, entries = run_operation_test(
        [
            "KYREX_OPERATION:not-valid-json\n",
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ],
        executor_prefix="fs",
        session_key="malformed-test-session",
    )

    check("stdin received DENY", "DENY\n" in stdin_written,
          f"got stdin={stdin_written!r}")
    check("no audit entries for malformed line", len(entries) == 0,
          f"got {len(entries)} entries — audit should not fire for unparseable")

policy.MODE = original_mode


# ------------------------------------------------------------------
# Test 5: unknown op gets DENY (no policy match in enforce mode)
# ------------------------------------------------------------------
print("\nTest 5: unknown op gets DENY")

policy.MODE = "enforce"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    op_line = json.dumps({
        "op": "fs.nonexistent",
        "target": "nowhere",
        "summary": "nonexistent op",
    })

    stdin_written, entries = run_operation_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
        ],
        executor_prefix="fs",
        session_key="unknown-op-test-session",
    )

    check("stdin received DENY", "DENY\n" in stdin_written,
          f"got stdin={stdin_written!r}")
    check("one audit entry written", len(entries) == 1,
          f"got {len(entries)} entries")
    if entries:
        e = entries[0]
        check("decision is deny", e.get("decision") == "deny",
              f"got {e.get('decision')!r}")
        check("outcome is blocked", e.get("outcome") == "blocked",
              f"got {e.get('outcome')!r}")
        check("operation is fs.nonexistent", e.get("operation") == "fs.nonexistent",
              f"got {e.get('operation')!r}")

policy.MODE = original_mode


# ------------------------------------------------------------------
# Test 6: executor-supplied tier field is ignored
# ------------------------------------------------------------------
print("\nTest 6: executor-supplied tier field is ignored")

policy.MODE = "dry_run"

with tempfile.TemporaryDirectory() as td:
    audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")

    # Executor sends tier=2, but the host must ignore it and derive
    # its own tier from the operation.  With tier=0 passed to
    # derive_tier for non-destructive summary, derived should be 0
    # and result should be ALLOW (not APPROVE).
    op_line = json.dumps({
        "op": "fs.read",
        "target": "readme.md",
        "summary": "read readme.md",
        "tier": 2,  # executor claims tier 2 — should be ignored
    })

    stdin_written, entries = run_operation_test(
        [
            f"KYREX_OPERATION:{op_line}\n",
            'KYREX_RESULT_JSON:{"status":"ok"}\n',
        ],
        executor_prefix="fs",
        session_key="tier-ignore-test-session",
    )

    # Should be ALLOW (tier-0 derived), not APPROVE (which would be tier-2)
    check("stdin received ALLOW (tier ignored, derived=0)", "ALLOW\n" in stdin_written,
          f"got stdin={stdin_written!r} — if APPROVE, tier was NOT ignored")
    check("one audit entry written", len(entries) == 1,
          f"got {len(entries)} entries")
    if entries:
        e = entries[0]
        check("decision is allow", e.get("decision") == "allow",
              f"got {e.get('decision')!r}")
        check("detail has ignored_executor_tier",
              e.get("detail", {}).get("ignored_executor_tier") == 2,
              f"got detail={e.get('detail')!r}")

policy.MODE = original_mode


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)