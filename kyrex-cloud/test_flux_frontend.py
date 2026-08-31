"""Tests for the Flux UI milestone — web frontend vs. backend contract.

The web frontend consumes the Flux SSE stream (flux.py via
web/backend/main.py). These checks pin the contract between the two so
they cannot drift:

  * the removed WebSocket endpoint stays removed;
  * the frontend attaches to the Flux SSE endpoint and the approval /
    cancel routes the backend exposes;
  * every event type the frontend listens for is one the backend emits
    (task_store.add_event call sites + flux.py synthetic events);
  * the frontend's "active task" resume statuses are non-terminal;
  * flux.py keeps its stream_events / format_sse surface.

Run: python3 test_flux_frontend.py
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flux  # noqa: E402
import task_store  # noqa: E402

failures = []

WEB_DIR = Path(__file__).resolve().parent / "web"
FRONTEND = WEB_DIR / "frontend" / "index.html"
BACKEND = WEB_DIR / "backend" / "main.py"


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


html = FRONTEND.read_text()
backend = BACKEND.read_text()


# ── 1. The old WebSocket path is gone ─────────────────────────────────

print("\nTest 1: removed WebSocket endpoint stays removed")
check("no /ws/task reference in frontend", "/ws/task" not in html,
      "frontend must use the Flux SSE endpoint")
check("no WebSocket constructor in frontend", "new WebSocket(" not in html,
      "frontend must use EventSource")


# ── 2. Frontend attaches to the Flux endpoints ────────────────────────

print("\nTest 2: frontend uses the Flux SSE + task action endpoints")
check("frontend opens EventSource", "new EventSource(" in html,
      "live progress must come from the Flux stream")
check("frontend targets the /events endpoint", "'/events'" in html,
      "expected the SSE route /api/task/{id}/events")
check("frontend posts approval replies", "/respond" in html,
      "approval replies must route to /api/task/{id}/respond")
check("frontend posts cancellation", "/cancel" in html,
      "cancellation must route to /api/task/{id}/cancel")
check("frontend submits tasks to the store-backed endpoint",
      "'/api/task'" in html,
      "submission must go through POST /api/task")


# ── 3. Backend keeps the matching routes ──────────────────────────────

print("\nTest 3: backend exposes the routes the frontend calls")
check("backend defines the SSE events route",
      re.search(r'@app\.get\("/api/task/\{task_id\}/events"\)', backend) is not None,
      "GET /api/task/{task_id}/events missing from web/backend/main.py")
check("backend defines the respond route",
      re.search(r'@app\.post\("/api/task/\{task_id\}/respond"\)', backend) is not None,
      "POST /api/task/{task_id}/respond missing from web/backend/main.py")
check("backend defines the cancel route",
      re.search(r'@app\.post\("/api/task/\{task_id\}/cancel"\)', backend) is not None,
      "POST /api/task/{task_id}/cancel missing from web/backend/main.py")
check("backend streams via flux.stream_events", "flux.stream_events" in backend,
      "SSE body must pump flux.stream_events")
check("backend formats frames via flux.format_sse", "flux.format_sse" in backend,
      "SSE frames must come from flux.format_sse")


# ── 4. Event vocabulary cannot drift ──────────────────────────────────

# Every event type the backend can put on the wire:
#   task_store.add_event call sites (store lifecycle + worker callbacks)
#   plus the synthetic end/error events flux.stream_events yields.
BACKEND_EVENT_TYPES = {
    "submitted", "claimed", "status",
    "start", "message", "edit",
    "approval_requested", "approval_resolved",
    "progress", "result",
    "end", "error",  # synthetic (flux.py), event_id: None
}

print("\nTest 4: frontend listens to exactly the backend event vocabulary")
# The contract list is the declared FLUX_EVENT_TYPES array; the listener
# loop iterates over it, so the declared list is the single source of truth.
declared = re.search(r"FLUX_EVENT_TYPES\s*=\s*\[([^\]]*)\]", html)
check("frontend declares FLUX_EVENT_TYPES", declared is not None,
      "the event contract list is missing from index.html")
frontend_types = set(re.findall(r"'(\w+)'", declared.group(1))) if declared else set()
check("listener loop consumes FLUX_EVENT_TYPES",
      "for (const type of FLUX_EVENT_TYPES)" in html,
      "EventSource listeners must be wired from the declared list")

check("frontend listens to every backend event type",
      BACKEND_EVENT_TYPES <= frontend_types,
      f"missing: {sorted(BACKEND_EVENT_TYPES - frontend_types)}")
check("frontend listens to nothing the backend never emits",
      frontend_types <= BACKEND_EVENT_TYPES,
      f"unknown to backend: {sorted(frontend_types - BACKEND_EVENT_TYPES)}")


# ── 5. Resume statuses are non-terminal ───────────────────────────────

print("\nTest 5: frontend resume statuses are non-terminal")
declared_active = re.search(r"ACTIVE_STATUSES\s*=\s*\[([^\]]*)\]", html)
active = set(re.findall(r"'(\w+)'", declared_active.group(1))) if declared_active else set()
check("frontend declares active statuses", bool(active),
      f"got {sorted(active)}")
check("no terminal status treated as active",
      active.isdisjoint(task_store.TERMINAL_STATUSES),
      f"terminal statuses leaked into ACTIVE_STATUSES: "
      f"{sorted(active & task_store.TERMINAL_STATUSES)}")
check("active statuses cover the real lifecycle",
      active == {"queued", "running", "awaiting_approval"},
      f"got {sorted(active)}")


# ── 6. flux.py surface ────────────────────────────────────────────────

print("\nTest 6: flux.py keeps its public surface")
check("stream_events exists", callable(getattr(flux, "stream_events", None)),
      "flux.stream_events missing")
check("format_sse exists", callable(getattr(flux, "format_sse", None)),
      "flux.format_sse missing")
check("latest_event_id exists", callable(getattr(flux, "latest_event_id", None)),
      "flux.latest_event_id missing")


# ── Summary ───────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
