"""calendar_windows.py — Google-Calendar day windows in America/New_York.

Pure stdlib helpers (``zoneinfo``) that turn "now" into the [timeMin,
timeMax) window for the three supported reads::

    today | tomorrow | week | weekday | YYYY-MM-DD

Every boundary is a LOCAL midnight in ``America/New_York`` — never a UTC
midnight. Adding ``timedelta(days=1)`` to a zone-aware ``datetime`` performs
wall-clock arithmetic in Python, so the next boundary is the next local
midnight with the offset re-resolved for that date. A DST day is therefore
still ONE local day (its absolute length is 23 h or 25 h), which is exactly
what Google Calendar expects for a "day" query.

This module is the single source of truth for both the window and the
rendering, shared by the in-process Chat reader (``serve.py``) and the
standalone ``cal_executor.py`` so the two can never drift.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

#: The owner's calendar timezone. All day boundaries are local to this zone.
CALENDAR_TZ = ZoneInfo("America/New_York")

#: Hard bounds — the provider payload and the rendered text are both capped.
MAX_EVENTS = 25
MAX_TITLE_CHARS = 200
MAX_RESPONSE_CHARS = 4000

#: The three supported windows.
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
WINDOWS = ("today", "tomorrow", "week", *WEEKDAYS)

_LABELS = {"today": "Today", "tomorrow": "Tomorrow", "week": "This Week"}


class CalendarWindowError(ValueError):
    """An unsupported window or a malformed provider response (fail closed)."""


def local_now(now=None):
    """Return *now* as an aware datetime in :data:`CALENDAR_TZ`.

    ``None`` uses the wall clock. A naive value is taken as already local; an
    aware value is converted. Tests pass a fixed instant for determinism.
    """
    if now is None:
        return datetime.now(CALENDAR_TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=CALENDAR_TZ)
    return now.astimezone(CALENDAR_TZ)


def _start_of_day(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def valid_date_key(value):
    """Accept one ISO calendar date, never ranges or provider parameters."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


_MONTHS = {name: n for n, name in enumerate((
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december"), 1)}
_MONTHS.update({name[:3]: n for name, n in list(_MONTHS.items())})


def named_date_key(value, *, now=None):
    """Resolve one English month/day; omitted year means the owner's current year."""
    if valid_date_key(value):
        return value
    match = re.fullmatch(
        r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:(?:,\s*|\s+)(\d{4}))?",
        str(value or "").strip(), re.IGNORECASE)
    if not match or match.group(1).lower() not in _MONTHS:
        return None
    year = int(match.group(3)) if match.group(3) else local_now(now).year
    key = f"{year:04d}-{_MONTHS[match.group(1).lower()]:02d}-{int(match.group(2)):02d}"
    return key if valid_date_key(key) else None


def valid_range_key(value):
    """One inclusive ISO date range, bounded to 31 local calendar days."""
    if not isinstance(value, str) or value.count('..') != 1:
        return False
    first, last = value.split('..')
    if not valid_date_key(first) or not valid_date_key(last):
        return False
    days = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).days
    return 0 <= days < 31


def named_range_key(value, *, now=None):
    """Resolve an explicit date range; a missing year uses the local year."""
    text = str(value or '').strip().lower()
    if valid_range_key(text):
        return text
    iso = re.fullmatch(r'(\d{4}-\d{2}-\d{2})\s*(?:to|through|[–—])\s*'
                       r'(\d{4}-\d{2}-\d{2})', text)
    if iso:
        key = '..'.join(iso.groups())
        return key if valid_range_key(key) else None
    match = re.fullmatch(
        r'([a-z]+)\s+(\d{1,2})\s*(?:-|–|—|to|through)\s*'
        r'(?:([a-z]+)\s+)?(\d{1,2})(?:,?\s+(\d{4}))?', text)
    if not match:
        return None
    month, first, last_month, last, year = match.groups()
    year = year or str(local_now(now).year)
    first_key = named_date_key(f'{month} {first} {year}', now=now)
    last_key = named_date_key(f'{last_month or month} {last} {year}', now=now)
    key = f'{first_key}..{last_key}'
    return key if valid_range_key(key) else None


def window_bounds(which, *, now=None):
    """Return ``(label, time_min, time_max)`` for *which*.

    All three are local (America/New_York) boundaries rendered as ISO-8601
    with offset; ``time_max`` is exclusive. Raises
    :class:`CalendarWindowError` for unsupported aliases or invalid dates.
    """
    key = str(which or "").strip().lower()
    if valid_range_key(key):
        first, last = key.split('..')
        start = datetime.fromisoformat(first).replace(tzinfo=CALENDAR_TZ)
        end_day = datetime.fromisoformat(last).replace(tzinfo=CALENDAR_TZ)
        end = end_day + timedelta(days=1)
        label = f"{start.strftime('%b %d, %Y')}–{end_day.strftime('%b %d, %Y')}"
        return label, start.isoformat(), end.isoformat()
    if valid_date_key(key):
        start = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=CALENDAR_TZ)
        end = start + timedelta(days=1)
        return start.strftime("%A, %b %d, %Y"), start.isoformat(), end.isoformat()
    if key not in WINDOWS:
        raise CalendarWindowError(f"unsupported calendar window {which!r}")
    start_today = _start_of_day(local_now(now))
    if key == "today":
        start, end = start_today, start_today + timedelta(days=1)
    elif key == "tomorrow":
        start = start_today + timedelta(days=1)
        end = start + timedelta(days=1)
    elif key in WEEKDAYS:
        days_ahead = (WEEKDAYS.index(key) - start_today.weekday()) % 7
        start = start_today + timedelta(days=days_ahead)
        end = start + timedelta(days=1)
        return start.strftime("%A, %b %d"), start.isoformat(), end.isoformat()
    else:  # week
        start, end = start_today, start_today + timedelta(days=7)
    return _LABELS[key], start.isoformat(), end.isoformat()


