#!/usr/bin/env python3
"""Phase 3 tests: Flux SSE + cancel + approval for the web trigger.

Two layers:

  1. Store-level contracts the web endpoints glue together (always run —
     needs no FastAPI): the durable approval lifecycle (approval_requested /
     approval_resolved events + awaiting_approval transitions), approval
     replies routed through store.respond() into serve.handle_approval_reply,
     and the cancel semantics backing POST /api/task/{id}/cancel.

  2. HTTP-layer tests through the FastAPI app (fastapi.testclient), skipped
     with a clear SKIP line when fastapi/httpx are not installed — the web
     service installs its own dependencies (web/README.md); the cloud worker
     image does not carry them.

Run: python3 test_web_flux.py
"""
import json
import os
import sys
import tempfile
import threading

# Point the persistent store at an isolated directory BEFORE importing the
# modules (paths.DATA_DIR is evaluated at import time).
_TMP = tempfile.mkdtemp(prefix="kyrex_webflux_")
os.environ["KYREX_DATA_DIR"] = _TMP
# main.py reads these at import time.
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "qa-operator")
# Keep any accidental non-terminal stream bounded during tests.
os.environ.setdefault("KYREX_FLUX_STREAM_MAX_SECONDS", "30")

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


# ══════════════════════════════════════════════════════════════════
# Layer 1: store contracts behind the web endpoints
# ══════════════════════════════════════════════════════════════════

store = CloudTaskStore()

# --- 1a. Approval lifecycle surfaces in the durable event stream ----------
print("\n1a: approval lifecycle → approval_requested / approval_resolved events")

tid = store.submit(session_key="qa-operator", task_text="approve flow")
store.set_status(tid, "running")
apr_id = store.persist_approval_request(
    tid, "qa-operator", "webmsg-1", 1, "", "run destructive op", "detail text"
)
check("approval persists with an id", bool(apr_id), f"apr_id={apr_id!r}")
check("status flips to awaiting_approval", store.status(tid) == "awaiting_approval",
      f"status={store.status(tid)!r}")
check("pending approval is discoverable by task",
      store.get_pending_approval(tid) is not None)

types = [e["type"] for e in store.get_events(tid)]
check("approval_requested event appended", "approval_requested" in types, f"types={types}")
req_idx = types.index("approval_requested")
await_idx = types.index("status") if "status" in types else -1
check("awaiting_approval status precedes the approval event",
      0 <= await_idx < req_idx, f"types={types}")

store.resolve_approval_request(tid, "webmsg-1", "APPROVED")
check("status returns to running", store.status(tid) == "running",
      f"status={store.status(tid)!r}")
check("pending approval cleared", store.get_pending_approval(tid) is None)
types = [e["type"] for e in store.get_events(tid)]
check("approval_resolved event appended", "approval_resolved" in types, f"types={types}")
check("resolution follows the request",
      types.index("approval_resolved") > req_idx, f"types={types}")

# --- 1b. store.respond() routes a reply into the serve approval protocol ---
print("\n1b: store.respond() resolves the live serve-side pending entry")

tid2 = store.submit(session_key="qa-operator", task_text="respond flow")
store.set_status(tid2, "running")
store.persist_approval_request(tid2, "qa-operator", "webmsg-2", 1, "", "needs y/n", "d")

import serve  # noqa: E402

evt = threading.Event()
serve.pending_approvals[("qa-operator", "webmsg-2")] = {
    "event": evt, "chat_id": "qa-operator", "tier": 1, "token": "", "result": None,
}
try:
    delivered = store.respond(tid2, "y")
    check("respond reports delivery", delivered is True, f"delivered={delivered!r}")
    check("reply woke the waiting executor", evt.wait(timeout=5))
    entry = serve.pending_approvals.get(("qa-operator", "webmsg-2"))
    check("reply recorded on the pending entry",
          entry is not None and entry.get("result") == "APPROVED",
          f"entry={entry!r}")
    # run_task's on_approval_resolved callback completes the durable half of
    # the protocol (decides the row, flips status, appends the event).
    store.resolve_approval_request(tid2, "webmsg-2", "APPROVED")
    check("durable approval row decided", store.get_pending_approval(tid2) is None)
finally:
    serve.pending_approvals.pop(("qa-operator", "webmsg-2"), None)

