#!/usr/bin/env python3
"""cal_writer.py — bounded, deterministic Calendar WRITER core.

Pure, dependency-free heart of the Calendar Writer capability: it turns the
OWNER's own text into ONE safe create intent, validates it against a bounded
field set, renders the EXACT payload shown at the confirmation gate, and
formats the safe receipt. Nothing here touches the network or a model.

Boundary (deliberately narrow): fields title/start/end/all_day only; the
owner's PRIMARY calendar only; America/New_York only. Guests, recurrence,
updates, deletes, attachments, conferencing, reminders, location, description,
or arbitrary calendar ids are rejected on the way IN or OUT. An ambiguous or
malformed request fails closed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

CALENDAR_ID = "primary"
TIMEZONE = "America/New_York"
MAX_TITLE_CHARS = 200
ALLOWED_FIELDS = frozenset({"title", "start", "end", "all_day"})

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):00$")
_INTENT_RE = re.compile(
    r"^\s*(?:create|add|schedule|book)\s+(?P<title>.+?)\s+on\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?:"
    r"from\s+(?P<start>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+"
    r"to\s+(?P<end>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
    r"|"
    r"at\s+(?P<start2>\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+"
    r"for\s+(?P<dur>\d+)\s*(?P<unit>minutes?|mins?|hours?|hrs?)"
    r"|"
    r"(?P<allday>all[\s-]*day)"
    r")\s*$",
    re.IGNORECASE,
)
_TITLE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DELEGATED_TITLED_RE = re.compile(
    r"^create\\s+a\\s+calendar\\s+event\\s+titled\\s+"
    r"(?P<title>\\\"[^\\\"]+\\\"|“[^”]+”)\\s+(?P<rest>on\\s+.+)$",
    re.IGNORECASE,
)


class CalendarWriterError(Exception):
    """A create request or intent is refused (fail closed, with a clear reason)."""


def _parse_time(token: str) -> str:
    raw = str(token or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", raw)
    if not m:
        raise CalendarWriterError(
            f"unrecognised time {token!r} — use HH:MM or e.g. 3pm")
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) is not None else 0
    meridiem = m.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            raise CalendarWriterError(f"invalid 12-hour time {token!r}")
        hour = (hour % 12) + (12 if meridiem == "pm" else 0)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise CalendarWriterError(f"time out of range: {token!r}")
    return f"{hour:02d}:{minute:02d}"


def _valid_date(value: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise CalendarWriterError(f"invalid date {value!r} — use YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise CalendarWriterError(f"invalid calendar date {value!r}")
    return value


def parse_create_request(text: str) -> dict:
    """Normalise the OWNER'S text into ONE validated create intent.

    Grammar: ``create <title> on <YYYY-MM-DD> from <HH:MM> to <HH:MM>``,
    ``... at <HH:MM> for <N> minutes|hours``, or ``... all day``. Anything else
    (ambiguous/missing time) raises :class:`CalendarWriterError`.
    """
    raw = str(text or "").strip()
    if not raw:
        raise CalendarWriterError("the request is empty")
    # Direct Kyrex Chat commands use the reserved namespace; delegated Writer
    # tasks already arrive without it. Strip exactly one prefix before applying
    # the same bounded grammar to both paths.
    raw = re.sub(r"^calendar:\s+", "", raw, count=1, flags=re.IGNORECASE)
    # Chief-of-Staff delegation may use this one deterministic wrapper. Reduce
    # it to the canonical grammar without interpreting any additional fields.
    delegated = _DELEGATED_TITLED_RE.match(raw)
    if delegated:
        quoted_title = delegated.group("title")
        title = quoted_title[1:-1].strip()
        raw = f"create {title} {delegated.group('rest')}"
    m = _INTENT_RE.match(raw)
    if not m:
        raise CalendarWriterError(
            "I could not read that as a calendar request. Use one of:\n"
            "  create <title> on YYYY-MM-DD from HH:MM to HH:MM\n"
            "  create <title> on YYYY-MM-DD at HH:MM for 30 minutes\n"
            "  create <title> on YYYY-MM-DD all day")
    title = m.group("title").strip()
    date = m.group("date")
    if m.group("allday"):
        return build_intent(title, date, None, None, all_day=True)
    if m.group("dur") is not None:
        start_hm = _parse_time(m.group("start2"))
        try:
            amount = int(m.group("dur"))
        except (TypeError, ValueError):
            raise CalendarWriterError("the duration must be a whole number")
        minutes = amount * 60 if m.group("unit").lower().startswith("h") else amount
        if minutes <= 0:
            raise CalendarWriterError("the duration must be positive")
        start_dt = datetime.strptime(f"{date} {start_hm}", "%Y-%m-%d %H:%M")
        end_hm = (start_dt + timedelta(minutes=minutes)).strftime("%H:%M")
        return build_intent(title, date, start_hm, end_hm, all_day=False)
    return build_intent(
        title, date, _parse_time(m.group("start")), _parse_time(m.group("end")),
        all_day=False)


def build_intent(title, date, start_hm, end_hm, *, all_day: bool) -> dict:
    date = _valid_date(date)
    if all_day:
        start = date
        end = (datetime.strptime(date, "%Y-%m-%d")
               + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start = f"{date}T{start_hm}:00"
        end = f"{date}T{end_hm}:00"
    return validate_intent({"title": title, "start": start, "end": end,
                            "all_day": bool(all_day)})


def validate_intent(intent) -> dict:
    if not isinstance(intent, dict):
        raise CalendarWriterError("a create intent must be an object")
    unknown = sorted(set(intent) - ALLOWED_FIELDS)
    if unknown:
        raise CalendarWriterError(
            f"unsupported field(s): {', '.join(unknown)} — this slice supports "
            "only title, start, end, and all_day")
    missing = sorted(ALLOWED_FIELDS - set(intent))
    if missing:
        raise CalendarWriterError(f"missing field(s): {', '.join(missing)}")
    title = intent.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CalendarWriterError("the event needs a non-empty title")
    title = title.strip()
    if len(title) > MAX_TITLE_CHARS:
        raise CalendarWriterError(
            f"the title is too long (max {MAX_TITLE_CHARS} characters)")
    if _TITLE_CONTROL_RE.search(title):
        raise CalendarWriterError("the title contains control characters")
    all_day = intent.get("all_day")
    if not isinstance(all_day, bool):
        raise CalendarWriterError("all_day must be true or false")
    start = intent.get("start")
    end = intent.get("end")
    if all_day:
        _valid_date(start)
        _valid_date(end)
        if end <= start:
            raise CalendarWriterError("the all-day end must be after its start")
    else:
        m_start = _DATETIME_RE.match(start) if isinstance(start, str) else None
        m_end = _DATETIME_RE.match(end) if isinstance(end, str) else None
        if not m_start or not m_end:
            raise CalendarWriterError(
                "timed events need start and end as YYYY-MM-DDTHH:MM:00")
        _valid_date(m_start.group(1))
        _valid_date(m_end.group(1))
        if end <= start:
            raise CalendarWriterError("the event end must be after its start")
    return {"title": title, "start": start, "end": end, "all_day": all_day}


def load_intent(raw) -> dict:
    if isinstance(raw, dict):
        return validate_intent(raw)
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        raise CalendarWriterError("the create intent is not valid JSON")
    return validate_intent(parsed)


def to_google_event(intent: dict) -> dict:
    """The EXACT Google event body: summary/start/end only, fixed timezone."""
    intent = validate_intent(intent)
    if intent["all_day"]:
        start = {"date": intent["start"]}
        end = {"date": intent["end"]}
    else:
        start = {"dateTime": intent["start"], "timeZone": TIMEZONE}
        end = {"dateTime": intent["end"], "timeZone": TIMEZONE}
    return {"summary": intent["title"], "start": start, "end": end}


def payload_display(intent: dict) -> str:
    """The human-readable payload shown at (and approved at) the gate."""
    intent = validate_intent(intent)
    return "\n".join([
        f"title: {intent['title']}",
        f"start: {intent['start']}",
        f"end: {intent['end']}",
        f"all day: {'yes' if intent['all_day'] else 'no'}",
        f"timezone: {TIMEZONE}",
        f"calendar: {CALENDAR_ID}",
    ])


def _human_when(intent: dict) -> str:
    if intent["all_day"]:
        return f"{intent['start']} (all day)"
    return (f"{intent['start'][:10]} {intent['start'][11:16]}\u2013"
            f"{intent['end'][11:16]} {TIMEZONE}")


def summary_line(intent: dict) -> str:
    """Starts with a harmless verb ("create") for the host's operation name."""
    intent = validate_intent(intent)
    return f"create calendar event \u201c{intent['title']}\u201d \u2014 {_human_when(intent)}"


def intent_from_task(raw) -> dict:
    if isinstance(raw, dict):
        return validate_intent(raw)
    text = str(raw or "").strip()
    if not text:
        raise CalendarWriterError("the request is empty")
    if text[:1] in "{[":
        return load_intent(text)
    return parse_create_request(text)


def format_receipt(intent: dict, created) -> str:
    """Readable receipt: title + when + a SAFE (opaque) event reference."""
    intent = validate_intent(intent)
    event_id = str(created.get("id") or "").strip() if isinstance(created, dict) else ""
    ref = f" (ref: {event_id})" if event_id else ""
    return f"\u2705 Created \u201c{intent['title']}\u201d \u2014 {_human_when(intent)}{ref}"
