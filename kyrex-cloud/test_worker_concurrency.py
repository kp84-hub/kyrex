"""Focused concurrency tests for the single-worker claim loop (Milestone 1 fix).

PR #63 introduced a single-worker process whose ``TaskWorker._claim_loop``
called ``execute_task`` synchronously, so different Bots could not actually run
concurrently in the deployed single worker process.  This module proves the
fix:

  1. Bot A can remain running while Bot B is claimed/executed (single worker,
     different-Bot concurrency via the bounded execution pool).
  2. Two tasks for the same Bot cannot execute concurrently (serialisation is
     enforced by CloudTaskStore.claim_next, preserved across the async change).
  3. Worker shutdown drains in-flight tasks and leaves the pool/store usable
     (no broken state).

These run with ONE ``TaskWorker`` (one claim loop, one process) — exactly the
deployed topology the blocker described.

Uses an injected fake ``executor`` (a plain callable) controlled by threading
Events, so no network, Rift, or provider keys are needed.
Run: python3 test_worker_concurrency.py
"""

import os
import sys
import tempfile
import threading
import time
import uuid

# Point the persistent store at an isolated directory BEFORE importing the
# modules (paths.DATA_DIR is evaluated at import time).
_TMP = tempfile.mkdtemp(prefix="kyrex_worker_conc_")
os.environ["KYREX_DATA_DIR"] = _TMP
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import task_store as ts


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
        time.sleep(0.02)
    return store.status(task_id)


