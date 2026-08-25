"""Integration tests for the Persistent Cloud Task Lifecycle.

Drives the real path:

    API / submit  ->  CloudTaskStore  ->  TaskWorker  ->  serve.run_task
                                                        (existing executor)
    with thin persistence callbacks, then asserts the store ends in the
    correct state (status transitions, claimed_by/claimed_at, approval
    persistence/resolution, result/error capture, recovery).

Uses tiny fake executors so no network, Rift, or real approval UI is needed.
"""

import os
import sys
import tempfile
import threading
import time
import uuid

# Point the persistent store at an isolated directory BEFORE importing the
# modules (paths.DATA_DIR is evaluated at import time).
_TMP = tempfile.mkdtemp(prefix="kyrex_lifecycle_")
os.environ["KYREX_DATA_DIR"] = _TMP

import task_store as ts
import serve

FAKE_AUTO = os.path.join(_TMP, "fake_auto.py")
FAKE_APPROVAL = os.path.join(_TMP, "fake_approval.py")

with open(FAKE_AUTO, "w") as f:
    f.write(
        "import sys, json, time\n"
        "print('KYREX_PROGRESS: ' + json.dumps({'tool': 'fake', 'step': 'working'}))\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.15)\n"
        "print('KYREX_RESULT_JSON: ' + json.dumps({'status': 'done', 'branch': 'fake-branch'}))\n"
        "sys.stdout.flush()\n"
    )

with open(FAKE_APPROVAL, "w") as f:
    f.write(
        "import sys, json, time\n"
        "print('KYREX_PROGRESS: ' + json.dumps({'tool': 'fake', 'step': 'awaiting'}))\n"
        "sys.stdout.flush()\n"
        "print('KYREX_APPROVAL: ' + json.dumps({'tier': 1, 'summary': 'list the files', 'token': '', 'detail': 'approve?'}))\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.15)\n"
        "dec = ''\n"
        "try:\n"
        "    dec = sys.stdin.readline()\n"
        "except Exception:\n"
        "    dec = ''\n"
        "print('KYREX_RESULT_JSON: ' + json.dumps({'status': 'done', 'decision': dec.strip()}))\n"
        "sys.stdout.flush()\n"
    )

# Route synthetic executor prefixes at the fake scripts (absolute paths).
serve.EXECUTORS["fake"] = FAKE_AUTO
serve.EXECUTORS["fake_approval"] = FAKE_APPROVAL


