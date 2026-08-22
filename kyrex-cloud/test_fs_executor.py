"""Tests for fs_executor.py — reading files inside KYREX_FS_ROOT safely.

Covers: successful read, path escaping via .., path escaping via symlink,
missing file, and unsupported command.

Run: python3 test_fs_executor.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve

failures = []

EXECUTOR = Path(__file__).resolve().parent / "fs_executor.py"


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def run_fs(task_text, *, root) -> dict:
    """Run fs_executor.py with the given task and return the parsed result dict."""
    env = os.environ.copy()
    env["KYREX_FS_ROOT"] = root
    proc = subprocess.run(
        [sys.executable, str(EXECUTOR), "--task", task_text],
        capture_output=True, text=True, timeout=15,
        env=env,
    )
    # Diagnostics must go to stderr, never stdout.
    if proc.stdout.strip() and not proc.stdout.strip().startswith("KYREX_"):
        check("no stray text on stdout", False,
              f"got non-protocol output: {proc.stdout.strip()!r}")
    for line in proc.stdout.splitlines():
        if line.startswith("KYREX_RESULT_JSON:"):
            return json.loads(line[len("KYREX_RESULT_JSON:"):])
    check("result line present", False, f"stdout={proc.stdout.strip()!r}, stderr={proc.stderr.strip()!r}")
    return {}


# ── Setup: a temp directory with a controlled file structure ───────────
print("=== Setting up test environment ===")
tmpdir = Path(tempfile.mkdtemp(prefix="fs_executor_test_"))
(root := tmpdir / "fs_root").mkdir(parents=True)
(inner := root / "inner").mkdir()
(root / "hello.txt").write_text("Hello from the filesystem!\n")
(inner / "deep.txt").write_text("Deep file content.\n")

# Symlink inside the root pointing outside — escape attempt via symlink.
(outside_target := tmpdir / "outside.txt").write_text("I should not be readable via symlink.\n")
escape_link = root / "escape_link"
escape_link.symlink_to(str(outside_target))

# Symlink inside the root pointing to another file inside the root — this is safe.
safe_link = root / "safe_link"
safe_link.symlink_to(str(root / "hello.txt"))

print("Setup complete.\n")

# ── Tests ─────────────────────────────────────────────────────────────

# 1. Successful read inside root
print("\nTest 1: read a file inside the root succeeds")
result = run_fs("read hello.txt", root=str(root))
check("status is ok", result.get("status") == "ok", f"got {result.get('status')!r}")
check("final_response contains file content",
      result.get("final_response", "").strip() == "Hello from the filesystem!",
      f"got {result.get('final_response')!r}"[:200])
check("no errors", not result.get("errors"), f"errors={result.get('errors')}")


# 2. Path escaping via .. must be rejected
print("\nTest 2: path with .. escaping the root is rejected")
result = run_fs("read ../hello.txt", root=str(root))
check("status is error", result.get("status") == "error", f"got {result.get('status')!r}")
check("error message mentions escape or outside",
      any("escape" in (e or "").lower() or "outside" in (e or "").lower()
          for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# 3. Path escaping via symlink must be rejected
print("\nTest 3: symlink to outside root is rejected")
result = run_fs("read escape_link", root=str(root))
check("status is error", result.get("status") == "error", f"got {result.get('status')!r}")
check("error message mentions escape or outside",
      any("escape" in (e or "").lower() or "outside" in (e or "").lower()
          for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# 4. Missing file
print("\nTest 4: missing file reports error")
result = run_fs("read nonexistent.txt", root=str(root))
check("status is error", result.get("status") == "error", f"got {result.get('status')!r}")
check("error message mentions not found",
      any("not found" in (e or "").lower() or "not exist" in (e or "").lower()
          for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# 5. Unsupported command
print("\nTest 5: unsupported command is rejected")
result = run_fs("write hello.txt new content", root=str(root))
check("status is error", result.get("status") == "error", f"got {result.get('status')!r}")
check("error message says only read is supported",
      any("only read" in (e or "").lower() or "unsupported" in (e or "").lower()
          for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# 6. Path inside a subdirectory
print("\nTest 6: read file from a subdirectory")
result = run_fs("read inner/deep.txt", root=str(root))
check("status is ok", result.get("status") == "ok", f"got {result.get('status')!r}")
check("final_response contains deep file content",
      result.get("final_response", "").strip() == "Deep file content.",
      f"got {result.get('final_response')!r}"[:200])


# 7. Symlink to file inside root is allowed
print("\nTest 7: symlink to file inside root is allowed")
result = run_fs("read safe_link", root=str(root))
check("status is ok", result.get("status") == "ok", f"got {result.get('status')!r}")
check("final_response contains resolved file content",
      result.get("final_response", "").strip() == "Hello from the filesystem!",
      f"got {result.get('final_response')!r}"[:200])


# 8. KYREX_FS_ROOT doesn't exist
print("\nTest 8: KYREX_FS_ROOT does not exist reports error")
nonexistent_root = tmpdir / "no_such_dir"
result = run_fs("read hello.txt", root=str(nonexistent_root))
check("status is error", result.get("status") == "error", f"got {result.get('status')!r}")
check("error message mentions KYREX_FS_ROOT or does not exist",
      any("KYREX_FS_ROOT" in (e or "") or "does not exist" in (e or "").lower()
          for e in result.get("errors", [])),
      f"errors={result.get('errors')}")


# ── Cleanup ───────────────────────────────────────────────────────────
print("\n=== Cleaning up ===")
shutil.rmtree(tmpdir, ignore_errors=True)


# ── Summary ───────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)