class _Tracker:
    """Records concurrent execution so we can assert (non-)overlap."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.starts = []

    def on_start(self, task_id):
        with self._lock:
            self.active += 1
            if self.active > self.max_active:
                self.max_active = self.active
            self.starts.append(task_id)

    def on_done(self, task_id):
        with self._lock:
            self.active -= 1


def _make_executor(tracker, release):
    """A fake executor that records start/finish and waits on *release*."""

    def _exec(*, session_key, task_id, on_result, **_kw):
        tracker.on_start(task_id)
        release.wait(timeout=30)
        tracker.on_done(task_id)
        on_result({"status": "done", "task_id": task_id})

    return _exec


# ── 1. Bot A stays running while Bot B is claimed/executed (single worker) ──
def test_single_worker_runs_different_bots_concurrently():
    store = ts.CloudTaskStore(os.path.join(_TMP, "db-diff-" + uuid.uuid4().hex[:8] + ".db"))
    tracker = _Tracker()
    release = threading.Event()
    executor = _make_executor(tracker, release)

    # ONE worker, TWO different-Bot tasks.
    w = ts.TaskWorker(store, worker_id="w-diff-" + uuid.uuid4().hex[:6],
                      executor=executor)
    tA = store.submit(session_key="botA", task_text="do A")
    tB = store.submit(session_key="botB", task_text="do B")
    w.start()

    # Both tasks must be claimed and in-flight at the same time.  With the old
    # synchronous loop only one could start before the other finished.
    _poll(store, tA, {ts.STATUS_RUNNING}, timeout=10)
    _poll(store, tB, {ts.STATUS_RUNNING}, timeout=10)
    check("both different-Bot tasks running in the single worker",
          store.status(tA) == ts.STATUS_RUNNING
          and store.status(tB) == ts.STATUS_RUNNING,
          f"A={store.status(tA)} B={store.status(tB)}")

    # The peak concurrency proves Bot B was claimed/executed while Bot A ran.
    check("Bot A remained running while Bot B was executed (concurrent)",
          tracker.max_active == 2, f"max_active={tracker.max_active}")

    release.set()
    _poll(store, tA, ts.TERMINAL_STATUSES, timeout=15)
    _poll(store, tB, ts.TERMINAL_STATUSES, timeout=15)
    check("botA task done", store.status(tA) == ts.STATUS_DONE)
    check("botB task done", store.status(tB) == ts.STATUS_DONE)

    w.stop()
    store.close()


# ── 2. Two tasks for the same Bot cannot execute concurrently ──────────────
def test_same_bot_not_concurrent():
    store = ts.CloudTaskStore(os.path.join(_TMP, "db-same-" + uuid.uuid4().hex[:8] + ".db"))
    tracker = _Tracker()
    release = threading.Event()
    executor = _make_executor(tracker, release)

    w = ts.TaskWorker(store, worker_id="w-same-" + uuid.uuid4().hex[:6],
                      executor=executor)
    t1 = store.submit(session_key="sameBot", task_text="first")
    t2 = store.submit(session_key="sameBot", task_text="second")
    w.start()

    _poll(store, t1, {ts.STATUS_RUNNING}, timeout=10)
    # While t1 (same session) is in flight, t2 must NOT be claimed/executed.
    time.sleep(0.5)
    check("same-Bot second task stays queued while first runs",
          store.status(t2) == ts.STATUS_QUEUED, store.status(t2))
    check("peak concurrency for same Bot is 1",
          tracker.max_active == 1, f"max_active={tracker.max_active}")

    release.set()
    _poll(store, t1, ts.TERMINAL_STATUSES, timeout=15)
    _poll(store, t2, ts.TERMINAL_STATUSES, timeout=15)
    check("same-Bot tasks serialized to completion",
          store.status(t1) == ts.STATUS_DONE
          and store.status(t2) == ts.STATUS_DONE)
    check("same-Bot tasks never ran concurrently",
          tracker.max_active == 1, f"max_active={tracker.max_active}")
    # t2 must have started only after t1 finished (strict ordering).
    check("same-Bot tasks ran in submission order",
          tracker.starts == [t1, t2], f"starts={tracker.starts}")

    w.stop()
    store.close()


# ── 3. Worker shutdown drains in-flight and leaves pool/store usable ───────
def test_shutdown_drains_in_flight_and_stays_usable():
    store = ts.CloudTaskStore(os.path.join(_TMP, "db-shut-" + uuid.uuid4().hex[:8] + ".db"))
    tracker = _Tracker()
    release = threading.Event()
    executor = _make_executor(tracker, release)

    w = ts.TaskWorker(store, worker_id="w-shut-" + uuid.uuid4().hex[:6],
                      executor=executor)
    t = store.submit(session_key="botC", task_text="long")
    w.start()
    _poll(store, t, {ts.STATUS_RUNNING}, timeout=10)
    check("task claimed and running before shutdown",
          store.status(t) == ts.STATUS_RUNNING)

    # Stop in a background thread so we can release the in-flight task and let
    # the graceful drain finish without deadlocking this test.
    stopper = threading.Thread(target=w.stop)
    stopper.start()
    release.set()
    stopper.join(timeout=15)

    check("stop() returned (graceful drain, no deadlock)",
          not stopper.is_alive(), "stopper still running")
    check("claim loop thread stopped", not w.is_alive())
    check("execution pool cleanly shut down", w._pool_stopped)
    check("in-flight task completed during drain",
          store.status(t) == ts.STATUS_DONE, store.status(t))

    # Store + synchronous execution path (claim_and_execute_once) still usable.
    t2 = store.submit(session_key="botD", task_text="after shutdown")
    ok = w.claim_and_execute_once(timeout=10)
    check("store usable after shutdown (sync path)", ok is True)
    check("post-shutdown task completed",
          store.status(t2) == ts.STATUS_DONE, store.status(t2))

    w.stop()  # idempotent
    store.close()


if __name__ == "__main__":
    # Optional argv[1] filter lets each test run in a short-lived invocation
    # (useful under tight command timeouts): e.g.
    #   python3 test_worker_concurrency.py diff
    #   python3 test_worker_concurrency.py same
    #   python3 test_worker_concurrency.py shutdown
    only = sys.argv[1] if len(sys.argv) > 1 else None

    if only is None or only == "diff":
        print("1. single worker runs different bots concurrently")
        test_single_worker_runs_different_bots_concurrently()
    if only is None or only == "same":
        print("2. same-bot tasks not concurrent")
        test_same_bot_not_concurrent()
    if only is None or only == "shutdown":
        print("3. shutdown drains in-flight and stays usable")
        test_shutdown_drains_in_flight_and_stays_usable()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("ALL FOCUSED CONCURRENCY TESTS PASSED")
