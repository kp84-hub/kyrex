"""calendar_windows.py — Google-Calendar day windows in America/New_York.

Pure stdlib helpers (``zoneinfo``) that turn "now" into the [timeMin,
timeMax) window for the three supported reads::

    today | tomorrow | week

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

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

#: The owner's calendar timezone. All day boundaries are local to this zone.
CALENDAR_TZ = ZoneInfo("America/New_York")

#: Hard bounds — the provider payload and the rendered text are both capped.
MAX_EVENTS = 25
MAX_TITLE_CHARS = 200
MAX_RESPONSE_CHARS = 4000

#: The three supported windows.
WINDOWS = ("today", "tomorrow", "week")

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


def window_bounds(which, *, now=None):
    """Return ``(label, time_min, time_max)`` for *which*.

    All three are local (America/New_York) boundaries rendered as ISO-8601
    with offset; ``time_max`` is exclusive. Raises
    :class:`CalendarWindowError` for anything but today/tomorrow/week.
    """
    key = str(which or "").strip().lower()
    if key not in WINDOWS:
        raise CalendarWindowError(f"unsupported calendar window {which!r}")
    start_today = _start_of_day(local_now(now))
    if key == "today":
        start, end = start_today, start_today + timedelta(days=1)
    elif key == "tomorrow":
        start = start_today + timedelta(days=1)
        end = start + timedelta(days=1)
    else:  # week
        start, end = start_today, start_today + timedelta(days=7)
    return _LABELS[key], start.isoformat(), end.isoformat()


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


def format_event(event):
    """Render one event as a readable local line (start-end + title).

    Raises :class:`CalendarWindowError` when the event is not a mapping or
    carries no usable start — callers fail closed rather than print garbage.
    """
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
    title = str(event.get("summary") or "").strip() or "(no title)"
    title = title[:MAX_TITLE_CHARS]
    if all_day_start:
        when = start.strftime("%a %b %d (all day)")
    elif end is None:
        when = f"{start.strftime('%a %b %d')} {_fmt_clock(start)}"
    else:
        when = (f"{start.strftime('%a %b %d')} "
                f"{_fmt_clock(start)}-{_fmt_clock(end)}")
    return f"  {when}  {title}"


def render_events(label, events):
    """Render a bounded, readable multi-line response for *events*.

    ``events`` MUST be a list (a malformed provider payload fails closed).
    Output is capped at :data:`MAX_EVENTS` events and
    :data:`MAX_RESPONSE_CHARS` characters, with explicit truncation markers.
    """
    if not isinstance(events, list):
        raise CalendarWindowError("malformed provider response (items not a list)")
    shown = events[:MAX_EVENTS]
    lines = [f"{label} ({len(events)} event(s))"]
    if not events:
        lines.append("  (no events)")
    else:
        for event in shown:
            lines.append(format_event(event))
        if len(events) > MAX_EVENTS:
            lines.append(f"  ... {len(events) - MAX_EVENTS} more not shown")
    text = "\n".join(lines)
    if len(text) > MAX_RESPONSE_CHARS:
        text = text[:MAX_RESPONSE_CHARS - 1].rstrip() + "..."
    return text
