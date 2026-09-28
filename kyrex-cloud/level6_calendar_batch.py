"""Build six approved all-day calendar creates from a validated weekly result.

The existing Level 6 calendar reader requires one tagged all-day event per
Monday–Saturday date. Never accept a week, workout, or calendar destination
from a user-supplied command; the caller supplies ``level6_weekly.run_weekly``
output and the owner's preferred calendar read.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import cal_writer

TZ = ZoneInfo("America/New_York")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
TITLE_PREFIX = "Level 6 Workout: "
_LINE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday) "
    r"(\d{4}-\d{2}-\d{2}) — (.+) — trainer: (.+)$"
)
_REQUEST = re.compile(
    r"(?:add|create|put|schedule)\s+"
    r"(?:this\s+week['’]s|the|these|those)\s+"
    r"(?:six\s+)?level\s*6\s+workouts\s+"
    r"(?:to|on|in)\s+my\s+calendar[.!]?",
    re.IGNORECASE,
)


class CalendarBatchError(ValueError):
    """No calendar writes may proceed with an ambiguous week or event list."""


def is_add_request(text: str) -> bool:
    """Recognize one bounded natural request on the unified Calendar Bot."""
    return bool(_REQUEST.fullmatch(str(text or "").strip()))


def intents_from_week(lines) -> list[dict]:
    """Validate exactly six joined workouts and build Writer-compatible intents."""
    if not isinstance(lines, list) or len(lines) != 6:
        raise CalendarBatchError("the weekly result must contain six days")
    intents = []
    monday = None
    for offset, line in enumerate(lines):
        match = _LINE.fullmatch(line) if isinstance(line, str) else None
        if not match or match.group(1) != WEEKDAYS[offset] or not match.group(4).strip():
            raise CalendarBatchError("a weekly workout row is invalid")
        try:
            day = date.fromisoformat(match.group(2))
        except ValueError as exc:
            raise CalendarBatchError("a weekly workout date is invalid") from exc
        if offset == 0:
            monday = day
            if day.weekday() != 0:
                raise CalendarBatchError("the weekly result does not start Monday")
        if day != monday + timedelta(days=offset):
            raise CalendarBatchError("the weekly dates are not consecutive")
        try:
            intents.append(cal_writer.build_intent(
                TITLE_PREFIX + match.group(3).strip(), day.isoformat(),
                None, None, all_day=True))
        except cal_writer.CalendarWriterError as exc:
            raise CalendarBatchError("a workout title is invalid") from exc
    return intents


def week_bounds(intents: list[dict]) -> tuple[str, str]:
    """Return local [Monday, Sunday) bounds for a complete-calendar read."""
    start = date.fromisoformat(intents[0]["start"])
    beginning = datetime.combine(start, time.min, tzinfo=TZ)
    ending = datetime.combine(start + timedelta(days=6), time.min, tzinfo=TZ)
    return beginning.isoformat(), ending.isoformat()


def missing_intents(intents: list[dict], events) -> list[dict]:
    """Skip exact existing all-day events; reject conflicting/duplicate tags."""
    if not isinstance(events, list):
        raise CalendarBatchError("calendar lookup was incomplete")
    expected = {intent["start"]: intent for intent in intents}
    seen = set()
    for event in events:
        if not isinstance(event, dict):
            raise CalendarBatchError("calendar returned a malformed event")
        if event.get("status") == "cancelled":
            continue
        title = event.get("summary")
        if (not isinstance(title, str)
                or not title.casefold().startswith(TITLE_PREFIX.casefold())):
            continue
        start = event.get("start") or {}
        end = event.get("end") or {}
        if not isinstance(start, dict) or not isinstance(end, dict):
            raise CalendarBatchError("a Level 6 event has invalid dates")
        day = start.get("date") or str(start.get("dateTime") or "")[:10]
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise CalendarBatchError("a Level 6 event has an unreadable start")
        if day not in expected:
            continue
        intended = expected[day]
        if (day in seen or title != intended["title"]
                or start.get("date") != intended["start"]
                or end.get("date") != intended["end"]):
            raise CalendarBatchError(
                f"a conflicting Level 6 event already exists on {day}")
        seen.add(day)
    return [intent for intent in intents if intent["start"] not in seen]


def approval_detail(intents: list[dict]) -> str:
    """The exact six event titles/dates shown at the single approval gate."""
    return "\n".join(f"{intent['start']} (all day): {intent['title']}"
                     for intent in intents)
