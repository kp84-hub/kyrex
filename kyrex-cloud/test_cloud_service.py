"""Integration tests for the Milestone 1 Cloud service wiring.

Proves the #61 foundation (CloudTaskStore + TaskWorker + serve.run_task hooks)
is actually wired into the live service:

  * worker startup (CloudTaskStore + TaskWorker start, register, recover)
  * Cloud API submission / status / result / list / events
  * Telegram submit path persists callbacks (approval + result) to the store
  * approval persistence + API approval-response routing
  * restart/recovery (orphaned tasks reclaimed; queued tasks still discoverable)
  * different-Bot concurrency (different sessions run at once)
  * same-Bot serialization (one session's tasks run one at a time)

Uses tiny fake executors so no network, Rift, or provider keys are needed.
Run: python3 test_cloud_service.py
"""

import os
import sys
import tempfile
import threading
import time
import uuid

# Point the persistent store at an isolated directory BEFORE importing the
# modules (paths.DATA_DIR is evaluated at import time).
_TMP = tempfile.mkdtemp(prefix="kyrex_cloud_svc_")
os.environ["KYREX_DATA_DIR"] = _TMP

# Env required by telegram_bot / web backend at import time.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")
os.environ.setdefault("GITHUB_CLIENT_ID", "cid")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "csec")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "alloweduser")
os.environ.setdefault("KYREX_TARGET_REPO_URL", "https://github.com/kp84-hub/kyrex.git")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
WEB_BACKEND = os.path.join(_HERE, "web", "backend")
sys.path.insert(0, WEB_BACKEND)

import task_store as ts
import serve
import telegram_bot as tb

FAKE_AUTO = os.path.join(_TMP, "fake_auto.py")
FAKE_APPROVAL = os.path.join(_TMP, "fake_approval.py")
SLOW_A = os.path.join(_TMP, "slow_a.py")
SLOW_B = os.path.join(_TMP, "slow_b.py")

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

with open(SLOW_A, "w") as f:
    f.write(
        "import sys, os, time, tempfile\n"
        "open(os.path.join(tempfile.gettempdir(), 'marker_A'), 'w').close()\n"
        "time.sleep(1.2)\n"
        "print('KYREX_RESULT_JSON: ' + '{\"status\": \"done\", \"branch\": \"A\"}')\n"
        "sys.stdout.flush()\n"
    )

with open(SLOW_B, "w") as f:
    f.write(
        "import sys, os, time, tempfile\n"
        "open(os.path.join(tempfile.gettempdir(), 'marker_B'), 'w').close()\n"
        "time.sleep(1.2)\n"
        "print('KYREX_RESULT_JSON: ' + '{\"status\": \"done\", \"branch\": \"B\"}')\n"
        "sys.stdout.flush()\n"
    )

serve.EXECUTORS["fake"] = FAKE_AUTO
serve.EXECUTORS["fake_approval"] = FAKE_APPROVAL
serve.EXECUTORS["slowA"] = SLOW_A
serve.EXECUTORS["slowB"] = SLOW_B

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


def _new_store():
    """A CloudTaskStore on its own DB file, so tests don't share state."""
    return ts.CloudTaskStore(
        os.path.join(_TMP, "db-" + uuid.uuid4().hex[:10] + ".db")
    )


def _clean_markers():
    for m in ("marker_A", "marker_B"):
        p = os.path.join(tempfile.gettempdir(), m)
        if os.path.exists(p):
            os.remove(p)


# ── 1. Worker startup ─────────────────────────────────────────────
def test_worker_startup():
    store = _new_store()
    wid = "w-startup-" + uuid.uuid4().hex[:6]
    w = ts.TaskWorker(store, worker_id=wid)
    check("worker not alive before start", not w.is_alive())
    w.start()
    time.sleep(0.4)
    check("worker alive after start", w.is_alive())
    check("worker registered in store", wid in store.live_workers())
    w.stop()
    store.close()


