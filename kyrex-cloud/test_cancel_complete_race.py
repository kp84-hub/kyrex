#!/usr/bin/env python3
"""Cancel → complete race — reproduction + regression.

Reported finding (Medium): finalization ordering is

    result_captured → is_cancel_requested

so a cancellation requested while a task is finishing can be durably
accepted (request_cancel returns True, cancel_requested=1) and then silently
lost: finalization marks the task ``done``, the cancel flag stays set
forever, no ``cancelled`` event is emitted, and the API/durable/flux views
disagree about what happened.

Production call path exercised:

    web/backend/main.py POST /api/task/{id}/cancel
      -> CloudTaskStore.request_cancel   (accepts; sets cancel_requested=1)
    TaskWorker.execute_task finalization  (cancelled_via_approval ->
       result_captured -> is_cancel_requested -> fail)

Tests
-----
A. Cancel a queued task -> stays cancelled, never executes.
B. Cancel a running task before completion -> the cancellation WINS even
   when the executor later completes with a result (no approval gate):
   final state is cancelled, never done-with-cancel-flag.
C. Cancel arrives immediately before the result is finalised -> the
   cancellation is deterministically honoured: request_cancel was accepted
   while the task was running, so finalisation must yield cancelled, a
   "cancelled" event, and no "done" result.  This is the regression test
   for the race (result captured -> cancel accepted -> finalise).
D. Cancel arrives after successful completion -> consistent, and why.
E. Normal completion (no cancellation requested) still yields done, and an
   approval-gated cancellation still lands on cancelled.

Run: python3 test_cancel_complete_race.py
"""
import os
import sys
import tempfile
import uuid

_TMP = tempfile.mkdtemp(prefix="kyrex_cancel_race_")
os.environ["KYREX_DATA_DIR"] = _TMP
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import task_store as ts  # noqa: E402
import serve  # noqa: E402  — approval gate / run_task used by the worker

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


print("\n=== Area 2: cancel → complete race ===")

# ── A. queued cancel is honoured and never executes ─────────────────────
print("\nA. cancel a QUEUED task → cancelled, never executed")
store = ts.CloudTaskStore()
tid = store.submit(session_key="sess-q", task_text="queued task")
accepted = store.request_cancel(tid)
check("A: queued cancel accepted", accepted is True)
check("A: status is cancelled immediately", store.status(tid) == ts.STATUS_CANCELLED,
      f"got {store.status(tid)}")
claimed = store.claim_next("w-inspec")
check("A: worker cannot claim a cancelled task", claimed is None,
      f"got {claimed}")
check("A: cancel flag consistent on terminal row",
      store.get(tid)["cancel_requested"] is True)
store.close()

# ── C. cancel lands after result capture, before finalisation ───────────
print("\nC. cancel arrives after result captured, before finalisation")
store = ts.CloudTaskStore()
tid = store.submit(session_key="sess-c", task_text="race me")

def executor_race(**kwards):
    on_result = kwards["on_result"]
    # The executor finishes: result is captured...
    on_result({"status": "done", "final_response": "work done"})
    # ... and exactly then the operator's cancel is accepted by the API.
    accepted = store.request_cancel(tid)
    assert accepted is True, "cancel must be accepted while task is running"
    return

worker = ts.TaskWorker(store, worker_id="w-" + uuid.uuid4().hex[:6],
                       executor=executor_race)
claimed = store.claim_next(worker.worker_id)
check("C: task claimed and running", claimed and claimed["task_id"] == tid)
worker.execute_task(claimed)
final = store.get(tid)
check("C: FINAL STATUS is cancelled (cancel wins, not lost)",
      final["status"] == ts.STATUS_CANCELLED,
      f"got {final['status']}")
check("C: cancel flag consistent on the cancelled row",
      final["cancel_requested"] is True,
      f"got {final['cancel_requested']}")
check("C: captured result NOT persisted as a completion",
      final["result"] is None,
      f"got {final['result']}")
types = [e["type"] for e in store.get_events(tid)]
check("C: 'cancelled' event present in the stream", "cancelled" in types,
      f"types={types}")
check("C: 'cancel_requested' event present", "cancel_requested" in types,
      f"types={types}")
status_events = [e for e in store.get_events(tid) if e["type"] == "status"]
check("C: no 'done' status event was emitted",
      not any(e["payload"].get("status") == ts.STATUS_DONE
              for e in status_events),
      f"status_events={status_events}")
check("C: lifecycle state matches cancellation semantics",
      (final["status"] == ts.STATUS_CANCELLED
       and final["cancel_requested"]
       and "cancel_requested" in types and "cancelled" in types
       and not any(e["payload"].get("status") == ts.STATUS_DONE
                   for e in status_events)))
store.close()