# The same events are what a Flux consumer streams for the web UI.
streamed = [e["type"] for e in flux.stream_events(store, tid2, poll_interval=0.01,
                                                  max_seconds=5)
            if e["type"] != "end"]
check("flux stream carries the approval lifecycle",
      "approval_requested" in streamed and "approval_resolved" in streamed,
      f"streamed={streamed}")

# --- 1c. Cancel semantics (backing POST /api/task/{id}/cancel) ------------
print("\n1c: cancel — queued is immediate, running is flagged, terminal refused")

tidq = store.submit(session_key="qa-operator", task_text="cancel me now")
check("queued cancel accepted", store.request_cancel(tidq) is True)
check("queued cancel is immediate", store.status(tidq) == "cancelled",
      f"status={store.status(tidq)!r}")
check("second cancel refused", store.request_cancel(tidq) is False)
check("cancelled event appended",
      any(e["type"] == "cancelled" for e in store.get_events(tidq)))

tidr = store.submit(session_key="qa-operator", task_text="cancel me later")
store.set_status(tidr, "running")
check("running cancel accepted", store.request_cancel(tidr) is True)
check("running task not yet cancelled", store.status(tidr) == "running",
      f"status={store.status(tidr)!r}")
check("cancel flag visible to the worker", store.is_cancel_requested(tidr) is True)
check("cancel_requested event appended",
      any(e["type"] == "cancel_requested" for e in store.get_events(tidr)))

check("terminal task refuses cancel", store.request_cancel(tidq) is False)

# ══════════════════════════════════════════════════════════════════
# Layer 2: HTTP endpoints (fastapi.testclient), skipped without deps
# ══════════════════════════════════════════════════════════════════

WEB_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "web", "backend")


def parse_sse_lines(lines):
    """Fold an SSE line stream into [{'id', 'event', 'data'}] frames."""
    frames, cur = [], {}
    for line in lines:
        if line == "":
            if cur:
                frames.append(cur)
                cur = {}
            continue
        if line.startswith("id: "):
            cur["id"] = int(line[4:])
        elif line.startswith("event: "):
            cur["event"] = line[7:]
        elif line.startswith("data: "):
            cur["data"] = json.loads(line[6:])
    if cur:
        frames.append(cur)
    return frames


print("\n2: HTTP endpoints via fastapi.testclient")
try:
    sys.path.insert(0, WEB_BACKEND)
    import main as web_main  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    HAVE_WEB_DEPS = True
except Exception as exc:  # fastapi / httpx / starlette missing
    HAVE_WEB_DEPS = False
    print(f"  SKIP  web deps unavailable ({exc.__class__.__name__}: {exc}); "
          f"install fastapi+httpx to run the HTTP layer")

