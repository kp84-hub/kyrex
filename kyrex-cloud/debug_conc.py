import os
import sys
import tempfile
import threading
import time
import uuid

_TMP = tempfile.mkdtemp(prefix="kyrex_dbg_")
os.environ["KYREX_DATA_DIR"] = _TMP
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

print("importing task_store...", flush=True)
import task_store as ts
print("imported task_store OK", flush=True)

tracker = type("T", (), {})()
tracker.lock = threading.Lock()
tracker.active = 0
tracker.max_active = 0

release = threading.Event()

def _exec(*, session_key, task_id, on_result, **_kw):
    print(f"  EXEC START {task_id} session={session_key}", flush=True)
    with tracker.lock:
        tracker.active += 1
        tracker.max_active = max(tracker.max_active, tracker.active)
    release.wait(timeout=30)
    with tracker.lock:
        tracker.active -= 1
    print(f"  EXEC DONE {task_id}", flush=True)
    on_result({"status": "done", "task_id": task_id})

store = ts.CloudTaskStore(os.path.join(_TMP, "dbg.db"))
print("store created", flush=True)
w = ts.TaskWorker(store, worker_id="wdbg", executor=_exec)
tA = store.submit(session_key="botA", task_text="a")
tB = store.submit(session_key="botB", task_text="b")
print(f"submitted tA={tA} tB={tB}", flush=True)
w.start()
print("worker started; loop alive=", w.is_alive(), flush=True)
time.sleep(1.0)
print("after 1s: tA status=", store.status(tA), "tB status=", store.status(tB),
      "max_active=", tracker.max_active, flush=True)
release.set()
time.sleep(1.0)
print("after release: tA=", store.status(tA), "tB=", store.status(tB), flush=True)
w.stop()
print("stopped", flush=True)
store.close()
print("DONE", flush=True)
