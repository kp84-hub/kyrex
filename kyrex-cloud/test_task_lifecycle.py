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

FAKE_APPROVAL_T2 = os.path.join(_TMP, "fake_approval_t2.py")
with open(FAKE_APPROVAL_T2, "w") as f:
    f.write(open(FAKE_APPROVAL).read().replace(
        "'tier': 1, 'summary': 'list the files', 'token': ''",
        "'tier': 2, 'summary': 'confirm write', 'token': 'confirm-lease'",
    ))

# Route synthetic executor prefixes at the fake scripts (absolute paths).
serve.EXECUTORS["fake"] = FAKE_AUTO
serve.EXECUTORS["fake_approval"] = FAKE_APPROVAL
serve.EXECUTORS["fake_approval_t2"] = FAKE_APPROVAL_T2


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

        # Operator reply is durably recorded; the worker-side bridge delivers it.
        assert store.record_operator_reply(tid, "y") is True
        assert store.record_operator_reply(tid, "n") is False
        assert store.deliver_operator_replies() == 1

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
    recovered = store.recover_stale(
        live_worker_ids=live, now=ts.datetime.now(ts.timezone.utc),
        approval_stale_after=0,
    )
    assert any(r["task_id"] == tid for r in recovered)
    # Orphaned awaiting_approval task is cancelled, not left invisible.
    assert store.status(tid) == ts.STATUS_CANCELLED
    # Its pending approval is resolved as timed-out so it cannot leak.
    assert store.get_pending_approval(tid) is None
    store.close()


def _age_task_lease(store, task_id, seconds=120):
    old = (ts.datetime.now(ts.timezone.utc)
           - ts.timedelta(seconds=seconds)).isoformat()
    with store._lock:
        store._conn.execute(
            "UPDATE tasks SET heartbeat_at = ? WHERE task_id = ?",
            (old, task_id),
        )
        store._conn.commit()


def test_expired_lease_recovers_live_worker_and_claims_successor():
    store = ts.CloudTaskStore()
    wid = "live-wedged-" + uuid.uuid4().hex[:6]
    first = store.submit(session_key="sess-lease-stale", task_text="wedged")
    second = store.submit(session_key="sess-lease-stale", task_text="successor")
    store.register_worker(wid)
    assert store.claim_next(wid)["task_id"] == first
    _age_task_lease(store, first)

    recovered = store.recover_stale(
        live_worker_ids={wid}, task_stale_after=30,
        now=ts.datetime.now(ts.timezone.utc),
    )
    assert [task["task_id"] for task in recovered] == [first]
    assert store.status(first) == ts.STATUS_FAILED
    # Recovery releases the existing session serialization gate, allowing the
    # queued successor to be claimed; claim_next's rule itself is unchanged.
    claimed = store.claim_next("successor-worker")
    assert claimed and claimed["task_id"] == second
    store.close()


def test_running_task_lease_renews_without_progress_callback():
    store = ts.CloudTaskStore()
    wid = "lease-running-" + uuid.uuid4().hex[:6]
    entered = threading.Event()
    release = threading.Event()

    def quiet_executor(**kwargs):
        entered.set()
        assert release.wait(5), "test executor was not released"
        kwargs["on_result"]({"status": "done"})

    worker = ts.TaskWorker(
        store, worker_id=wid, executor=quiet_executor, heartbeat_interval=0.02,
    )
    tid = store.submit(session_key="sess-lease-running", task_text="quiet run")
    worker.start()
    try:
        assert entered.wait(3)
        before = store.get(tid)["heartbeat_at"]
        time.sleep(0.08)
        after = store.get(tid)["heartbeat_at"]
        assert after > before
        assert store.status(tid) == ts.STATUS_RUNNING
        assert store.recover_stale(
            live_worker_ids={wid}, task_stale_after=1,
            now=ts.datetime.now(ts.timezone.utc),
        ) == []
        release.set()
        assert _poll(store, tid, {ts.STATUS_DONE}, timeout=3) == ts.STATUS_DONE
    finally:
        release.set()
        worker.stop()
        store.close()


def test_approval_wait_task_lease_renews_until_operator_reply():
    old_timeout = serve.APPROVAL_TIMEOUT
    serve.APPROVAL_TIMEOUT = 15
    store = ts.CloudTaskStore()
    wid = "lease-approval-" + uuid.uuid4().hex[:6]
    worker = ts.TaskWorker(
        store, worker_id=wid, heartbeat_interval=0.02,
    )
    tid = store.submit(
        session_key="sess-lease-approval", task_text="approval lease",
        executor_prefix="fake_approval_t2",
    )
    worker.start()
    try:
        assert _poll(store, tid, {ts.STATUS_AWAITING_APPROVAL}, timeout=5) == ts.STATUS_AWAITING_APPROVAL
        before = store.get(tid)["heartbeat_at"]
        time.sleep(0.08)
        after = store.get(tid)["heartbeat_at"]
        assert after > before
        assert store.recover_stale(
            live_worker_ids={wid}, approval_stale_after=1,
            now=ts.datetime.now(ts.timezone.utc),
        ) == []
        assert store.record_operator_reply(tid, "confirm-lease") is True
        assert store.deliver_operator_replies() == 1
        assert _poll(store, tid, ts.TERMINAL_STATUSES, timeout=5) == ts.STATUS_DONE
    finally:
        worker.stop()
        store.close()
        serve.APPROVAL_TIMEOUT = old_timeout


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


