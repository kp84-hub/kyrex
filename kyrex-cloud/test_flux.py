#!/usr/bin/env python3
"""Tests for flux.py — cursor-based event streaming over CloudTaskStore.

Covers: full replay of a finished task, cursor resume, live tailing while
events are appended, explicit end event on terminal state, unknown task,
cancelled task, max_seconds guard, and SSE formatting (id/event/data
frames, cursor-free synthetic events).

Run: python3 test_flux.py
"""
import json
import os
import sys
import tempfile
import threading
import time

# Point the persistent store at an isolated directory BEFORE importing the
# modules (paths.DATA_DIR is evaluated at import time).
_TMP = tempfile.mkdtemp(prefix="kyrex_flux_")
os.environ["KYREX_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flux  # noqa: E402
from task_store import CloudTaskStore  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


store = CloudTaskStore()  # DB lives under the temp KYREX_DATA_DIR


def collect(gen):
    """Drain a flux generator into (events, end_event_or_None)."""
    events = []
    end = None
    for e in gen:
        if e["type"] == "end":
            end = e
            break
        events.append(e)
    return events, end


# --- Test 1: full replay of a finished task -------------------------------
print("\nTest 1: replay — finished task streams its whole history then ends")

tid = store.submit(session_key="flux-op", task_text="replay me")
store.add_event(tid, "progress", {"tool": "fake", "step": 1})
store.add_event(tid, "progress", {"tool": "fake", "step": 2})
# Mirror the real worker flow: on_result_cb appends the "result" event,
# then complete() flips status (which appends the "status" event last).
store.add_event(tid, "result", {"status": "done", "final_response": "ok"})
store.complete(tid, {"status": "done", "final_response": "ok"})

events, end = collect(flux.stream_events(store, tid, poll_interval=0.01,
                                         max_seconds=5))
types = [e["type"] for e in events]
check("all history replayed in order",
      types == ["submitted", "progress", "progress", "result", "status"],
      f"types={types}")
check("end event present", end is not None)
check("end event carries final status",
      end and end["payload"].get("status") == "done",
      f"end={end}")
check("end event is synthetic (no cursor id)",
      end and end["event_id"] is None)
ids = [e["event_id"] for e in events]
check("event ids strictly increasing", ids == sorted(ids) and len(set(ids)) == len(ids),
      f"ids={ids}")

# --- Test 2: cursor resume — only events after the cursor ------------------
print("\nTest 2: resume — after_event_id skips already-seen events")

mid = ids[1]  # first progress event
events2, end2 = collect(flux.stream_events(store, tid, after_event_id=mid,
                                           poll_interval=0.01, max_seconds=5))
types2 = [e["type"] for e in events2]
check("resume yields only later events",
      types2 == ["progress", "result", "status"],
      f"types={types2}")
all_ids = [e["event_id"] for e in events2]
check("resumed ids all after cursor", all(i > mid for i in all_ids),
      f"cursor={mid} ids={all_ids}")

# --- Test 3: live tailing — events appended mid-stream are delivered -------
print("\nTest 3: live tail — events appended while streaming are delivered")

tid2 = store.submit(session_key="flux-op", task_text="tail me")
collected = []
end_box = []
done = threading.Event()


def tail():
    for e in flux.stream_events(store, tid2, poll_interval=0.05,
                                max_seconds=20):
        if e["type"] == "end":
            end_box.append(e)
            done.set()
            break
        collected.append(e)


thread = threading.Thread(target=tail, daemon=True)
thread.start()
time.sleep(0.2)  # let the stream attach and replay the submitted event
store.add_event(tid2, "progress", {"step": "live-1"})
time.sleep(0.3)
store.add_event(tid2, "progress", {"step": "live-2"})
store.complete(tid2, {"status": "done"})
check("stream ended", done.wait(timeout=10))

tail_types = [e["type"] for e in collected]
check("live events delivered",
      "progress" in tail_types and tail_types.count("progress") >= 2,
      f"types={tail_types}")
check("end arrived last with final status",
      end_box and end_box[0]["payload"].get("status") == "done",
      f"end_box={end_box}")

# --- Test 4: unknown task → error event, stream stops ----------------------
print("\nTest 4: unknown task id yields a single error event")

events4 = list(flux.stream_events(store, "task-does-not-exist",
                                  poll_interval=0.01, max_seconds=2))
check("exactly one event for unknown task", len(events4) == 1,
      f"events={events4}")
check("error is unknown_task",
      events4 and events4[0]["type"] == "error"
      and events4[0]["payload"].get("error") == "unknown_task",
      f"events={events4}")

# --- Test 5: cancelled task ends with cancelled status ---------------------
print("\nTest 5: cancelled task streams to an end with status cancelled")

tid3 = store.submit(session_key="flux-op", task_text="cancel me")
store.add_event(tid3, "progress", {"step": "starting"})
store.request_cancel(tid3)
events5, end5 = collect(flux.stream_events(store, tid3, poll_interval=0.01,
                                           max_seconds=5))
check("cancel flow streamed", any(e["type"] == "cancelled" for e in events5),
      f"types={[e['type'] for e in events5]}")
check("end carries cancelled status",
      end5 and end5["payload"].get("status") == "cancelled",
      f"end={end5}")

# --- Test 6: max_seconds guard on a never-terminal task ---------------------
print("\nTest 6: max_seconds guard ends a stuck stream with a reason")

tid4 = store.submit(session_key="flux-op", task_text="hangs forever")
store.set_status(tid4, "running")  # never completes
events6, end6 = collect(flux.stream_events(store, tid4, poll_interval=0.05,
                                           max_seconds=0.3))
check("stuck stream ends", end6 is not None, f"events={len(events6)}")
check("end reason is max_seconds_exceeded",
      end6 and end6["payload"].get("reason") == "max_seconds_exceeded",
      f"end={end6}")
check("end reports status observed at expiry",
      end6 and end6["payload"].get("status") == "running",
      f"end={end6}")

# --- Test 7: SSE formatting ------------------------------------------------
print("\nTest 7: format_sse produces valid id/event/data frames")

real_event = store.get_events(tid, after_event_id=0)[0]
frame = flux.format_sse(real_event)
check("frame starts with id line", frame.startswith(f"id: {real_event['event_id']}\n"),
      f"frame={frame!r}")
check("frame names the event type", f"event: {real_event['type']}\n" in frame,
      f"frame={frame!r}")
check("frame carries json data", "\ndata: " in frame and frame.endswith("\n\n"),
      f"frame={frame!r}")
data_line = [l for l in frame.splitlines() if l.startswith("data: ")][0]
check("data payload round-trips",
      json.loads(data_line[len("data: "):]) == real_event["payload"],
      f"data={data_line!r}")

synthetic = {"event_id": None, "type": "end",
             "payload": {"status": "done"}, "created_at": "now"}
frame2 = flux.format_sse(synthetic)
check("synthetic frame has no id line", not frame2.startswith("id:"),
      f"frame={frame2!r}")
check("synthetic frame ends the stream visibly",
      "event: end\n" in frame2 and frame2.endswith("\n\n"),
      f"frame={frame2!r}")

# --- Test 8: latest_event_id ------------------------------------------------
print("\nTest 8: latest_event_id returns the newest id (0 when none)")

check("latest id matches last replayed event",
      flux.latest_event_id(store, tid) == ids[-1],
      f"latest={flux.latest_event_id(store, tid)} last={ids[-1]}")
check("no events → 0", flux.latest_event_id(store, "task-none") == 0)

print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
