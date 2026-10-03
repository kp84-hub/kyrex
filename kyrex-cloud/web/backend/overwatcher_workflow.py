"""Return short Browser reads to Overwatcher without changing target authority.

No new tasks, approval decisions, raw provider results or browser controls are
created here. One turn has one bounded wait budget across all Browser reads.
Slow work retains its durable asynchronous lifecycle and normal result relay.
"""
import json
import time

WAIT_SECONDS = 20.0
POLL_SECONDS = 0.05
TERMINAL = {"done", "failed", "cancelled", "rejected"}


def follow_browser_read(chat, bot_service, session, frame, submitted):
    from jev_stream_router import _is_explicit_browser_subtask
    if not _is_explicit_browser_subtask(chat, bot_service, session, frame):
        return submitted
    try:
        actions = json.loads(frame["task"])["actions"]
        if not any(a["action"] == "read" for a in actions) or any(
                a["action"] not in {"navigate", "read"} for a in actions):
            return submitted
    except (KeyError, TypeError, ValueError):
        return submitted
    ctx = session.delegation_ctx or {}
    owner = str(ctx.get("owner") or "")
    coordinator = str((ctx.get("bot") or {}).get("id") or "")
    conversation = str(ctx.get("conversation_id") or "")
    did, tid = submitted.get("delegation_id"), submitted.get("task_id")
    if not owner or not coordinator or not conversation or not did or not tid:
        return submitted
    deadline = getattr(session, "_browser_follow_deadline", None)
    if deadline is None:
        deadline = time.monotonic() + WAIT_SECONDS
        session._browser_follow_deadline = deadline
    cancelled = getattr(session, "_browser_follow_cancel", None)
    try:
        store = chat._task_store()
        while True:
            if callable(cancelled) and cancelled():
                return submitted
            rec = store.get_delegation(did) or {}
            # Validate the complete durable relationship before observing or
            # marking anything. A stale/foreign result cannot become evidence.
            if (rec.get("owner") != owner or rec.get("coordinator_bot_id") != coordinator
                    or rec.get("parent_conversation_id") != conversation
                    or rec.get("target_bot_id") != frame.get("target_bot_id")
                    or rec.get("task_id") != tid or rec.get("executor_prefix") != "browser"):
                return submitted
            task = store.get(tid) or {}
            if (task.get("chat_id") != owner or task.get("bot_id") != rec["target_bot_id"]
                    or task.get("parent_delegation_id") != did
                    or task.get("executor_prefix") != "browser"):
                return submitted
            status = task.get("status")
            if status in TERMINAL or status == "awaiting_approval":
                rec = chat._reconcile_delegation(store, rec)
                view = dict(chat.delegation.public_view(rec))
                if status in TERMINAL and view.get("status") in TERMINAL:
                    # The result is already being delivered to the coordinator
                    # model; retain the task card but avoid a duplicate notice.
                    store.mark_delegation_relayed(did)
                return view
            if time.monotonic() >= deadline:
                return submitted
            time.sleep(POLL_SECONDS)
    except Exception:
        # A convenience wait must never turn a successful submission into a
        # task failure or cancel background work on transport/read failure.
        return submitted


def is_browser_read_task(task):
    """Identify persisted research work; other delegated actions keep receipts."""
    if not task or task.get("executor_prefix") != "browser":
        return False
    try:
        spec = json.loads(task.get("task_text") or "")
        actions = spec["actions"]
        return bool(actions) and any(a["action"] == "read" for a in actions) and all(
            a["action"] in {"navigate", "read"} for a in actions)
    except (KeyError, TypeError, ValueError):
        return False
