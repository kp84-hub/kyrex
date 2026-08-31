"""Regression tests for truncation detection in git_workflow.py.

Asserts that when the engine hits its recursion cap and emits
"[!] Max recursion depth reached." in the agent's final_response, the
KYREX_RESULT_JSON: line carries "status": "truncated" and a descriptive
final_response indicating the summary may be incomplete. A normal run (no
recursion marker) must NOT report truncated.

Run: python3 test_truncation_status.py
"""
import io
import json
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import git_workflow as gw

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []
_truncated = "[!] Max recursion depth reached."


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


class MockAgent:
    """Replacement for HeadlessAgent with controllable final_response.

    Matches the real HeadlessAgent's public interface that git_workflow.main()
    reads after run() completes: start() returns True, run() is a no-op.
    """
    def __init__(self, bridge, repo_dir, *, python="python3",
                 startup_timeout=60, idle_timeout=300, overall_timeout=1800,
                 on_event=None, read_only=False):
        self.final_response = ""
        self.reasoning = ""
        self.chat_done_seen = True
        self.approvals = []
        self.tool_calls = []
        self.errors = []
        # Keep the callback for compatibility; not invoked in tests.
        self.on_event = on_event

    def start(self, task):
        return True

    def run(self):
        pass


def _run_and_capture(truncated_response):
    """Run git_workflow.main() with heavy mocking and return the parsed
    KYREX_RESULT_JSON dict.

    Args:
        truncated_response: if truthy, the agent's final_response will contain
            the recursion marker text; if falsy, a normal response is used.
    """
    # Patch all heavy dependencies on the gw module.
    real_agent = gw.HeadlessAgent

    class FakeAgent(MockAgent):
        def __init__(self, bridge, repo_dir, **kw):
            super().__init__(bridge, repo_dir, **kw)
            if truncated_response:
                # Simulate engine returning recursion-cap message.
                self.final_response = (
                    "Some work was done before hitting the cap.\n"
                    "[!] Max recursion depth reached.\n"
                    "No further actions taken."
                )
            else:
                self.final_response = (
                    "Task completed successfully. All changes applied."
                )

    gw.HeadlessAgent = FakeAgent

    # Stub out git operations and API calls.
    def stub_prepare_workspace(args, branch):
        d = Path(tempfile.mkdtemp(prefix="test_trunc_"))
        return (d, "https://github.com/test/repo.git", lambda: None)

    def stub_commit_and_push(workdir, branch, task, remote_url, token,
                             read_only=False):
        return bool(truncated_response)  # "has changes" if truncated

    def stub_diff(*a, **kw):
        return ""

    def stub_review(*a, **kw):
        return {"available": True, "matches_task": True}

    def stub_pr(*a, **kw):
        return {"skipped": True, "reason": "no token"}

    def stub_find_bridge(*a, **kw):
        return Path("/tmp/test_bridge.py")

    gw.prepare_workspace = stub_prepare_workspace
    gw.commit_and_push = stub_commit_and_push
    gw.get_diff_since_base = stub_diff
    gw.review_diff = stub_review
    gw.open_pull_request = stub_pr
    gw.find_bridge_script = stub_find_bridge

    # Capture stdout.
    old_argv = sys.argv
    old_stdout = sys.stdout
    sys.argv = [
        "git_workflow.py",
        "--task", "test recursion truncation",
        "--local-repo", "/tmp/nonexistent",
        "--branch", f"kyrex/test-trunc-{int(time.time() * 1000000)}",
        "--keep-workdir",
    ]
    out = io.StringIO()
    sys.stdout = out
    try:
        gw.main()
    finally:
        sys.stdout = old_stdout
        sys.argv = old_argv
        gw.HeadlessAgent = real_agent

    output = out.getvalue()
    for line in output.splitlines():
        if "KYREX_RESULT_JSON:" in line:
            return json.loads(line.split("KYREX_RESULT_JSON:", 1)[1])
    raise RuntimeError(f"No KYREX_RESULT_JSON: line found in output:\n{output}")


# --- Test 1: truncated run ------------------------------------------------
print("\nTest 1: agent hits recursion cap — status is 'truncated'")
result = _run_and_capture(truncated_response=1)
check("status is 'truncated'",
      result.get("status") == "truncated",
      f"got {result.get('status')!r}")
check("final_response mentions cut short",
      "cut short" in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")
check("final_response may be incomplete",
      "may be incomplete" in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")


# --- Test 2: normal run must NOT be truncated -----------------------------
print("\nTest 2: normal completion — status is NOT 'truncated'")
result = _run_and_capture(truncated_response=0)
check("status is not 'truncated'",
      result.get("status") != "truncated",
      f"got {result.get('status')!r}")
check("final_response is the original agent output",
      "Task completed successfully" in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")
# The marker must not appear.
check("final_response does not contain recursion marker",
      "[!] Max recursion" not in result.get("final_response", ""),
      f"got {result.get('final_response')!r}")


# --- Test 3: truncated still carries expected metadata --------------------
print("\nTest 3: truncated result still has task/branch/metadata fields")
result = _run_and_capture(truncated_response=1)
check("task field present", "task" in result, f"{list(result.keys())}")
check("branch field present", "branch" in result)
check("base field present", "base" in result)
check("chat_done_seen is true", result.get("chat_done_seen") is True)
check("has_changes field present", "has_changes" in result)


# --- Test 4: empty final_response (no chat_done at all) must not crash -----
print("\nTest 4: empty final_response with recursion marker — graceful")
class SilentFailingAgent(MockAgent):
    def __init__(self, bridge, repo_dir, **kw):
        super().__init__(bridge, repo_dir, **kw)
        self.final_response = "[!] Max recursion depth reached."
        self.chat_done_seen = False

real_agent2 = gw.HeadlessAgent
gw.HeadlessAgent = SilentFailingAgent

old_argv = sys.argv
old_stdout = sys.stdout
sys.argv = [
    "git_workflow.py",
    "--task", "test empty response",
    "--local-repo", "/tmp/nonexistent",
    "--branch", f"kyrex/test-trunc-empty-{int(time.time() * 1000000)}",
    "--keep-workdir",
]
out = io.StringIO()
sys.stdout = out
try:
    gw.main()
finally:
    sys.stdout = old_stdout
    sys.argv = old_argv
    gw.HeadlessAgent = real_agent2

output = out.getvalue()
for line in output.splitlines():
    if "KYREX_RESULT_JSON:" in line:
        result_e = json.loads(line.split("KYREX_RESULT_JSON:", 1)[1])
        break
else:
    raise RuntimeError("No KYREX_RESULT_JSON line")

check("silent failing agent still gets 'truncated' status",
      result_e.get("status") == "truncated",
      f"got {result_e.get('status')!r}")
check("final_response says incomplete",
      "may be incomplete" in result_e.get("final_response", ""))
# has_changes is not set when chat_done_seen is False — that's expected.
check("no has_changes when agent didn't complete",
      "has_changes" not in result_e)


print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)