# ── Per-task lease scope + recovery race safety (lease blockers) ──────────


def test_claimed_task_without_execution_context_is_recovered():
    """A task owned by a LIVE worker but with no live execution context (its
    executor thread is gone) must NOT have its lease renewed by the worker's
    heartbeat, and must be recoverable.  This is the core lease-scope fix: the
    worker-wide renewal kept such a task alive forever.
    """
    store = ts.CloudTaskStore()
    wid = "ctxless-" + uuid.uuid4().hex[:6]
    worker = ts.TaskWorker(store, worker_id=wid, heartbeat_interval=0.02)
    tid = store.submit(session_key="sess-ctxless", task_text="no ctx")
    store.register_worker(wid)
    # Claim it directly: the worker owns it, but nothing is executing it.
    assert store.claim_next(wid)["task_id"] == tid
    _age_task_lease(store, tid)
    worker.start()
    try:
        before = store.get(tid)["heartbeat_at"]
        time.sleep(0.1)  # several heartbeat cycles
        after = store.get(tid)["heartbeat_at"]
        assert after == before, (
            "lease was renewed for a task with no live execution context"
        )
        recovered = store.recover_stale(
            live_worker_ids={wid}, task_stale_after=30,
            now=ts.datetime.now(ts.timezone.utc),
        )
        assert [t["task_id"] for t in recovered] == [tid]
        assert store.status(tid) == ts.STATUS_FAILED
    finally:
        worker.stop()
        store.close()


def test_dead_executor_thread_does_not_pin_task():
    """An executor thread that dies hard (no callback, no finalisation) under
    a still-live worker must stop having its lease renewed, so recovery can
    reclaim the task instead of it being pinned forever."""
    store = ts.CloudTaskStore()
    wid = "deadexec-" + uuid.uuid4().hex[:6]
    entered = threading.Event()

    def dying_executor(**_kw):
        entered.set()
        raise SystemExit("executor thread died hard")

    worker = ts.TaskWorker(
        store, worker_id=wid, executor=dying_executor, heartbeat_interval=0.02,
    )
    tid = store.submit(session_key="sess-deadexec", task_text="die")
    worker.start()
    try:
        assert entered.wait(3)
        time.sleep(0.2)  # let the pool observe the death and clear in-flight
        assert store.status(tid) == ts.STATUS_RUNNING
        before = store.get(tid)["heartbeat_at"]
        time.sleep(0.1)
        after = store.get(tid)["heartbeat_at"]
        assert after == before, "a dead executor thread still pinned the lease"
        # With the thread gone the lease stops advancing; age it to model the
        # elapsed time a real recovery scan would see.
        _age_task_lease(store, tid)
        recovered = store.recover_stale(
            live_worker_ids={wid}, task_stale_after=30,
            now=ts.datetime.now(ts.timezone.utc),
        )
        assert [t["task_id"] for t in recovered] == [tid]
        assert store.status(tid) == ts.STATUS_FAILED
    finally:
        worker.stop()
        store.close()


def test_fresh_lease_live_task_protected_from_recovery():
    """A genuinely live task (fresh lease, executing thread alive) must never
    be recovered, even with a very short stale window."""
    store = ts.CloudTaskStore()
    wid = "fresh-" + uuid.uuid4().hex[:6]
    entered = threading.Event()
    release = threading.Event()

    def blocking_executor(**kw):
        entered.set()
        assert release.wait(5), "test executor was not released"
        kw["on_result"]({"status": "done"})

    worker = ts.TaskWorker(
        store, worker_id=wid, executor=blocking_executor, heartbeat_interval=0.02,
    )
    tid = store.submit(session_key="sess-fresh", task_text="healthy")
    worker.start()
    try:
        assert entered.wait(3)
        assert store.recover_stale(
            live_worker_ids={wid}, task_stale_after=1,
            now=ts.datetime.now(ts.timezone.utc),
        ) == []
        assert store.status(tid) == ts.STATUS_RUNNING
        release.set()
        assert _poll(store, tid, {ts.STATUS_DONE}, timeout=3) == ts.STATUS_DONE
    finally:
        release.set()
        worker.stop()
        store.close()


