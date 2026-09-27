"""Submit the Facebook Level 6 group message each Sunday at 7 PM Eastern.

The existing Browser Bot reads the pinned Facebook post; the Calendar Bot
reads Glofox and sends the validated result through the paired profile.
Set KYREX_LEVEL6_SCHEDULE_ENABLED=1 to opt in.
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
    """Fail closed unless the existing Browser and Calendar Bots share a host."""
    import bots
    import browser_hosts
    import level6_weekly
    import serve

    registry = list(bots.load_bots().values())
    matches = [
        bot for bot in registry
        if str(bot.get("owner") or "").strip() == owner
        and str(bot.get("status") or "").strip() == "running"
        and serve.calendar_bot_granted(bot)
        and browser_hosts.binding_for(owner, bot.get("id"))
    ]
    if len(matches) != 1:
        return None
    browser = next((bot for bot in registry
                    if bot.get("id") == level6_weekly.BROWSER_BOT_ID), None)
    host = browser_hosts.binding_for(owner, matches[0]["id"])
    if (browser is None
            or str(browser.get("owner") or "").strip() != owner
            or str(browser.get("status") or "").strip() != "running"
            or not serve.is_browser_bot_policy(browser.get("policy"))
            or browser_hosts.binding_for(owner, level6_weekly.BROWSER_BOT_ID) != host):
        return None
    return matches[0]


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
        return "Browser Bot or Calendar Bot binding unavailable"

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
