"""Submit the fixed Level 6 group message each Sunday at 7 PM Eastern.

The worker only queues the existing Calendar Bot task. Calendar, Glofox,
Browser Host policy, fixed destination and host send receipts remain the
authoritative execution path. Set KYREX_LEVEL6_SCHEDULE_ENABLED=1 to opt in.
"""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

from task_store import DuplicateTaskId

EASTERN = ZoneInfo("America/New_York")


def due_date(now: datetime | None = None) -> str | None:
    """Only run Sunday 19:00–19:59 Eastern; tolerate a short restart."""
    local = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    if local.weekday() != 6 or not (time(19) <= local.time() < time(20)):
        return None
    return local.date().isoformat()


def select_bot(owner: str) -> dict | None:
    """Fail closed unless exactly one owned Calendar Bot has a host binding."""
    import bots
    import browser_hosts
    import serve

    matches = [
        bot for bot in bots.load_bots().values()
        if str(bot.get("owner") or "").strip() == owner
        and str(bot.get("status") or "").strip() == "running"
        and serve.calendar_bot_granted(bot)
        and browser_hosts.binding_for(owner, bot.get("id"))
    ]
    return matches[0] if len(matches) == 1 else None


def submit_due(store, *, now: datetime | None = None, owner: str | None = None) -> str:
    """Queue once per owner/week. Never resubmit a failed or running send."""
    day = due_date(now)
    if day is None:
        return "not due"
    owner = str(owner if owner is not None else os.environ.get("WEB_ALLOWED_GITHUB_USERNAME") or "").strip()
    if not owner:
        return "owner unavailable"
    bot = select_bot(owner)
    if bot is None:
        return "Calendar Bot binding unavailable"

    import serve
    task_id = "gm-week-" + hashlib.sha256(f"{owner}:{day}".encode()).hexdigest()[:32]
    try:
        store.submit(
            session_key=bot["id"], task_text=serve.LEVEL6_MESSAGE_REQUEST,
            repo_url=None, executor_prefix="level6", bot_id=bot["id"],
            rift=str(bot.get("rift") or ""), chat_id=owner,
            task_id=task_id, resolve_bot=True)
        return "queued"
    except DuplicateTaskId:
        return "already queued"


def run(store, shutdown_event) -> None:
    """Run in the existing worker process; the task store deduplicates restarts."""
    previous = None
    while not shutdown_event.is_set():
        try:
            result = submit_due(store)
            if result != previous and result != "not due":
                print(f"[level6-schedule] {result}", file=sys.stderr, flush=True)
            previous = result
        except Exception as exc:
            result = f"unavailable: {type(exc).__name__}"
            if result != previous:
                print(f"[level6-schedule] {result}", file=sys.stderr, flush=True)
            previous = result
        shutdown_event.wait(30)