def test_recovery_never_recovers_callers_own_inflight_task():
    """Even if a transient renewal failure ages a task's lease, the worker's
    own in-flight task is protected from recovery (no self-recovery)."""
    store = ts.CloudTaskStore()
    wid = "protect-" + uuid.uuid4().hex[:6]
    entered = threading.Event()
    release = threading.Event()

    def blocking_executor(**kw):
        entered.set()
        assert release.wait(5), "test executor was not released"
        kw["on_result"]({"status": "done"})

    worker = ts.TaskWorker(
        store, worker_id=wid, executor=blocking_executor,
        heartbeat_interval=30.0,  # no renewal during the test window
    )
    tid = store.submit(session_key="sess-protect", task_text="own task")
    worker.start()
    try:
        assert entered.wait(3)
        # Simulate a transient renewal failure: stale lease, live execution.
        _age_task_lease(store, tid, seconds=120)
        recovered = store.recover_stale(
            live_worker_ids={wid}, task_stale_after=30,
            now=ts.datetime.now(ts.timezone.utc),
            protect_task_ids={tid},
        )
        assert recovered == []
        assert store.status(tid) == ts.STATUS_RUNNING
        # Without protection the same aged lease WOULD be recovered, proving
        # the protection is what saved the live task.
        aged = store.get(tid)["heartbeat_at"]
        unprotected = store.recover_stale(
            live_worker_ids={wid}, task_stale_after=30,
            now=ts.datetime.now(ts.timezone.utc),
            protect_task_ids=set(),
        )
        assert [t["task_id"] for t in unprotected] == [tid]
        assert store.status(tid) == ts.STATUS_FAILED
        # (Restore running is unnecessary — the task is legitimately reclaimed
        # once unprotected; just release the executor so nothing hangs.)
        release.set()
    finally:
        release.set()
        worker.stop()
        store.close()


def test_pool_queued_task_stays_renewed_and_protected():
    """A claimed task waiting in a SATURATED pool (its executor thread has not
    started yet) must still be registered in-flight, so its lease is renewed
    and its own worker cannot falsely recover it.  Regression for the
    claim -> pool-queue window (registration must happen in _dispatch, before
    pool.submit, not inside _run_task_safe)."""
    store = ts.CloudTaskStore()
    wid = "queued-" + uuid.uuid4().hex[:6]
    entered = threading.Event()
    release = threading.Event()

    def blocking_executor(**kw):
        entered.set()
        assert release.wait(5), "test executor was not released"
        kw["on_result"]({"status": "done"})

    # ONE pool slot: task B is claimed (running) but queued behind A.
    worker = ts.TaskWorker(
        store, worker_id=wid, executor=blocking_executor,
        heartbeat_interval=0.02, max_workers=1,
    )
    a = store.submit(session_key="sess-q-a", task_text="a")
    b = store.submit(session_key="sess-q-b", task_text="b")
    worker.start()
    try:
        assert entered.wait(3)  # A is executing in the single pool slot
        # The claim loop claims B (a different session) and submits it; the
        # pool is full, so B sits queued with NO executor thread yet. Wait for
        # B to be registered in-flight (registration happens in _dispatch, a
        # moment after claim_next flips it to running).
        deadline = time.time() + 3
        while time.time() < deadline:
            with worker._inflight_lock:
                if b in worker._inflight:
                    break
            time.sleep(0.02)
        assert store.status(b) == ts.STATUS_RUNNING, store.status(b)
        with worker._inflight_lock:
            inflight = set(worker._inflight)
        assert a in inflight and b in inflight, f"inflight={inflight!r}"

        # Simulate that B waited: age its lease, then confirm the heartbeat
        # renews it (pre-fix, a queued task was never renewed).
        _age_task_lease(store, b)
        aged = store.get(b)["heartbeat_at"]
        time.sleep(0.1)  # several heartbeat cycles
        assert store.get(b)["heartbeat_at"] > aged, (
            "pool-queued task lease was not renewed"
        )

        # With a fresh lease the queued task must not be falsely recovered,
        # even with a short stale window and without explicit protection.
        assert store.recover_stale(
            live_worker_ids={wid}, task_stale_after=30,
            now=ts.datetime.now(ts.timezone.utc),
        ) == []
        assert store.status(b) == ts.STATUS_RUNNING

        release.set()
        assert _poll(store, a, {ts.STATUS_DONE}, timeout=5) == ts.STATUS_DONE
        assert _poll(store, b, {ts.STATUS_DONE}, timeout=5) == ts.STATUS_DONE
    finally:
        release.set()
        worker.stop()
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
    test_claimed_task_without_execution_context_is_recovered()
    print("PASS test_claimed_task_without_execution_context_is_recovered")
    test_dead_executor_thread_does_not_pin_task()
    print("PASS test_dead_executor_thread_does_not_pin_task")
    test_fresh_lease_live_task_protected_from_recovery()
    print("PASS test_fresh_lease_live_task_protected_from_recovery")
    test_recovery_never_recovers_callers_own_inflight_task()
    print("PASS test_recovery_never_recovers_callers_own_inflight_task")
    test_pool_queued_task_stays_renewed_and_protected()
    print("PASS test_pool_queued_task_stays_renewed_and_protected")
    print("ALL TESTS PASSED")
