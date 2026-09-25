#!/usr/bin/env python3
"""email_event.py — bounded, deterministic email -> calendar event extraction.

Pure, dependency-free heart of the bounded "email -> calendar" handoff: given
ONE already-read, already-redacted Gmail message projection (Subject/From/Date
plus the tag-free ``body``), it extracts the SAFE event facts a calendar create
needs -- title, date, start/end time (or all-day), and location -- and reports,
just as explicitly, which REQUIRED facts are genuinely MISSING or AMBIGUOUS.

Nothing here touches the network, a model, a token, or the connector: it is a
pure text reducer over the owner's own message. It deliberately FAILS CLOSED:
a fact is only offered when there is a SINGLE, unambiguous reading; a second,
conflicting reading marks the fact ambiguous, and an unreadable/absent fact is
left missing. It never guesses a year, a meridiem, or an event title it did
not actually see.

It also recognises the bounded PRONOUN handoff ("add that to my calendar",
"put it on my calendar") so the router can resolve "that"/"it" to the selected
email -- never to an arbitrary message.
"""
from __future__ import annotations

import re

#: Bounds on the derived strings (so an oversized message can never balloon a
#: Chat response or a create intent).
MAX_TITLE_CHARS = 200
MAX_LOCATION_CHARS = 200
MAX_TEXT_CHARS = 600

#: The three required facts a create intent cannot be built without.
_REQUIRED = ("title", "date", "time")

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_US_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_MONTH_DATE_RE = re.compile(
    r"\b(" + _MONTH_ALT + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(\d{4}))?\b",
    re.IGNORECASE)

#: An explicit, single-line event title ("Event: ...", "Title: ...").
_TITLE_LABEL_RE = re.compile(
    r"^\s*(?:event|title|what)\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)

#: A time of day WITH a required meridiem marker. A bare hour is never taken.
_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)\b", re.IGNORECASE)
#: A start/end RANGE: "9:00 am - 2:00 pm", "9am to 2pm", "9:00–2:00" ...
_RANGE_RE = re.compile(
    r"(\d{1,2}(?::\d{2})?\s*(?:a\.?\s?m\.?|p\.?\s?m\.?))"
    r"\s*(?:-|–|—|to|until|through)\s*"
    r"(\d{1,2}(?::\d{2})?\s*(?:a\.?\s?m\.?|p\.?\s?m\.?))",
    re.IGNORECASE)
_ALL_DAY_RE = re.compile(r"\ball[\s-]*day\b", re.IGNORECASE)

_LOCATION_LABEL_RE = re.compile(
    r"^\s*(?:location|where|place|venue|address)\s*[:\-]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE)

_LEADING_REPLY_RE = re.compile(r"^(?:(?:re|fwd|fw)\s*:\s*)+", re.IGNORECASE)

#: The bounded PRONOUN handoff: an "add/put/save/schedule" verb over an
#: anaphor ("that"/"this"/"it"/"the email"/"the event") that names a calendar.
_CAL_ADD_ANAPHOR_RE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
    r"(?:add|put|save|schedule|book|create)\s+"
    r"(?:that|this|it|these|those|the\s+(?:e-?mail|message|event|invite|"
    r"trip|field\s+trip|appointment))\b"
    r".*\b(?:calendar|schedule)\b",
    re.IGNORECASE)


class EmailEventError(Exception):
    """A create intent cannot be built from the extracted facts (fail closed).

    ``needs`` names the required facts that are missing/ambiguous so the caller
    can ask for exactly those and nothing more.
    """

    def __init__(self, message: str, *, needs=None):
        super().__init__(message)
        self.needs = list(needs or [])


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _bounded(value, limit: int):
    text = _clean(value)
    return text[:limit] if text else None


# ── date extraction ────────────────────────────────────────────────────

def _year_from_date_header(value):
    """The 4-digit year in a Gmail ``Date`` header, or ``None`` (never guess)."""
    m = re.search(r"\b(\d{4})\b", str(value or ""))
    if not m:
        return None
    year = int(m.group(1))
    return year if 1970 <= year <= 2999 else None