# ── 2. Cloud API submission / status / result / list / events ─────
def test_api_submission_status_result():
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:
        print(f"  SKIP  api submission (fastapi unavailable: {exc})")
        return
    store = _new_store()
    import main as webmain
    webmain._store = store
    webmain.sessions["tok"] = "alloweduser"
    client = TestClient(webmain.app)

    r = client.post("/api/task", json={"task": "fake: do the thing"},
                    cookies={"session": "tok"})
    check("POST /api/task -> 200", r.status_code == 200, r.text)
    if r.status_code != 200:
        return
    tid = r.json()["task_id"]
    check("POST returns task_id", bool(tid))

    # Execute via the worker (the only execution path).
    w = ts.TaskWorker(store, worker_id="w-api-" + uuid.uuid4().hex[:6])
    ok = w.claim_and_execute_once(timeout=10)
    check("worker executed the queued API task", ok is True)

    r2 = client.get(f"/api/task/{tid}", cookies={"session": "tok"})
    check("GET /api/task -> 200", r2.status_code == 200, r2.text)
    body = r2.json()
    check("task status is done", body["status"] == "done", body)
    check("task result branch captured", body.get("result", {}).get("branch") == "fake-branch", body)

    r3 = client.get("/api/tasks", cookies={"session": "tok"})
    check("GET /api/tasks -> 200", r3.status_code == 200)
    check("list includes submitted task",
          any(t["task_id"] == tid for t in r3.json()["tasks"]))

    r4 = client.get(f"/api/task/{tid}/events", cookies={"session": "tok"})
    check("GET /api/task events -> 200", r4.status_code == 200)
    check("event stream has lifecycle events",
          len(r4.json()["events"]) >= 2)

    w.stop()
    store.close()


# ── 3. Telegram submit path persists callbacks ───────────────────
def test_telegram_callback_persistence():
    store = _new_store()
    tb._store = store  # telegram_bot.launch() queues into this store
    tid = tb.launch(chat_id=12345, repo_url="https://x/y.git",
                    task_text="fake_approval: approve me",
                    executor_prefix="fake_approval")
    check("telegram launch returns task_id", bool(tid))
    check("telegram task is queued in store", store.status(tid) == ts.STATUS_QUEUED)

    w = ts.TaskWorker(store, worker_id="w-tg-" + uuid.uuid4().hex[:6])
    th = threading.Thread(target=w.claim_and_execute_once, kwargs={"timeout": 10})
    th.start()

    st = _poll(store, tid, {ts.STATUS_AWAITING_APPROVAL}, timeout=10)
    check("telegram task reaches awaiting_approval", st == ts.STATUS_AWAITING_APPROVAL, st)

    pending = store.get_pending_approval(tid)
    check("approval persisted to store (task_id-linked)", pending is not None)
    if pending:
        check("approval session_key is the telegram chat", pending["session_key"] == "12345")
        check("approval decision pending", pending["decision"] == "pending")

    # Operator replies via the restart-safe entry point.
    check("store.respond routes the reply", store.respond(tid, "y") is True)

    st = _poll(store, tid, ts.TERMINAL_STATUSES, timeout=15)
    check("telegram task completes after approval", st == ts.STATUS_DONE, st)
    check("telegram task result captured", store.get(tid)["result"]["decision"] == "APPROVED")

    th.join()
    w.stop()
    store.close()


# ── 4. Approval persistence + API approval response ──────────────
def test_api_approval_response():
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:
        print(f"  SKIP  api approval (fastapi unavailable: {exc})")
        return
    store = _new_store()
    import main as webmain
    webmain._store = store
    webmain.sessions["tok"] = "alloweduser"
    client = TestClient(webmain.app)

    r = client.post("/api/task", json={"task": "fake_approval: approve me"},
                    cookies={"session": "tok"})
    tid = r.json()["task_id"]

    w = ts.TaskWorker(store, worker_id="w-appr-" + uuid.uuid4().hex[:6])
    th = threading.Thread(target=w.claim_and_execute_once, kwargs={"timeout": 10})
    th.start()

    _poll(store, tid, {ts.STATUS_AWAITING_APPROVAL}, timeout=10)
    check("approval pending before API response", store.get_pending_approval(tid) is not None)

    r2 = client.post(f"/api/task/{tid}/respond", json={"decision": "y"},
                     cookies={"session": "tok"})
    check("POST /api/task respond -> 200", r2.status_code == 200, r2.text)
    check("respond reports success", r2.json().get("responded") is True)

    st = _poll(store, tid, ts.TERMINAL_STATUSES, timeout=15)
    check("task done after API approval", st == ts.STATUS_DONE, st)
    check("approval resolved (none pending)", store.get_pending_approval(tid) is None)

    th.join()
    w.stop()
    store.close()