# ── B. cancel mid-run, executor completes anyway (no approval gate) ──────
print("\nB. cancel running task, executor keeps going and completes")
store = ts.CloudTaskStore()
tid = store.submit(session_key="sess-b", task_text="keep going")

def executor_finish(**kwards):
    on_result = kwards["on_result"]
    # the cancel request lands mid-run...
    assert store.request_cancel(tid) is True
    # ... but there is no approval gate, so the executor runs to the end.
    on_result({"status": "done", "final_response": "finished anyway"})
    return

worker = ts.TaskWorker(store, worker_id="w-" + uuid.uuid4().hex[:6],
                       executor=executor_finish)
claimed = store.claim_next(worker.worker_id)
worker.execute_task(claimed)
f = store.get(tid)
check("B: mid-run cancel WINS over the completed result (cancelled)",
      f["status"] == ts.STATUS_CANCELLED, f"got {f['status']}")
check("B: cancel_requested consistent on the cancelled row",
      f["cancel_requested"] is True, f"got {f['cancel_requested']}")
check("B: result not persisted as a completion", f["result"] is None,
      f"got {f['result']}")
bt = [e["type"] for e in store.get_events(tid)]
check("B: 'cancelled' event in the stream", "cancelled" in bt, f"types={bt}")
store.close()

# ── D. cancel after completion is refused ────────────────────────────────
print("\nD. cancel AFTER successful completion")
store = ts.CloudTaskStore()
tid = store.submit(session_key="sess-d", task_text="done already")
store.complete(tid, {"status": "done", "final_response": "ok"})
check("D: task is done", store.status(tid) == ts.STATUS_DONE)
accepted = store.request_cancel(tid)
check("D: late cancel is refused (terminal)", accepted is False)
check("D: status unchanged (done)", store.status(tid) == ts.STATUS_DONE)
store.close()

# ── E. normal completion + approval-gated cancellation ────────────────────
print("\nE. no cancellation -> done; approval-gated cancellation -> cancelled")
store = ts.CloudTaskStore()

def executor_normal(**kwards):
    kwards["on_result"]({"status": "done", "final_response": "ok"})

t_e1 = store.submit(session_key="sess-e1", task_text="normal run")
w1 = ts.TaskWorker(store, worker_id="w-" + uuid.uuid4().hex[:6],
                   executor=executor_normal)
c1 = store.claim_next(w1.worker_id)
check("E: normal task claimed", c1 and c1["task_id"] == t_e1)
w1.execute_task(c1)
e1 = store.get(t_e1)
check("E: no-cancel run completes as done", e1["status"] == ts.STATUS_DONE,
      f"got {e1['status']}")
check("E: result persisted", e1["result"] is not None)
check("E: cancel flag clear on the done task",
      e1["cancel_requested"] is False)
e1t = [e["type"] for e in store.get_events(t_e1)]
check("E: no cancelled event for a normal run", "cancelled" not in e1t,
      f"types={e1t}")

# Approval-gated cancellation: cancel lands while running, BEFORE the
# executor raises its approval; the worker's approval gate auto-denies and
# the task must finalise as cancelled (existing approval-cancel behaviour).
FAKE_APPROVAL = os.path.join(_TMP, "fake_approval_race.py")
with open(FAKE_APPROVAL, "w") as f:
    f.write(
        "import sys, json, time\n"
        "sys.stdout.write('KYREX_APPROVAL:' + json.dumps(\n"
        "    {'tier': 1, 'summary': 'approve me', 'token': '', 'detail': ''}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.15)\n"
        "dec = ''\n"
        "try:\n"
        "    dec = sys.stdin.readline()\n"
        "except Exception:\n"
        "    dec = ''\n"
        "sys.stdout.write('KYREX_RESULT_JSON:' + json.dumps(\n"
        "    {'status': 'done', 'decision': dec.strip()}) + '\\n')\n"
        "sys.stdout.flush()\n"
    )
serve.EXECUTORS["fake_approval_race"] = FAKE_APPROVAL

t_e2 = store.submit(session_key="sess-e2", task_text="approve then cancel",
                    executor_prefix="fake_approval_race")
w2 = ts.TaskWorker(store, worker_id="w-" + uuid.uuid4().hex[:6])
c2 = store.claim_next(w2.worker_id)
check("E: approval task claimed", c2 and c2["task_id"] == t_e2)
# Operator requests cancellation while the (claimed, running) task has not
# yet raised its approval gate.
accepted = store.request_cancel(t_e2)
check("E: cancel accepted while running (pre-gate)", accepted is True)
w2.execute_task(c2)
e2 = store.get(t_e2)
check("E: approval-gated cancellation finalises as cancelled",
      e2["status"] == ts.STATUS_CANCELLED, f"got {e2['status']}")
e2t = [e["type"] for e in store.get_events(t_e2)]
check("E: 'cancelled' event for approval-gated cancel", "cancelled" in e2t,
      f"types={e2t}")
store.close()

print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)