def _collect_dates(text: str, default_year):
    """Return ``(distinct_iso_dates, any_yearless)`` from *text*.

    Only well-formed dates are collected. A month/day with no visible year
    uses *default_year* when one is known, otherwise it is counted as a
    yearless reading so the caller can fail closed rather than assume a year.
    """
    found: set[str] = set()
    yearless = False

    def _add(year, month, day):
        nonlocal yearless
        try:
            if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
                return
        except (TypeError, ValueError):
            return
        if year is None:
            yearless = True
            return
        found.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")

    for m in _ISO_DATE_RE.finditer(text):
        _add(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in _US_DATE_RE.finditer(text):
        _add(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    for m in _MONTH_DATE_RE.finditer(text):
        month = _MONTHS.get(m.group(1).lower())
        year = int(m.group(3)) if m.group(3) else default_year
        _add(year, month, int(m.group(2)))
    return found, yearless


# ── time extraction ────────────────────────────────────────────────────

def _parse_meridiem(token: str):
    t = re.sub(r"[\s.]", "", str(token or "")).lower()
    if t in ("am", "a"):
        return "am"
    if t in ("pm", "p"):
        return "pm"
    return None


def _to_hm(hour: int, minute: int, meridiem: str):
    hour = int(hour)
    minute = int(minute or 0)
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _parse_time(token: str):
    """``"9:30 am"`` -> ``"09:30"``; ``None`` when there is no meridiem."""
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)\s*$",
                 str(token or ""), re.IGNORECASE)
    if not m:
        return None, None
    mer = _parse_meridiem(m.group(3))
    if mer is None:
        return None, None
    return _to_hm(int(m.group(1)), int(m.group(2) or 0), mer), mer


def _collect_times(text: str):
    """Return ``(distinct_ranges, ambiguous)`` where a range is ``(s, e)``.

    Only ranges whose BOTH ends carry an explicit meridiem are accepted; a
    range whose end lacks a meridiem inherits the start's, flipped when that
    would put the end before the start (e.g. "9:00 am to 2:00" -> 14:00). A
    second, different range marks the time ambiguous.
    """
    ranges: set[tuple] = set()
    for m in _RANGE_RE.finditer(text):
        start_hm, start_mer = _parse_time(m.group(1))
        end_hm, end_mer = _parse_time(m.group(2))
        if start_hm is None:
            continue
        if end_hm is None:
            end_mer = start_mer
            em = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*$", m.group(2))
            if not em:
                continue
            end_hm = _to_hm(int(em.group(1)), int(em.group(2) or 0), end_mer)
            if end_hm is None:
                continue
            if end_hm <= start_hm:
                flip = "pm" if end_mer == "am" else "am"
                end_hm = _to_hm(int(em.group(1)), int(em.group(2) or 0), flip)
        if end_hm is None or end_hm <= start_hm:
            continue
        ranges.add((start_hm, end_hm))
    return ranges, len(ranges) > 1


# ── location extraction ────────────────────────────────────────────────

def _collect_locations(text: str):
    locations = {
        _clean(m.group(1))[:MAX_LOCATION_CHARS]
        for m in _LOCATION_LABEL_RE.finditer(text)
        if _clean(m.group(1))
    }
    return locations


# ── the extractor ──────────────────────────────────────────────────────

def extract_event_facts(*, subject=None, sender=None, date=None, body=None) -> dict:
    """Reduce ONE message to SAFE event facts plus its missing/ambiguous set.

    Returns a dict with ``title``/``date``/``start``/``end``/``all_day``/
    ``location``/``text`` (each ``None`` when not reliably present) and the
    diagnostic ``date_ambiguous``/``time_ambiguous``/``missing``/``ambiguous``/
    ``needs`` keys. Never raises.
    """
    body = str(body or "")
    sender = _clean(sender)

    # Title: an explicit labelled line wins; else the (cleaned) subject.
    title = None
    label = _TITLE_LABEL_RE.search(body)
    if label and _clean(label.group(1)):
        title = _bounded(label.group(1), MAX_TITLE_CHARS)
    if not title:
        subj = _LEADING_REPLY_RE.sub("", _clean(subject)).strip()
        title = _bounded(subj, MAX_TITLE_CHARS)

    default_year = _year_from_date_header(date)
    dates, yearless = _collect_dates(body, default_year)
    date_ambiguous = len(dates) > 1
    if date_ambiguous:
        event_date = None
    elif len(dates) == 1:
        event_date = next(iter(dates)) if not yearless else next(iter(dates))
    else:
        event_date = None
    # A single month/day with no year and no header year is NOT reliable.
    if len(dates) == 1 and yearless and default_year is None:
        event_date = None

    ranges, time_ambiguous = _collect_times(body)
    all_day = bool(_ALL_DAY_RE.search(body))
    start = end = None
    if len(ranges) == 1 and not time_ambiguous:
        start, end = next(iter(ranges))

    locations = _collect_locations(body)
    location_ambiguous = len(locations) > 1
    location = None
    if len(locations) == 1:
        location = next(iter(locations))

    missing: list[str] = []
    ambiguous: list[str] = []
    if not title:
        missing.append("title")
    if event_date is None:
        (ambiguous if date_ambiguous else missing).append("date")
    if not all_day:
        if start is None or end is None:
            (ambiguous if time_ambiguous else missing).append("time")
        elif time_ambiguous:
            ambiguous.append("time")

    return {
        "title": title,
        "date": event_date,
        "start": start,
        "end": end,
        "all_day": all_day,
        "location": location,
        "location_ambiguous": location_ambiguous,
        "text": _bounded(body, MAX_TEXT_CHARS),
        "sender": sender or None,
        "date_ambiguous": date_ambiguous,
        "time_ambiguous": time_ambiguous,
        "missing": missing,
        "ambiguous": ambiguous,
        "needs": required_needs({
            "title": title, "date": event_date, "start": start, "end": end,
            "all_day": all_day, "date_ambiguous": date_ambiguous,
            "time_ambiguous": time_ambiguous,
        }),
    }


