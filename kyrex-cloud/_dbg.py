import os
os.environ["KYREX_DATA_DIR"] = "/tmp/ts_dbg2"
import task_store as ts
import serve

tmp = "/tmp/ts_dbg2"
fa = tmp + "/fake_auto.py"
with open(fa, "w") as f:
    f.write(
        "import sys, json, time\n"
        "print('KYREX_PROGRESS: ' + json.dumps({'tool': 'fake', 'step': 'working'}))\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.15)\n"
        "print('KYREX_RESULT_JSON: ' + json.dumps({'status': 'done', 'branch': 'fake-branch'}))\n"
        "sys.stdout.flush()\n"
    )
serve.EXECUTORS["fake"] = fa
s = ts.CloudTaskStore()
w = ts.TaskWorker(s, worker_id="w1")
tid = s.submit(session_key="s", task_text="do it", repo_url="https://x/y.git", executor_prefix="fake")
print("before:", s.status(tid))
w.claim_and_execute_once(timeout=5.0)
t = s.get(tid)
print("status:", t["status"])
print("error:", t["error"])
print("events:", [(e["type"], e["payload"]) for e in s.get_events(tid)])
s.close()
