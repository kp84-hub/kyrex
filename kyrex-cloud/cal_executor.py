#!/usr/bin/env python3
"""cal_executor.py — Kyrex Cloud Google Calendar executor.

Read-only executor that lists calendar events.  Supports three commands:

  list today     — events starting today (local timezone).
  list tomorrow  — events starting tomorrow.
  list week      — events from today through the next 7 days.

Credentials are read from the environment:
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  GOOGLE_REFRESH_TOKEN

Scope: https://www.googleapis.com/auth/calendar.readonly

Protocol: speaks KYREX_PROGRESS: and KYREX_OPERATION: lines during work
and exactly one KYREX_RESULT_JSON: line at the end on stdout.  Diagnostics
go to stderr.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_ID = "primary"


# ---------------------------------------------------------------------------
# Google Calendar helpers
# ---------------------------------------------------------------------------

def _build_service():
    """Authenticate and return a Google Calendar API service object.

    Uses google.auth and googleapiclient to build a read-only service.
    Environment variables: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
    GOOGLE_REFRESH_TOKEN.
    """
    from google.auth import credentials as google_creds
    from google.auth.transport import requests as google_requests
    from google.oauth2 import credentials as oauth2_creds
    from googleapiclient.discovery import build

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")

    missing = []
    if not client_id:
        missing.append("GOOGLE_CLIENT_ID")
    if not client_secret:
        missing.append("GOOGLE_CLIENT_SECRET")
    if not refresh_token:
        missing.append("GOOGLE_REFRESH_TOKEN")
    if missing:
        raise RuntimeError(
            "Missing required Google Calendar credentials: "
            f"{', '.join(missing)}"
        )

    creds = oauth2_creds.Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[SCOPE],
    )
    # Ensure the token is refreshed before first use.
    creds.refresh(google_requests.Request())

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _fetch_events(service, time_min: str, time_max: str) -> list[dict]:
    """Fetch calendar events in the given time window."""
    events_result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return events_result.get("items", [])


def _format_event(event: dict) -> str:
    """Format a single event as a human-readable line."""
    start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date", "?")
    end = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date", "?")
    summary = event.get("summary", "(no title)")
    return f"  {start} — {end}  {summary}"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _list_events(service, title: str, time_min: str, time_max: str) -> tuple[str, list[str]]:
    """Fetch and format events.

    Returns (title_line, formatted_lines) so the caller can build the
    final response.
    """
    items = _fetch_events(service, time_min, time_max)
    lines = [f"📅 {title} ({len(items)} event(s))"]
    if items:
        for event in items:
            lines.append(_format_event(event))
    else:
        lines.append("  (no events)")
    return lines[0], lines


def handle_list_today(service):
    now = datetime.now(timezone.utc)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_tomorrow = start_of_today + timedelta(days=1)
    time_min = start_of_today.isoformat()
    time_max = start_of_tomorrow.isoformat()
    return _list_events(service, "Today", time_min, time_max)


def handle_list_tomorrow(service):
    now = datetime.now(timezone.utc)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_tomorrow = start_of_today + timedelta(days=1)
    start_of_day_after = start_of_tomorrow + timedelta(days=1)
    time_min = start_of_tomorrow.isoformat()
    time_max = start_of_day_after.isoformat()
    return _list_events(service, "Tomorrow", time_min, time_max)


def handle_list_week(service):
    now = datetime.now(timezone.utc)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_today + timedelta(days=7)
    time_min = start_of_today.isoformat()
    time_max = end_of_week.isoformat()
    return _list_events(service, "This Week", time_min, time_max)


# ---------------------------------------------------------------------------
# Command dispatch table
# ---------------------------------------------------------------------------

COMMANDS = {
    "list today": handle_list_today,
    "list tomorrow": handle_list_tomorrow,
    "list week": handle_list_week,
}


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def _emit_operation(op: str, target: str, summary: str) -> None:
    """Emit a KYREX_OPERATION: line for host-side policy evaluation."""
    operation = {
        "op": op,
        "target": target,
        "summary": summary,
    }
    print(f"KYREX_OPERATION:{json.dumps(operation)}", flush=True)


def _get_verdict() -> bool:
    """Read the host's decision after KYREX_OPERATION:.

    Returns True to proceed, False to refuse.
    """
    decision = sys.stdin.readline().strip()
    if decision == "ALLOW":
        return True
    if decision == "APPROVE":
        print(
            f"KYREX_APPROVAL:{json.dumps({'tier': 0, 'summary': 'calendar read'})}",
            flush=True,
        )
        second = sys.stdin.readline().strip()
        return second == "APPROVED"
    # DENY, DENIED, or unrecognised → refuse
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Kyrex Cloud Google Calendar Executor")
    ap.add_argument("--task", required=True, help="task text, e.g. 'list today'")
    ap.add_argument("--repo-url", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--base", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    task = args.task.strip().lower()

    # Identify the command.
    cmd_parts = task.split(maxsplit=2)
    if len(cmd_parts) < 2 or cmd_parts[0] != "list":
        result = {
            "status": "error",
            "final_response": "",
            "errors": [
                "unsupported command — supported: list today, list tomorrow, list week"
            ],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    # Reconstruct the canonical command key (e.g. "list today", "list week").
    cmd_key = task  # task is already lowered and trimmed

    handler = COMMANDS.get(cmd_key)
    if handler is None:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [
                f"unsupported calendar command: {cmd_key!r} — "
                "supported: list today, list tomorrow, list week"
            ],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    # Summarise the operation for the host.
    summary = f"list calendar events for {cmd_key.removeprefix('list ')}"
    print(f'KYREX_PROGRESS:{{"cal": {json.dumps(task)}}}', flush=True)

    _emit_operation("cal.list", cmd_key, summary)

    if not _get_verdict():
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"calendar read denied: {cmd_key}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    # Build the Google Calendar service and fetch events.
    try:
        service = _build_service()
    except Exception as e:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"Calendar authentication failed: {e}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    try:
        title_line, formatted_lines = handler(service)
    except Exception as e:
        result = {
            "status": "error",
            "final_response": "",
            "errors": [f"Calendar API error: {e}"],
        }
        print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)
        return

    final_response = "\n".join(formatted_lines)
    result = {
        "status": "ok",
        "final_response": final_response,
        "errors": [],
    }
    print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)


if __name__ == "__main__":
    main()