if HAVE_WEB_DEPS:
    client = TestClient(web_main.app)
    web_main.sessions["qa-tok"] = "qa-operator"
    web_main.sessions["other-tok"] = "mallory"
    QA = {"cookies": {"session": "qa-tok"}}

    # --- 2a. Auth & ownership ------------------------------------------------
    hidden = web_main.store.submit(session_key="mallory", task_text="not yours")
    r = client.get(f"/api/task/{hidden['task_id']}/events")
    check("events requires auth", r.status_code == 401, f"code={r.status_code}")
    r = client.get(f"/api/task/{hidden['task_id']}/events", **QA)
    check("events refuses another user's task", r.status_code == 404,
          f"code={r.status_code}")
    r = client.get(f"/api/task/{hidden['task_id']}", **QA)
    check("get_task refuses another user's task", r.status_code == 404,
          f"code={r.status_code}")

    # --- 2b. get_task / cancel / respond endpoints ---------------------------
    tidw = web_main.store.submit(session_key="qa-operator", task_text="web task")
    w = tidw["task_id"]
    r = client.get(f"/api/task/{w}", **QA)
    body = r.json()
    check("get_task returns the queued task",
          r.status_code == 200 and body["status"] == "queued"
          and body["task"] == "web task", f"code={r.status_code} body={body}")
    check("get_task exposes task_id",
          body.get("task_id") == w, f"body={body}")

    r = client.post(f"/api/task/{w}/cancel", **QA)
    body = r.json()
    check("cancel endpoint cancels a queued task",
          r.status_code == 200 and body.get("requested") is True
          and body.get("status") == "cancelled", f"code={r.status_code} body={body}")

    r = client.post(f"/api/task/{w}/respond", json={"text": "y"}, **QA)
    body = r.json()
    check("respond with nothing pending reports undelivered",
          r.status_code == 200 and body.get("delivered") is False,
          f"code={r.status_code} body={body}")

    web_main.store.set_status(w, "running")
    web_main.store.persist_approval_request(w, "qa-operator", "webmsg-9", 1, "",
                                            "needs y/n", "d")
    evt9 = threading.Event()
    serve.pending_approvals[("qa-operator", "webmsg-9")] = {
        "event": evt9, "chat_id": "qa-operator", "tier": 1, "token": "",
        "result": None,
    }
    try:
        r = client.post(f"/api/task/{w}/respond", json={"text": "y"}, **QA)
        body = r.json()
        check("respond endpoint resolves the pending approval",
              r.status_code == 200 and body.get("delivered") is True
              and evt9.wait(timeout=5), f"code={r.status_code} body={body}")
    finally:
        serve.pending_approvals.pop(("qa-operator", "webmsg-9"), None)

    # --- 2c. SSE stream: full replay of a finished task ----------------------
    tidf = web_main.store.submit(session_key="qa-operator", task_text="stream me")
    f = tidf["task_id"]
    web_main.store.set_status(f, "running")
    web_main.store.add_event(f, "progress", {"tool": "web", "lines": ["step 1"]})
    web_main.store.persist_approval_request(f, "qa-operator", "webmsg-3", 1, "",
                                            "approve me", "d")
    web_main.store.resolve_approval_request(f, "webmsg-3", "APPROVED")
    web_main.store.add_event(f, "result", {"status": "done", "final_response": "ok"})
    web_main.store.complete(f, {"status": "done"})

    lines = []
    with client.stream("GET", f"/api/task/{f}/events", **QA) as r:
        check("stream is 200 text/event-stream",
              r.status_code == 200
              and r.headers["content-type"].startswith("text/event-stream"),
              f"code={r.status_code} ct={r.headers.get('content-type')!r}")
        for line in r.iter_lines():
            lines.append(line)
    frames = parse_sse_lines(lines)
    kinds = [fr.get("event") for fr in frames]
    check("stream ends with an end frame", kinds and kinds[-1] == "end",
          f"kinds={kinds}")
    check("end frame carries final status",
          frames[-1].get("data", {}).get("status") == "done",
          f"end={frames[-1]}")
    check("end frame has no cursor id (synthetic)",
          "id" not in frames[-1], f"end={frames[-1]}")
    check("full replay includes the approval lifecycle",
          "approval_requested" in kinds and "approval_resolved" in kinds,
          f"kinds={kinds}")
    check("replay is ordered oldest first",
          kinds.index("approval_requested") < kinds.index("approval_resolved"),
          f"kinds={kinds}")
    real = [fr for fr in frames if fr.get("event") != "end"]
    check("real events carry id lines", all("id" in fr for fr in real),
          f"real={real[:3]}")

    # --- 2d. SSE resume: Last-Event-ID skips already-seen events -------------
    cursor = next(fr["id"] for fr in frames if fr.get("event") == "approval_requested")
    lines = []
    with client.stream("GET", f"/api/task/{f}/events",
                       headers={"last-event-id": str(cursor)}, **QA) as r:
        for line in r.iter_lines():
            lines.append(line)
    frames2 = parse_sse_lines(lines)
    kinds2 = [fr.get("event") for fr in frames2]
    seen_ids = [fr["id"] for fr in frames2 if "id" in fr]
    check("resume never replays pre-cursor events",
          all(i > cursor for i in seen_ids), f"cursor={cursor} ids={seen_ids}")
    check("resume skips the approval_requested frame",
          "approval_requested" not in kinds2, f"kinds2={kinds2}")
    check("resume still ends with the end frame",
          kinds2 and kinds2[-1] == "end", f"kinds2={kinds2}")

    # --- 2e. SSE query-param cursor (?after=) --------------------------------
    r = client.get(f"/api/task/{f}/events", params={"after": cursor}, **QA)
    check("after= cursor accepted", r.status_code == 200, f"code={r.status_code}")

print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
