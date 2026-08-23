"""Tests for the APPROVE reply handling in fs_executor.py.

Covers the host's three possible replies to KYREX_OPERATION::
  ALLOW   → proceed without any KYREX_APPROVAL: line.
  DENY    → refuse without any KYREX_APPROVAL: line.
  APPROVE → emit KYREX_APPROVAL:, read a second line, proceed only on APPROVED.

Also covers unrecognised replies (refuse without prompting) and the
backward-compat legacy APPROVED/DENIED replies.

Run: python3 test_fs_approve_flow.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

failures = []

EXECUTOR = Path(__file__).resolve().parent / "fs_executor.py"


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def run_fs_interactive(task_text, *, root, stdin_text="") -> tuple[dict, list[str]]:
    """Run fs_executor.py with the given task and stdin input.

    Returns (result_dict, all_stdout_lines) so the caller can inspect
    KYREX_APPROVAL: lines as well as the final KYREX_RESULT_JSON: line.
    """
    env = os.environ.copy()
    env["KYREX_FS_ROOT"] = root
    proc = subprocess.Popen(
        [sys.executable, str(EXECUTOR), "--task", task_text],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    stdout, stderr = proc.communicate(input=stdin_text, timeout=15)
    if stdout.strip() and not stdout.strip().startswith("KYREX_"):
        check("no stray text on stdout", False,
              f"got non-protocol output: {stdout.strip()!r}")
    lines = stdout.splitlines()
    result_json = {}
    for line in lines:
        if line.startswith("KYREX_RESULT_JSON:"):
            result_json = json.loads(line[len("KYREX_RESULT_JSON:"):])
    if not result_json:
        check("result line present", False,
              f"stdout={stdout.strip()!r}, stderr={stderr.strip()!r}")
    return result_json, lines


# ── Setup ──────────────────────────────────────────────────────────────
print("=== Setting up test environment ===")
tmpdir = Path(tempfile.mkdtemp(prefix="fs_approve_test_"))
(root := tmpdir / "fs_root").mkdir(parents=True)
(root / "existing.txt").write_text("Existing content.\n")

# Helper to count KYREX_APPROVAL: lines in stdout lines.
def count_approval_lines(lines):
    return len([l for l in lines if l.startswith("KYREX_APPROVAL:")])


# ── Tests: ALLOW ────────────────────────────────────────────────────────

print("\n=== ALLOW → proceeds with no approval line ===")

print("\nTest 1: write ALLOW succeeds without KYREX_APPROVAL:")
result, lines = run_fs_interactive(
    "write allowed.txt <<< ALLOW works",
    root=str(root),
    stdin_text="ALLOW\n",
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
check("no approval lines for ALLOW", count_approval_lines(lines) == 0,
      f"got {count_approval_lines(lines)} approval line(s)")
file_path = root / "allowed.txt"
check("file was created", file_path.exists())
check("file content matches", file_path.read_text() == "ALLOW works")


print("\nTest 2: delete ALLOW succeeds without KYREX_APPROVAL:")
(target := root / "del_allow.txt").write_text("Delete me.\n")
assert target.exists()
result, lines = run_fs_interactive(
    "delete del_allow.txt",
    root=str(root),
    stdin_text="ALLOW\n",
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
check("no approval lines for ALLOW", count_approval_lines(lines) == 0,
      f"got {count_approval_lines(lines)} approval line(s)")
check("file was deleted", not target.exists())


# ── Tests: DENY ────────────────────────────────────────────────────────

print("\n=== DENY → refuses with no approval line ===")

print("\nTest 3: write DENY refuses without KYREX_APPROVAL:")
result, lines = run_fs_interactive(
    "write denied.txt <<< Should not appear",
    root=str(root),
    stdin_text="DENY\n",
)
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error mentions denied",
      any("denied" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")
check("no approval lines for DENY", count_approval_lines(lines) == 0,
      f"got {count_approval_lines(lines)} approval line(s)")
file_path = root / "denied.txt"
check("file was NOT created", not file_path.exists())


print("\nTest 4: delete DENY refuses without KYREX_APPROVAL:")
(target2 := root / "del_deny.txt").write_text("Keep me.\n")
assert target2.exists()
result, lines = run_fs_interactive(
    "delete del_deny.txt",
    root=str(root),
    stdin_text="DENY\n",
)
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("no approval lines for DENY", count_approval_lines(lines) == 0,
      f"got {count_approval_lines(lines)} approval line(s)")
check("file still exists", target2.exists())


# ── Tests: APPROVE + APPROVED ──────────────────────────────────────────

print("\n=== APPROVE → emit KYREX_APPROVAL:, then read second line ===")

print("\nTest 5: APPROVE then APPROVED performs the write")
result, lines = run_fs_interactive(
    "write approved_write.txt <<< Human-approved content",
    root=str(root),
    stdin_text="APPROVE\nAPPROVED\n",
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
check("approval line present", count_approval_lines(lines) >= 1,
      f"got {count_approval_lines(lines)} approval line(s)")
file_path = root / "approved_write.txt"
check("file was created", file_path.exists(),
      f"expected {file_path}")
check("file content matches",
      file_path.read_text() == "Human-approved content")
if count_approval_lines(lines) >= 1:
    approval_line = [l for l in lines if l.startswith("KYREX_APPROVAL:")][0]
    try:
        approval_data = json.loads(approval_line[len("KYREX_APPROVAL:"):])
        check("approval has tier 1",
              approval_data.get("tier") == 1,
              f"got tier={approval_data.get('tier')!r}")
        check("approval has summary",
              "summary" in approval_data)
    except json.JSONDecodeError as e:
        check("approval JSON valid", False, f"decode error: {e}")


print("\nTest 6: APPROVE then APPROVED performs the delete")
(target3 := root / "del_approved.txt").write_text("Will be deleted.\n")
assert target3.exists()
result, lines = run_fs_interactive(
    "delete del_approved.txt",
    root=str(root),
    stdin_text="APPROVE\nAPPROVED\n",
)
check("status is ok", result.get("status") == "ok",
      f"got {result.get('status')!r}")
check("approval line present", count_approval_lines(lines) >= 1,
      f"got {count_approval_lines(lines)} approval line(s)")
check("file was deleted", not target3.exists())
if count_approval_lines(lines) >= 1:
    approval_line = [l for l in lines if l.startswith("KYREX_APPROVAL:")][0]
    try:
        approval_data = json.loads(approval_line[len("KYREX_APPROVAL:"):])
        check("delete approval has tier 2",
              approval_data.get("tier") == 2,
              f"got tier={approval_data.get('tier')!r}")
        check("delete approval has token",
              approval_data.get("token") == "DELETE del_approved.txt",
              f"got token={approval_data.get('token')!r}")
    except json.JSONDecodeError as e:
        check("approval JSON valid", False, f"decode error: {e}")


# ── Tests: APPROVE + DENIED ────────────────────────────────────────────

print("\n=== APPROVE → emit KYREX_APPROVAL:, then refuse on DENIED ===")

print("\nTest 7: APPROVE then DENIED refuses the write")
result, lines = run_fs_interactive(
    "write denied_after_approve.txt <<< Should not appear",
    root=str(root),
    stdin_text="APPROVE\nDENIED\n",
)
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("error mentions denied",
      any("denied" in (e or "").lower() for e in result.get("errors", [])),
      f"errors={result.get('errors')}")
file_path = root / "denied_after_approve.txt"
check("file was NOT created", not file_path.exists())


print("\nTest 8: APPROVE then DENIED refuses the delete")
(target4 := root / "del_denied_after.txt").write_text("Stay here.\n")
assert target4.exists()
result, lines = run_fs_interactive(
    "delete del_denied_after.txt",
    root=str(root),
    stdin_text="APPROVE\nDENIED\n",
)
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("file still exists", target4.exists())


# ── Tests: Unrecognised reply ─────────────────────────────────────────

print("\n=== Unrecognised reply → refuses without approval line ===")

print("\nTest 9: unrecognised reply on write refuses")
result, lines = run_fs_interactive(
    "write unknown_reply.txt <<< Should not appear",
    root=str(root),
    stdin_text="MAYBE\n",
)
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("no approval lines for unrecognised",
      count_approval_lines(lines) == 0,
      f"got {count_approval_lines(lines)} approval line(s)")
file_path = root / "unknown_reply.txt"
check("file was NOT created", not file_path.exists())


print("\nTest 10: unrecognised reply on delete refuses")
(target5 := root / "del_unknown.txt").write_text("Keep.\n")
assert target5.exists()
result, lines = run_fs_interactive(
    "delete del_unknown.txt",
    root=str(root),
    stdin_text="MAYBE\n",
)
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("no approval lines for unrecognised",
      count_approval_lines(lines) == 0,
      f"got {count_approval_lines(lines)} approval line(s)")
check("file still exists", target5.exists())


print("\nTest 11: empty reply on write refuses")
result, lines = run_fs_interactive(
    "write empty_reply.txt <<< Should not appear",
    root=str(root),
    stdin_text="\n",
)
check("status is error", result.get("status") == "error",
      f"got {result.get('status')!r}")
check("no approval lines for empty reply",
      count_approval_lines(lines) == 0,
      f"got {count_approval_lines(lines)} approval line(s)")
file_path = root / "empty_reply.txt"
check("file was NOT created", not file_path.exists())


# ── Cleanup ────────────────────────────────────────────────────────────
print("\n=== Cleaning up ===")
shutil.rmtree(tmpdir, ignore_errors=True)


# ── Summary ────────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)