#!/usr/bin/env python3
"""
flux.py — Kyrex Cloud event streaming.

Flux is the durable, cursor-based event stream between Kyrex Cloud's
execution path and its transports. The single source of truth is the
``task_events`` table in ``CloudTaskStore`` (SQLite, survives restarts);
flux turns it into a live stream any transport can consume:

  * replay      — an event cursor (``after_event_id``) replays history
                  exactly, so a consumer that reconnects with its last
                  cursor misses nothing;
  * tail        — new events are polled and yielded as they are appended;
  * termination — the stream ends once the task is in a terminal state and
                  fully drained, with an explicit in-band ``end`` event
                  carrying the final status;
  * transport-neutral — yields plain dicts; the HTTP layer formats SSE,
                  a test consumes them directly, a future transport can
                  do either.

KX_SERVE_DESIGN.md's host interface defines ``events(task_id) -> stream of
progress / approval / result``. Flux is that, backed by the store rather
than an in-memory channel, so events survive worker restarts and any
number of concurrent consumers can follow the same task.

Nothing here knows about FastAPI, Telegram, or the frontend.
"""
import json
import time
from datetime import datetime, timezone

from task_store import TERMINAL_STATUSES

# Default polling cadence. The store is SQLite on local disk; polling is
# cheap, and 0.5s keeps end-to-end latency well inside what an operator
# notices, without busy-looping the claim loop's database.
DEFAULT_POLL_INTERVAL = 0.5

# Upper bound on a single stream's lifetime. Tasks have their own watchdog
# and the store recovers orphans, but a consumer that walks away must not
# pin a stream forever — the HTTP layer passes its own value when it wants
# a different bound.
DEFAULT_MAX_SECONDS = 3600.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def latest_event_id(store, task_id: str) -> int:
    """Return the newest event_id for *task_id*, or 0 if there are none.

    Used by transports that want to attach to a stream "now" without
    replaying history.
    """
    events = store.get_events(task_id, after_event_id=0)
    if not events:
        return 0
    return events[-1]["event_id"]


def stream_events(
    store,
    task_id: str,
    after_event_id: int = 0,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    max_seconds: float = DEFAULT_MAX_SECONDS,
):
    """Yield the event stream for *task_id*, oldest first, blocking.

    Behaviour:
      * Unknown *task_id*: yields exactly one ``error`` event
        (``payload.error == "unknown_task"``) and stops — a consumer can
        distinguish a dead cursor from an empty stream.
      * Known task: replays every event with ``event_id > after_event_id``,
        then tails new ones until the task reaches a terminal state
        (done / failed / cancelled) and its events are fully drained.
      * Termination is explicit: the last yielded event is an ``end`` event
        whose payload carries the final ``status`` (and a ``reason`` when
        the stream ended for a reason other than task completion).
      * Synthetic events (``end``, ``error``) have ``event_id: None`` —
        they are stream metadata, not store rows, and must never advance
        a consumer's cursor.

    Args:
        store: a ``CloudTaskStore`` (any object with get / status /
            get_events is enough).
        task_id: the task to follow.
        after_event_id: cursor — only events after this id are streamed.
            0 replays the whole task.
        poll_interval: seconds between store polls while waiting for new
            events.
        max_seconds: hard lifetime for the stream. On expiry the stream
            ends with an ``end`` event whose payload has
            ``reason == "max_seconds_exceeded"`` and the status observed
            at that moment.

    Yields:
        Event dicts: ``{"event_id", "type", "payload", "created_at"}``
        as stored, plus the synthetic ``end`` / ``error`` events.
    """
    started = time.monotonic()
    cursor = int(after_event_id)

    task = store.get(task_id)
    if task is None:
        yield {
            "event_id": None,
            "type": "error",
            "payload": {"error": "unknown_task", "task_id": task_id},
            "created_at": _now_iso(),
        }
        return

    while True:
        events = store.get_events(task_id, after_event_id=cursor)
        if events:
            for event in events:
                cursor = event["event_id"]
                yield event
            # Drain immediately before polling again — a burst appended
            # while we were yielding should not wait out a poll interval.
            continue

        status = store.status(task_id)
        if status is None:
            # The task vanished mid-stream (store reset). Report rather
            # than spin until max_seconds.
            yield {
                "event_id": None,
                "type": "end",
                "payload": {"task_id": task_id, "status": None,
                            "reason": "task_not_found"},
                "created_at": _now_iso(),
            }
            return
        if status in TERMINAL_STATUSES:
            yield {
                "event_id": None,
                "type": "end",
                "payload": {"task_id": task_id, "status": status},
                "created_at": _now_iso(),
            }
            return
        if max_seconds is not None and (time.monotonic() - started) > max_seconds:
            yield {
                "event_id": None,
                "type": "end",
                "payload": {"task_id": task_id, "status": status,
                            "reason": "max_seconds_exceeded"},
                "created_at": _now_iso(),
            }
            return
        time.sleep(poll_interval)


def format_sse(event: dict) -> str:
    """Format one stream event as a Server-Sent Events frame.

    Real store events carry ``event_id`` and become ``id:`` lines, so a
    browser ``EventSource`` reconnect resumes from its ``Last-Event-ID``
    with no gaps. Synthetic events (``event_id: None``) omit the id line —
    they are stream metadata and must not advance a consumer's cursor.

    Returns a complete frame ending in a blank line.
    """
    event_type = event.get("type", "message")
    payload = event.get("payload")
    data = json.dumps(payload if payload is not None else {}, sort_keys=True)
    lines = []
    if event.get("event_id") is not None:
        lines.append(f"id: {event['event_id']}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"
