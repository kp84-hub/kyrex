#!/usr/bin/env python3
"""Focused regression tests for the web approval/isolation invariants.

Reproduces the concrete defects found in the audit of the web + worker
approval path:

  1. Unbound (web-submitted) sessions must be able to RESOLVE an approval
     the executor raised.  In production policy.MODE="enforce" an unbound
     session's UNBOUND_POLICY (safe reads only) default-denies every repo
     operation, so the pending approval was created with tier="deny" — a
     string that made handle_approval_reply consume ANY reply (even an
     unrelated message) as a blanket DENIED and made "y" unable to approve.
     The operator could never approve a web task.

  2. store.respond must only report delivery when the live in-memory
     pending approval exists in THIS process.  run_task executes in the
     worker process; the web API process has its own (empty)
     serve.pending_approvals, so a "y" reply was consumed as a stale reply,
     returned delivered=True, and the worker's pending approval was never
     resolved — the executor then timed out and denied.

  3. The results list must be scoped per session: list_tasks without a
     session_key filter exposed every user's task text and final response
     to any authenticated user.

Run: python3 test_web_approval_regression.py
"""
import os
import sys
import tempfile
import threading
import time
import uuid

# Point the persistent store at an isolated directory BEFORE importing the
# modules (paths.DATA_DIR is evaluated at import time).
_TMP = tempfile.mkdtemp(prefix="kyrex_web_approval_")
os.environ["KYREX_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import task_store as ts  # noqa: E402
import serve  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def _poll(store, task_id, want, timeout=20.0):
    if not isinstance(want, (set, list, tuple)):
        want = {want}
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = store.status(task_id)
        if st in want:
            return st
        time.sleep(0.05)
    return store.status(task_id)


# A fake executor that raises one T1 approval and echoes the host's decision.
FAKE_APPROVAL = os.path.join(_TMP, "fake_approval.py")
with open(FAKE_APPROVAL, "w") as f:
    f.write(
        "import sys, json\n"
        "sys.stdout.write('KYREX_APPROVAL:' + json.dumps(\n"
        "    {'tier': 1, 'summary': 'approve me', 'token': '', 'detail': ''}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "dec = ''\n"
        "try:\n"
        "    dec = sys.stdin.readline()\n"
        "except Exception:\n"
        "    dec = ''\n"
        "sys.stdout.write('KYREX_RESULT_JSON:' + json.dumps(\n"
        "    {'status': 'done', 'decision': dec.strip()}) + '\\n')\n"
        "sys.stdout.flush()\n"
    )
serve.EXECUTORS["fake_approval"] = FAKE_APPROVAL


# --- Test 1: unbound approval is operator-resolvable (deny-lock regression) --
print("\nTest 1: unbound (web) T1 approval can be approved by 'y'")
store = ts.CloudTaskStore()
wid = "w-apr-" + uuid.uuid4().hex[:6]
worker = ts.TaskWorker(store, worker_id=wid)
tid = store.submit(session_key="webuser", task_text="approve me",
                   repo_url="https://example.com/r.git",
                   executor_prefix="fake_approval")
th = threading.Thread(target=worker.claim_and_execute_once, kwargs={"timeout": 8.0})
th.start()
st = _poll(store, tid, {ts.STATUS_AWAITING_APPROVAL}, timeout=10.0)
check("task paused awaiting approval", st == ts.STATUS_AWAITING_APPROVAL, f"st={st}")

pending = store.get_pending_approval(tid)
check("durable approval pending",
      pending is not None and pending["decision"] == "pending",
      f"pending={pending}")

# Same-process reply (the only process where the live entry exists).
responded = store.respond(tid, "y")
check("same-process 'y' delivered", responded is True, f"responded={responded}")

st = _poll(store, tid, ts.TERMINAL_STATUSES, timeout=15.0)
check("task completed", st == ts.STATUS_DONE, f"st={st}")
result = store.get(tid)["result"]
check("executor received APPROVED",
      result and result.get("decision") == "APPROVED",
      f"result={result}")
check("approval resolved (no longer pending)",
      store.get_pending_approval(tid) is None)
store.close()


# --- Test 2: cross-process reply must not report false delivery ------------
print("\nTest 2: a reply without a live in-memory entry is not reported delivered")
store = ts.CloudTaskStore()
tid = store.submit(session_key="webuser", task_text="approve me",
                   executor_prefix="fake")
store.set_status(tid, ts.STATUS_RUNNING)
store.persist_approval_request(tid, "webuser", "msg-1", 1, "",
                               "approve me", "")
# A separate web process has its OWN serve.pending_approvals (empty here);
# the worker process that owns the live entry is simulated by clearing this
# process's in-memory table, which is exactly the state the web process sees.
serve.pending_approvals.clear()
check("no live in-memory entry in this process",
      len(serve.pending_approvals) == 0)
responded = store.respond(tid, "y")
check("reply NOT reported delivered cross-process",
      responded is False, f"responded={responded}")
still = store.get_pending_approval(tid)
check("durable approval still pending (not eaten, not resolved)",
      still is not None and still["decision"] == "pending",
      f"still={still}")
store.close()


# --- Test 3: results listing is scoped per session --------------------------
print("\nTest 3: list_tasks(session_key=...) never returns another session's task")
store = ts.CloudTaskStore()
a = store.submit(session_key="alice", task_text="alice secret",
                 repo_url="https://github.com/alice/r.git")
b = store.submit(session_key="bob", task_text="bob secret",
                 repo_url="https://github.com/bob/r.git")
store.complete(a, {"status": "done", "final_response": "alice output"})
store.complete(b, {"status": "done", "final_response": "bob output"})
alice_view = store.list_tasks(session_key="alice", limit=50)
check("alice sees only her own tasks",
      all(t["session_key"] == "alice" for t in alice_view))
check("bob's task is not in alice's view",
      all(t["task_id"] != b for t in alice_view),
      f"ids={[t['task_id'] for t in alice_view]}")
check("bob's task text is not visible to alice",
      all(t["task_text"] != "bob secret" for t in alice_view))
# The /api/results endpoint now uses exactly this filtered call.
store.close()

print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)