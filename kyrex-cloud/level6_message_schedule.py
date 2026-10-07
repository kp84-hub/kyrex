"""Prepare the upcoming week's Level 6 preview each Sunday evening.

The existing Browser Bot reads the pinned Facebook post; the Calendar Bot
reads Glofox. Results are saved in Chat; sending requires the owner's later
recipient review and explicit Send confirmation through the phone companion.
Set KYREX_LEVEL6_PREVIEW_SCHEDULE_ENABLED=1 to opt in.
"""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from task_store import DuplicateTaskId

EASTERN = ZoneInfo("America/New_York")


def enabled() -> bool:
    # Keep the old opt-in as a migration alias, but never schedule old sends.
    return os.environ.get("KYREX_LEVEL6_PREVIEW_SCHEDULE_ENABLED",
                          os.environ.get("KYREX_LEVEL6_SCHEDULE_ENABLED", "0")) == "1"


def recipient() -> str:
    value = os.environ.get("KYREX_LEVEL6_PREVIEW_RECIPIENT", "L6 Besties").strip()
    if not value or len(value) > 200:
        raise ValueError("Configure a group name of 1–200 characters")
    return value


def preview_task_id(owner: str, week: str) -> str:
    digest = hashlib.sha256(owner.encode()).hexdigest()[:16]
    return f"l6-preview-{digest}-{week}"


def expected_week(task_id: str | None, owner: str) -> str | None:
    prefix = preview_task_id(owner, "")
    if not str(task_id or "").startswith(prefix):
        return None
    week = task_id[len(prefix):]
    try:
        parsed = date.fromisoformat(week)
    except ValueError:
        raise ValueError("Invalid scheduled workout week") from None
    if parsed.weekday() != 0 or parsed.isoformat() != week:
        raise ValueError("Scheduled workout week must start on Monday")
    return week


def preview_conversation(owner: str, destination: str) -> str:
    # Chat modules are otherwise only imported by the web process. This
    # opt-in worker path shares Chat's existing owner-scoped persistence.
    backend = str(Path(__file__).resolve().parent / "web" / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    import chat_service
    return chat_service.ensure_level6_preview_conversation(owner, destination)


def due_date(now: datetime | None = None) -> str | None:
    """Sunday 19:00–19:59 Eastern; return the upcoming Monday, with DST."""
    local = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    if local.weekday() != 6 or not (time(19) <= local.time() < time(20)):
        return None
    return (local.date() + timedelta(days=1)).isoformat()


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
    """Queue one read-only preview per owner/week, including after restart."""
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
    destination = recipient()
    conversation_id = preview_conversation(owner, destination)
    task_id = preview_task_id(owner, day)
    try:
        store.submit(
            session_key=bot["id"], task_text=serve.LEVEL6_MESSAGE_PREVIEW_REQUEST,
            repo_url=None, executor_prefix="level6", bot_id=bot["id"],
            rift=str(bot.get("rift") or ""), chat_id=owner,
            task_id=task_id, resolve_bot=True, conversation_id=conversation_id)
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