def required_needs(facts: dict) -> list[str]:
    """The ordered required facts that are missing/ambiguous (may be empty)."""
    needs: list[str] = []
    if not facts.get("title"):
        needs.append("title")
    if not facts.get("date"):
        needs.append("date")
    if not facts.get("all_day"):
        if not (facts.get("start") and facts.get("end")):
            needs.append("time")
    return needs


# ── the pronoun handoff + intent building ──────────────────────────────

def is_add_to_calendar_request(text) -> bool:
    """True for a bounded PRONOUN handoff ("add that to my calendar").

    Deliberately strict: it requires an add/put/save verb over an anaphor
    (that/this/it/the email/...) AND a calendar/schedule word. A plain create
    grammar ("create X on YYYY-MM-DD from ...") is NOT this handoff.
    """
    raw = _clean(text)
    if not raw:
        return False
    if raw.lower().startswith("calendar:"):
        return False
    return bool(_CAL_ADD_ANAPHOR_RE.match(raw))


def need_prompt(facts: dict) -> str:
    """A friendly ask naming ONLY the genuinely missing/conflicting facts."""
    missing = facts.get("missing") or []
    ambiguous = facts.get("ambiguous") or []
    asks: list[str] = []
    if "title" in missing:
        asks.append("a clear event title")
    if "date" in missing:
        asks.append("the event date")
    elif "date" in ambiguous:
        asks.append("which date (the email mentions more than one)")
    if "time" in missing:
        asks.append("the start and end time")
    elif "time" in ambiguous:
        asks.append("which start and end time (the email mentions more than one)")
    if not asks:
        asks.append("a little more detail")
    return ("I can add that to your calendar, but I need "
            + ", ".join(asks) + " first. "
            "Tell me the missing detail and I'll create it.")


def event_intent_args(facts: dict):
    """Return ``(title, date, start_hm, end_hm, all_day)`` or raise fail closed.

    Raises :class:`EmailEventError` carrying ``needs`` when a required fact is
    missing/ambiguous -- nothing is guessed.
    """
    needs = required_needs(facts or {})
    if needs:
        raise EmailEventError(need_prompt(facts or {}), needs=needs)
    title = _bounded(facts.get("title"), MAX_TITLE_CHARS)
    event_date = facts.get("date")
    if not title or not event_date:
        raise EmailEventError(need_prompt(facts or {}), needs=["title", "date"])
    if facts.get("all_day"):
        return (title, event_date, None, None, True)
    start, end = facts.get("start"), facts.get("end")
    if not start or not end:
        raise EmailEventError(need_prompt(facts or {}), needs=["time"])
    return (title, event_date, start, end, False)


def render_details(facts: dict) -> str:
    """A compact, readable rendering of the extracted facts (show-before-ask)."""
    facts = facts or {}
    when = facts.get("date") or "(date needed)"
    if facts.get("all_day"):
        when = f"{when} (all day)"
    elif facts.get("start") and facts.get("end"):
        when = f"{when} {facts['start']}-{facts['end']}"
    lines = [
        f"Event: {facts.get('title') or '(title needed)'}",
        f"When: {when}",
    ]
    if facts.get("location"):
        lines.append(f"Where: {facts['location']}")
    if facts.get("sender"):
        lines.append(f"From: {facts['sender']}")
    return "\n".join(lines)