def search_window_bounds(*, now=None):
    """Return a bounded calendar-search interval around local today.

    Include recent history and future reminders without allowing an unbounded
    provider scan. The result uses local midnight boundaries in the calendar's
    timezone, like the regular day/week reader.
    """
    today = _start_of_day(local_now(now))
    start = today - timedelta(days=365)
    end = today + timedelta(days=731)
    return "Calendar search · past year and next two years", start.isoformat(), end.isoformat()


def parse_event_time(value):
    """Parse an event start/end value into ``(aware_dt, all_day)``.

    Accepts an RFC3339 ``dateTime`` (offset or ``Z``) or a bare ``date``.
    Returns ``(None, False)`` for anything unparseable — the caller decides
    whether that is fatal.
    """
    if not isinstance(value, str) or not value.strip():
        return None, False
    text = value.strip()
    try:
        if "T" in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(CALENDAR_TZ), False
        day = datetime.strptime(text, "%Y-%m-%d")
        return day.replace(tzinfo=CALENDAR_TZ), True
    except (ValueError, TypeError):
        return None, False


def _fmt_clock(dt):
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{dt.strftime('%M %p')}"


def _event_details(event):
    """Return a validated local start/end, all-day flag, and one-line title."""
    if not isinstance(event, dict):
        raise CalendarWindowError("malformed event (not an object)")
    start_raw = event.get("start") or {}
    end_raw = event.get("end") or {}
    if not isinstance(start_raw, dict) or not isinstance(end_raw, dict):
        raise CalendarWindowError("malformed event start/end")
    start, all_day_start = parse_event_time(
        start_raw.get("dateTime") or start_raw.get("date"))
    end, _all_day_end = parse_event_time(
        end_raw.get("dateTime") or end_raw.get("date"))
    if start is None:
        raise CalendarWindowError("event has no parseable start")
    title = re.sub(r"\s+", " ", str(event.get("summary") or "")).strip()
    return start, end, all_day_start, (title or "(no title)")[:MAX_TITLE_CHARS]


def format_event(event):
    """Render one event as a readable local line (start-end + title)."""
    start, end, all_day_start, title = _event_details(event)
    if all_day_start:
        when = start.strftime("%a %b %d (all day)")
    elif end is None:
        when = f"{start.strftime('%a %b %d')} {_fmt_clock(start)}"
    else:
        when = (f"{start.strftime('%a %b %d')} "
                f"{_fmt_clock(start)}-{_fmt_clock(end)}")
    return f"  {when}  {title}"


def _markdown_title(title):
    """Keep untrusted provider titles literal in the Chat Markdown list."""
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", title)


def render_events(label, events):
    """Render a bounded Chat Markdown agenda grouped by local calendar day.

    ``events`` MUST be a list (a malformed provider payload fails closed).
    Output is capped at :data:`MAX_EVENTS` events and
    :data:`MAX_RESPONSE_CHARS` characters, with explicit truncation markers.
    """
    if not isinstance(events, list):
        raise CalendarWindowError("malformed provider response (items not a list)")
    shown = events[:MAX_EVENTS]
    count = len(events)
    lines = [f"**{label}** · {count} {'event' if count == 1 else 'events'}"]
    if not events:
        lines.extend(("", "No events scheduled."))
    else:
        current_day = None
        for event in shown:
            start, end, all_day, title = _event_details(event)
            if start.date() != current_day:
                current_day = start.date()
                lines.extend(("", f"**{start.strftime('%A, %b %d')}**", ""))
            if all_day:
                when = "All day"
            elif end is None:
                when = _fmt_clock(start)
            else:
                when = f"{_fmt_clock(start)}–{_fmt_clock(end)}"
            lines.append(f"- {when} — {_markdown_title(title)}")
        if count > MAX_EVENTS:
            lines.extend(("", f"Showing the first {MAX_EVENTS} events."))
    text = "\n".join(lines)
    if len(text) > MAX_RESPONSE_CHARS:
        # Preserve complete event lines and valid Markdown when truncating.
        lines = lines[:1]
        for line in text.splitlines()[1:]:
            candidate = "\n".join((*lines, line, "", "More events not shown."))
            if len(candidate) > MAX_RESPONSE_CHARS:
                break
            lines.append(line)
        text = "\n".join(lines).rstrip() + "\n\nMore events not shown."
    return text