# ── 5. Restart / recovery ────────────────────────────────────────
def test_restart_recovery():
    db = os.path.join(_TMP, "restart.db")
    store = ts.CloudTaskStore(db)
    tid = store.submit(session_key="s-restart", task_text="x",
                        executor_prefix="fake")
    # Simulate a dead worker having claimed + started the task.
    store.claim_next("dead-worker")
    store.set_status(tid, ts.STATUS_RUNNING)
    store.close()

    # "Restart": a fresh store + worker process (same DB).
    store2 = ts.CloudTaskStore(db)
    wid = "w-restart-" + uuid.uuid4().hex[:6]
    w = ts.TaskWorker(store2, worker_id=wid)
    recovered = store2.recover_stale(live_worker_ids={wid})
    check("orphaned running task recovered on restart",
          any(r["task_id"] == tid for r in recovered))
    check("recovered task marked failed", store2.status(tid) == ts.STATUS_FAILED)

    # A queued task is still discoverable and executable after restart.
    tid2 = store2.submit(session_key="s-restart2", task_text="y",
                         executor_prefix="fake")
    check("queued task discoverable after restart",
          store2.status(tid2) == ts.STATUS_QUEUED)
    ok = w.claim_and_execute_once(timeout=10)
    check("worker executes discoverable task", ok is True)
    check("restarted task completed", store2.status(tid2) == ts.STATUS_DONE)

    w.stop()
    store2.close()


# ── 6. Different-Bot concurrency ─────────────────────────────────
def test_different_bot_concurrency():
    _clean_markers()
    store = _new_store()
    tA = store.submit(session_key="botA", task_text="a", executor_prefix="slowA")
    tB = store.submit(session_key="botB", task_text="b", executor_prefix="slowB")

    wA = ts.TaskWorker(store, worker_id="wA-" + uuid.uuid4().hex[:6])
    wB = ts.TaskWorker(store, worker_id="wB-" + uuid.uuid4().hex[:6])
    wA.start()
    wB.start()

    # Both different-Bot tasks should be running at the same time.
    _poll(store, tA, {ts.STATUS_RUNNING}, timeout=10)
    _poll(store, tB, {ts.STATUS_RUNNING}, timeout=10)
    markerA = os.path.join(tempfile.gettempdir(), "marker_A")
    markerB = os.path.join(tempfile.gettempdir(), "marker_B")
    # Both executors write their marker immediately then sleep 1.2s, so both
    # markers existing together proves concurrent execution.
    check("different bots run concurrently (both started)",
          os.path.exists(markerA) and os.path.exists(markerB),
          f"markerA={os.path.exists(markerA)} markerB={os.path.exists(markerB)}")

    _poll(store, tA, ts.TERMINAL_STATUSES, timeout=15)
    _poll(store, tB, ts.TERMINAL_STATUSES, timeout=15)
    check("botA task done", store.status(tA) == ts.STATUS_DONE)
    check("botB task done", store.status(tB) == ts.STATUS_DONE)

    # Never allow the same task claimed twice.
    wA.stop()
    wB.stop()
    _clean_markers()
    store.close()


# ── 7. Same-Bot serialization ────────────────────────────────────
def test_same_bot_serialization():
    _clean_markers()
    store = _new_store()
    t1 = store.submit(session_key="sameBot", task_text="1", executor_prefix="slowA")
    t2 = store.submit(session_key="sameBot", task_text="2", executor_prefix="slowB")

    w = ts.TaskWorker(store, worker_id="w-same-" + uuid.uuid4().hex[:6])
    w.start()

    _poll(store, t1, {ts.STATUS_RUNNING}, timeout=10)
    # While t1 (same session) is running, t2 must remain queued.
    time.sleep(0.4)
    check("same-bot second task stays queued while first runs",
          store.status(t2) == ts.STATUS_QUEUED, store.status(t2))

    _poll(store, t1, ts.TERMINAL_STATUSES, timeout=15)
    _poll(store, t2, ts.TERMINAL_STATUSES, timeout=15)
    check("same-bot tasks serialized to completion",
          store.status(t1) == ts.STATUS_DONE and store.status(t2) == ts.STATUS_DONE)

    w.stop()
    _clean_markers()
    store.close()


if __name__ == "__main__":
    print("worker startup")
    test_worker_startup()
    print("api submission / status / result")
    test_api_submission_status_result()
    print("telegram callback persistence")
    test_telegram_callback_persistence()
    print("api approval response")
    test_api_approval_response()
    print("restart / recovery")
    test_restart_recovery()
    print("different-bot concurrency")
    test_different_bot_concurrency()
    print("same-bot serialization")
    test_same_bot_serialization()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("ALL CLOUD SERVICE TESTS PASSED")
