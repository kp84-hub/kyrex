#!/usr/bin/env python3
"""cal_executor.py — Kyrex Cloud Google Calendar READ-ONLY executor.

The reader supports exactly three byte-exact commands::

    calendar: today
    calendar: tomorrow
    calendar: week

Legacy aliases ``list today`` / ``list tomorrow`` / ``list week`` are also
accepted so pre-existing callers and tests keep working. Any other request
is refused: there is NO event creation, update, or delete in this executor
(event creation is out of scope and removed here).

Day boundaries are computed in America/New_York (DST-correct) by
``calendar_windows`` -- never in UTC, so a "day" is always local midnight to
local midnight.

Credentials (read-only scope
``https://www.googleapis.com/auth/calendar.readonly``):

  * The owner-facing Chat path never uses this process; it runs the reader
    in-process against the OWNER-SCOPED encrypted connector store
    (``connectors.py``). This standalone executor keeps the host
    ``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET`` / ``GOOGLE_REFRESH_TOKEN``
    variables for the non-Chat path and FAILS CLOSED when they are absent.

Protocol: speaks KYREX_PROGRESS: and KYREX_OPERATION: lines during work and
exactly one KYREX_RESULT_JSON: line at the end on stdout. Diagnostics go to
stderr.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import calendar_windows as _cal  # noqa: E402

SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_ID = "primary"
#: Bound on the provider payload and the rendered output.
MAX_EVENTS = _cal.MAX_EVENTS

#: Canonical command text -> window key. Exactly these six; anything else is
#: unsupported and fails closed.
_COMMANDS = {
    "list today": "today",
    "list tomorrow": "tomorrow",
    "list week": "week",
    "calendar: today": "today",
    "calendar: tomorrow": "tomorrow",
    "calendar: week": "week",
}

#: Backwards-compatible view of the command table.
COMMANDS = dict(_COMMANDS)


# ---------------------------------------------------------------------------
# Google Calendar helpers
# ---------------------------------------------------------------------------

def _build_service():
    """Authenticate and return a Google Calendar API service object.

    Environment variables: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
    GOOGLE_REFRESH_TOKEN. Missing credentials raise before any API call.
    """
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


def _fetch_events(service, time_min, time_max, max_results=MAX_EVENTS):
    """Fetch a BOUNDED batch of events for the window.

    The provider response is validated: a non-mapping payload or a non-list
    ``items`` value fails closed rather than being rendered.
    """
    events_result = (
        service.events()
        .list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=max(1, min(int(max_results or MAX_EVENTS), MAX_EVENTS)),
        )
        .execute()
    )
    if not isinstance(events_result, dict):
        raise RuntimeError("malformed Google Calendar response (not an object)")
    items = events_result.get("items", [])
    if items is None:
        items = []
    if not isinstance(items, list):
        raise RuntimeError("malformed Google Calendar response (items not a list)")
    return items


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _read_window(service, window):
    """Fetch and render the readable response for *window*."""
    label, time_min, time_max = _cal.window_bounds(window)
    items = _fetch_events(service, time_min, time_max)
    return label, _cal.render_events(label, items)


def handle_list_today(service):
    return _read_window(service, "today")


def handle_list_tomorrow(service):
    return _read_window(service, "tomorrow")


def handle_list_week(service):
    return _read_window(service, "week")


# ---------------------------------------------------------------------------
# Protocol helpers
# ---------------------------------------------------------------------------

def _emit_operation(op: str, target: str, summary: str) -> None:
    """Emit a KYREX_OPERATION: line for host-side policy evaluation."""
    operation = {"op": op, "target": target, "summary": summary}
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
    # DENY, DENIED, or unrecognised -> refuse
    return False


def _emit_result(result: dict) -> None:
    print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Kyrex Cloud Google Calendar Executor")
    ap.add_argument("--task", required=True, help="task text, e.g. 'calendar: today'")
    ap.add_argument("--repo-url", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--base", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    orig = args.task.strip()
    # Byte-exact routing: collapse whitespace, lowercase, then look up.
    key = " ".join(orig.lower().split())
    window = _COMMANDS.get(key)
    if window is None:
        _emit_result({
            "status": "error",
            "final_response": "",
            "errors": [
                f"unsupported calendar command: {orig!r} — supported: "
                "list today, list tomorrow, list week, "
                "calendar: today, calendar: tomorrow, calendar: week"
            ],
        })
        return

    cmd_key = f"calendar: {window}" if key.startswith("calendar:") else f"list {window}"

    print(f'KYREX_PROGRESS:{{"cal": {json.dumps(orig)}}}', flush=True)
    _emit_operation("cal.list", cmd_key,
                    f"list calendar events for {window}")

    if not _get_verdict():
        _emit_result({
            "status": "error",
            "final_response": "",
            "errors": [f"calendar read denied: {cmd_key}"],
        })
        return

    # Build the Google Calendar service and fetch events.
    try:
        service = _build_service()
    except Exception as e:
        _emit_result({
            "status": "error",
            "final_response": "",
            "errors": [f"Calendar authentication failed: {e}"],
        })
        return

    try:
        _title_line, final_response = _read_window(service, window)
    except Exception as e:
        _emit_result({
            "status": "error",
            "final_response": "",
            "errors": [f"Calendar API error: {e}"],
        })
        return

    _emit_result({"status": "ok", "final_response": final_response, "errors": []})


if __name__ == "__main__":
    main()
