import os
import sys
import tempfile
import threading
import time
import uuid

_TMP = tempfile.mkdtemp(prefix="kyrex_dsame_")
os.environ["KYREX_DATA_DIR"] = _TMP
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import task_store as ts

def log(m):
    print(m, flush=True)

tracker = type("T", (), {})()
tracker.lock = threading.Lock()
tracker.active = 0
tracker.max_active = 0
tracker.starts = []

release = threading.Event()

def _exec(*, session_key, task_id, on_result, **_kw):
    log(f"  EXEC START {task_id}")
    with tracker.lock:
        tracker.active += 1
        tracker.max_active = max(tracker.max_active, tracker.active)
        tracker.starts.append(task_id)
    release.wait(timeout=30)
    with tracker.lock:
        tracker.active -= 1
    log(f"  EXEC END {task_id}")
    on_result({"status": "done", "task_id": task_id})

try:
    store = ts.CloudTaskStore(os.path.join(_TMP, "ds.db"))
    log("store created")
    w = ts.TaskWorker(store, worker_id="w-ds", executor=_exec)
    t1 = store.submit(session_key="sameBot", task_text="first")
    t2 = store.submit(session_key="sameBot", task_text="second")
    log(f"submitted t1={t1} t2={t2}")
    w.start()
    log("started; loop alive=" + str(w.is_alive()))
    for _ in range(100):
        if store.status(t1) == ts.STATUS_RUNNING:
            break
        time.sleep(0.02)
    log("t1 status=" + str(store.status(t1)))
    time.sleep(0.5)
    log("t2 status after 0.5s=" + str(store.status(t2)))
    log("max_active=" + str(tracker.max_active))
    log("calling release.set()")
    release.set()
    log("release set; waiting for t1 terminal")
    for _ in range(300):
        if store.status(t1) in ts.TERMINAL_STATUSES:
            break
        time.sleep(0.05)
    log("t1 status=" + str(store.status(t1)))
    for _ in range(300):
        if store.status(t2) in ts.TERMINAL_STATUSES:
            break
        time.sleep(0.05)
    log("t2 status=" + str(store.status(t2)))
    log("starts=" + str(tracker.starts))
    log("calling stop")
    w.stop()
    log("stopped")
    store.close()
    log("DONE")
except Exception as e:
    import traceback
    log("EXCEPTION: " + repr(e))
    traceback.print_exc()