def _poll(store, task_id, want, timeout=20.0):
    """Poll store.status until it is in *want* (a set) or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = store.status(task_id)
        if st in want:
            return st
        time.sleep(0.05)
    return store.status(task_id)


def test_auto_complete_and_claim_identity():
    store = ts.CloudTaskStore()
    wid = "worker-auto-" + uuid.uuid4().hex[:6]
    worker = ts.TaskWorker(store, worker_id=wid)

    tid = store.submit(
        session_key="sess-auto", task_text="do it", repo_url="https://x/y.git",
        executor_prefix="fake",
    )
    assert store.status(tid) == ts.STATUS_QUEUED
    ok = worker.claim_and_execute_once(timeout=5.0)
    assert ok is True

    t = store.get(tid)
    assert t["status"] == ts.STATUS_DONE, t["status"]
    assert t["result"]["branch"] == "fake-branch"
    # claimed_by / claimed_at captured at claim time
    assert t["claimed_by"] == wid
    assert t["claimed_at"]
    assert t["run_id"]
    # event stream reflects the full lifecycle
    types = [e["type"] for e in store.get_events(tid)]
    assert types[0] == "submitted"
    assert "claimed" in types and "status" in types
    assert types[-1] == "status"  # final set_status(done)
    # timestamps present
    assert t["created_at"] and t["started_at"] and t["finished_at"]
    store.close()


def test_approval_flow_persists_and_resolves():
    # Shorten the approval wait so the test does not block on human input.
    old_timeout = serve.APPROVAL_TIMEOUT
    serve.APPROVAL_TIMEOUT = 15
    try:
        store = ts.CloudTaskStore()
        wid = "worker-apr-" + uuid.uuid4().hex[:6]
        worker = ts.TaskWorker(store, worker_id=wid)

        tid = store.submit(
            session_key="sess-apr", task_text="approve me",
            repo_url="https://x/y.git", executor_prefix="fake_approval",
        )
        # Run the blocking execution in a background thread.
        th = threading.Thread(target=worker.claim_and_execute_once, kwargs={"timeout": 5.0})
        th.start()

        # Wait until the task is paused awaiting approval.
        st = _poll(store, tid, {ts.STATUS_AWAITING_APPROVAL}, timeout=10.0)
        assert st == ts.STATUS_AWAITING_APPROVAL, st

        # The approval_request must be durable and task_id-linked.
        pending = store.get_pending_approval(tid)
        assert pending is not None
        assert pending["task_id"] == tid
        assert pending["session_key"] == "sess-apr"
        assert pending["decision"] == "pending"

        # Operator responds using task_id (the restart-safe entry point).
        responded = store.respond(tid, "y")
        assert responded is True

        # Wait for completion.
        st = _poll(store, tid, ts.TERMINAL_STATUSES, timeout=15.0)
        assert st == ts.STATUS_DONE, st
        assert store.get(tid)["result"]["decision"] == "APPROVED"

        # Approval is now resolved (not pending) and the task resumed running.
        assert store.get_pending_approval(tid) is None
        types = [e["type"] for e in store.get_events(tid)]
        assert "approval_requested" in types
        assert "approval_resolved" in types
        store.close()
    finally:
        serve.APPROVAL_TIMEOUT = old_timeout


def test_same_session_serialized_across_workers():
    store = ts.CloudTaskStore()
    wid_a = "worker-A-" + uuid.uuid4().hex[:6]
    wid_b = "worker-B-" + uuid.uuid4().hex[:6]
    worker_a = ts.TaskWorker(store, worker_id=wid_a)
    worker_b = ts.TaskWorker(store, worker_id=wid_b)

    t1 = store.submit(session_key="sess-serial", task_text="one", executor_prefix="fake")
    t2 = store.submit(session_key="sess-serial", task_text="two", executor_prefix="fake")

    # Claim t1 (marks sess-serial busy) but leave it in-flight so the session
    # stays serialised.  Worker B must NOT be able to claim t2 (same session)
    # and must instead claim a different session's task.
    claimed = store.claim_next(wid_a)
    assert claimed is not None and claimed["task_id"] == t1
    assert store.status(t1) == ts.STATUS_RUNNING
    assert store.status(t2) == ts.STATUS_QUEUED, "t2 must stay queued while session is busy"

    # A different session's task is claimable concurrently on worker B and must
    # be preferred over the busy session's queued t2.
    t3 = store.submit(session_key="other-session", task_text="three", executor_prefix="fake")
    assert worker_b.claim_and_execute_once(timeout=5.0) is True
    assert store.status(t3) == ts.STATUS_DONE
    assert store.status(t2) == ts.STATUS_QUEUED, "t2 still blocked while session busy"

    # Release the serial session; now t2 can be claimed and completed.
    store.fail(t1, "test release of in-flight task")
    assert worker_b.claim_and_execute_once(timeout=5.0) is True
    assert store.status(t2) == ts.STATUS_DONE
    store.close()


def test_recovery_cancels_orphaned_awaiting_approval():
    store = ts.CloudTaskStore()
    tid = store.submit(session_key="sess-rec", task_text="rec", executor_prefix="fake")
    # Simulate a dead worker having claimed the task and paused for approval.
    store.claim_next("dead-worker-1")
    store.set_status(tid, ts.STATUS_AWAITING_APPROVAL)
    store.persist_approval_request(tid, "sess-rec", "msg-x", 1, "tok", "s", "d")

    live = {"live-worker-now"}
    recovered = store.recover_stale(live_worker_ids=live)
    assert any(r["task_id"] == tid for r in recovered)
    # Orphaned awaiting_approval task is cancelled, not left invisible.
    assert store.status(tid) == ts.STATUS_CANCELLED
    # Its pending approval is resolved as timed-out so it cannot leak.
    assert store.get_pending_approval(tid) is None
    store.close()


def test_queued_cancel_is_immediate():
    store = ts.CloudTaskStore()
    tid = store.submit(session_key="sess-cancel", task_text="cancel me", executor_prefix="fake")
    assert store.cancel(tid) is True
    assert store.status(tid) == ts.STATUS_CANCELLED
    store.close()


def test_worker_loop_auto_discovers_queued_task():
    """The REAL background worker loop must discover a queued task and run it
    on its own. We never call claim_and_execute_once() — discovery and
    execution must come entirely from TaskWorker.start()'s internal claim
    loop (claim_next -> execute_task)."""
    store = ts.CloudTaskStore()
    wid = "worker-loop-" + uuid.uuid4().hex[:6]
    worker = ts.TaskWorker(store, worker_id=wid)

    tid = store.submit(
        session_key="sess-loop", task_text="auto via loop",
        repo_url="https://x/y.git", executor_prefix="fake",
    )
    assert store.status(tid) == ts.STATUS_QUEUED

    # Start the real background worker. No manual claim_and_execute_once call.
    worker.start()
    try:
        assert worker.is_alive(), "background claim loop must be running"
        # The loop must discover the queued task and drive it to done.
        st = _poll(store, tid, {ts.STATUS_DONE, ts.STATUS_FAILED}, timeout=20.0)
        assert st == ts.STATUS_DONE, f"task ended as {st}, expected done"
    finally:
        worker.stop()

    t = store.get(tid)
    # Provenance: the loop's own claim_next() set claimed_by to this worker.
    assert t["claimed_by"] == wid, t["claimed_by"]
    assert t["result"]["branch"] == "fake-branch"
    # Full lifecycle recorded: submitted -> claimed -> ... -> status(done).
    types = [e["type"] for e in store.get_events(tid)]
    assert "submitted" in types and "claimed" in types
    store.close()


if __name__ == "__main__":
    test_auto_complete_and_claim_identity()
    print("PASS test_auto_complete_and_claim_identity")
    test_worker_loop_auto_discovers_queued_task()
    print("PASS test_worker_loop_auto_discovers_queued_task")
    test_approval_flow_persists_and_resolves()
    print("PASS test_approval_flow_persists_and_resolves")
    test_same_session_serialized_across_workers()
    print("PASS test_same_session_serialized_across_workers")
    test_recovery_cancels_orphaned_awaiting_approval()
    print("PASS test_recovery_cancels_orphaned_awaiting_approval")
    test_queued_cancel_is_immediate()
    print("PASS test_queued_cancel_is_immediate")
    print("ALL TESTS PASSED")
