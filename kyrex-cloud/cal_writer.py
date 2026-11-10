#!/usr/bin/env python3
"""cal_writer.py — bounded, deterministic Calendar WRITER core.

Pure, dependency-free heart of the Calendar Writer capability: it turns the
OWNER's own text into ONE safe create intent, validates it against a bounded
field set, renders the EXACT payload shown at the confirmation gate, and
formats the safe receipt. Nothing here touches the network or a model.

Boundary (deliberately narrow): title/start/end/all_day and optional location
and description; the
owner's PRIMARY calendar only; America/New_York only. Guests, recurrence,
updates, deletes, attachments, conferencing, reminders,
or arbitrary calendar ids are rejected on the way IN or OUT. An ambiguous or
malformed request fails closed.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CALENDAR_ID = "primary"
TIMEZONE = "America/New_York"
MAX_TITLE_CHARS = 200
REQUIRED_FIELDS = frozenset({"title", "start", "end", "all_day"})
DETAIL_LIMITS = {"location": 500, "description": 2000}
ALLOWED_FIELDS = REQUIRED_FIELDS | frozenset(DETAIL_LIMITS)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):00$")
_MONTH_NAMES = ("january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december")
_MONTHS = {name: n for n, name in enumerate(_MONTH_NAMES, 1)}
_MONTHS.update({name[:3]: n for n, name in enumerate(_MONTH_NAMES, 1)})
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY = "(?:" + "|".join(_WEEKDAYS) + ")"
_EXPLICIT_REQUEST_DATE = (r"(?:\d{4}-\d{2}-\d{2}|(?:"
                 + "|".join(_MONTHS)
                 + r")[.]?\s+\d{1,2}(?:st|nd|rd|th)?(?:(?:,\s*|\s+)\d{4})?)")
_REQUEST_DATE = (r"(?:(?:" + _WEEKDAY + r"(?:,\s*|\s+))?"
                 + _EXPLICIT_REQUEST_DATE + r"|today|tomorrow|(?:next\s+)?" + _WEEKDAY + r")")
_CLOCK = r"\d{1,2}(?:[:.]\d{2}|\d{2}(?=\s*[ap]))?\s*(?:[ap]\.?\s*m\.?)?"
_DURATION = r"(?:for\s+)?(?P<amount>\d+)\s*(?P<unit>minutes?|mins?|hours?|hrs?)"
_DURATION_REPLY = re.compile(
    r"(?:(?:" + _DURATION + r")(?:[,\s]+from\s+(?P<start>" + _CLOCK + r")\s+to\s+(?P<end>" + _CLOCK + r"))?"
    r"|(?:until|to|ends?\s+at)\s+(?P<until>" + _CLOCK + r"))[.!]?", re.I)
_CREATE_OPENER = re.compile(r"(?:add|create)\s+an?\s+(?:new\s+)?(?:calendar\s+)?event"
                            r"(?:\s+(?:to|on|in)\s+(?:my|the)\s+calendar)?[.!]?", re.I)
_ALLDAY_REQUEST_RE = re.compile(
    r"^\s*(?:create|add|schedule|book)\s+(?:an?\s+)?all[\s-]*day\s+"
    r"(?P<title>.+?)(?:\s+event)?\s+on\s+(?P<date>" + _REQUEST_DATE + r")[.]?\s*$",
    re.IGNORECASE,
)
_INTENT_RE = re.compile(
    r"^\s*(?:create|add|schedule|book)\s+(?P<title>.+?)\s+on\s+"
    r"(?P<date>" + _REQUEST_DATE + r")\s+"
    r"(?:"
    r"from\s+(?P<start>" + _CLOCK + r")\s+"
    r"to\s+(?P<end>" + _CLOCK + r")"
    r"|"
    r"at\s+(?P<start2>" + _CLOCK + r")\s+"
    r"for\s+(?P<dur>\d+)\s*(?P<unit>minutes?|mins?|hours?|hrs?)"
    r"|"
    r"(?P<allday>all[\s-]*day)"
    r")[.]?\s*$",
    re.IGNORECASE,
)
_TITLE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TITLE_WRAPPER_RE = re.compile(
    r"^(?:(?:an?\s+)?calendar\s+event(?:\s+(?:titled|called|named))?"
    r"|an?\s+event(?:\s+(?:titled|called|named))?"
    r"|event\s+(?:titled|called|named))\s+",
    re.IGNORECASE,
)
_REMINDER_REFERENCE_RE = re.compile(
    r"(?:set\s+up|set|add|create|schedule|make)\s+(?:an?\s+)?calendar\s+reminder\s+"
    r"for\s+(?:that|this|it)(?:\s+(?:event|drop))?[.!?]?", re.IGNORECASE,
)
_CALENDAR_REFERENCE = (r"(?:add|put|schedule)\s+(?:that|this|it)\s+"
                       r"(?:to|on|in)\s+(?:my|the)\s+calendar")
_CALENDAR_AND_PURCHASE_QUESTION_RE = re.compile(
    _CALENDAR_REFERENCE + r"(?:,?\s+and|[.?])\s+(?:are\s+you\s+able\s+to|can\s+you|could\s+you)\s+"
    r"(?:buy|purchase)\s+(?:that|this|it)(?:\s+for\s+me)?"
    r"(?:\s+when\s+it\s+(?:drops|goes\s+on\s+sale|is\s+released))?[.!?]?", re.I,
)
_CONTEXT_START_RE = re.compile(
    r"(?<!\w)(?P<date>(?:" + _WEEKDAY + r"(?:,\s*|\s+))?"
    + _EXPLICIT_REQUEST_DATE + r")\s+at\s+"
    r"(?P<clock>\d{1,2}(?::\d{2})?\s*[ap]\.?m|\d{1,2}:\d{2})(?!\w)",
    re.IGNORECASE,
)


class CalendarWriterError(Exception):
    """A create request or intent is refused (fail closed, with a clear reason)."""


def _parse_time(token: str) -> str:
    raw = str(token or "").strip().lower().replace(" ", "")
    raw = re.sub(r"([ap])\.?m\.?$", r"\1m", raw)
    m = re.fullmatch(r"(\d{1,2})(?:[:.]?(\d{2}))?(am|pm)?", raw)
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


def _parse_range(start, end):
    start_hm, end_hm = _parse_time(start), _parse_time(end)
    # In "6 to 8 PM", the trailing period applies to the bare hour when
    # that produces a same-period, forward range. Explicit clocks stay exact.
    period = re.search(r"([ap])\.?\s*m\.?\s*$", end, re.I)
    if period and re.fullmatch(r"(?:[1-9]|1[0-2])", start.strip()):
        same_period = _parse_time(start.strip() + period[1] + "m")
        if same_period < end_hm:
            start_hm = same_period
    return start_hm, end_hm


def _valid_date(value: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise CalendarWriterError(f"invalid date {value!r} — use YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise CalendarWriterError(f"invalid calendar date {value!r}")
    return value


def _request_date(value: str, now=None) -> str:
    current = now or datetime.now(ZoneInfo(TIMEZONE))
    if current.tzinfo is not None:
        current = current.astimezone(ZoneInfo(TIMEZONE))
    word = value.lower().strip()
    if word in {"today", "tomorrow"}:
        return (current + timedelta(days=word == "tomorrow")).strftime("%Y-%m-%d")
    dated_weekday = re.fullmatch(
        r"(" + _WEEKDAY + r")(?:,\s*|\s+)(" + _EXPLICIT_REQUEST_DATE + r")", word, re.I)
    if dated_weekday:
        date = _request_date(dated_weekday[2], now)
        if datetime.strptime(date, "%Y-%m-%d").weekday() != _WEEKDAYS.index(dated_weekday[1]):
            raise CalendarWriterError("The weekday and date disagree. Which date should I use?")
        return date
    days = _WEEKDAYS
    if word.removeprefix("next ") in days:
        offset = (days.index(word.removeprefix("next ")) - current.weekday()) % 7
        if word.startswith("next ") and offset == 0:
            offset = 7
        return (current + timedelta(days=offset)).strftime("%Y-%m-%d")
    if _DATE_RE.fullmatch(value):
        return _valid_date(value)
    match = re.fullmatch(
        r"([A-Za-z]+)[.]?\s+(\d{1,2})(?:st|nd|rd|th)?(?:(?:,\s*|\s+)(\d{4}))?",
        value, re.IGNORECASE)
    if not match or match.group(1).lower() not in _MONTHS:
        raise CalendarWriterError("Use an explicit date including the year")
    month = _MONTHS[match.group(1).lower()]
    return _valid_date(f"{int(match.group(3) or current.year):04d}-{month:02d}-{int(match.group(2)):02d}")


class CalendarClarification(CalendarWriterError):
    def __init__(self, question, draft):
        super().__init__(question)
        self.draft = draft


def _request_title(value):
    # Strip only explicit event wrappers, leaving titles such as Event Horizon
    # intact. Apply after parsing the date/time so quoted titles keep their data.
    title = _TITLE_WRAPPER_RE.sub("", value.strip(), count=1).strip()
    if len(title) >= 2 and (title[0], title[-1]) in {('"', '"'), ("“", "”"), ("'", "'"), ("‘", "’")}:
        title = title[1:-1].strip()
    if not title or len(title) > MAX_TITLE_CHARS or _TITLE_CONTROL_RE.search(title):
        raise CalendarWriterError("Use a non-empty event title without control characters (max 200 characters)")
    return title


def request_text(text):
    """Strip bounded conversational wrappers, never instructions in old context."""
    raw = re.sub(r"\bcalender\b", "calendar", str(text or "").strip(), flags=re.I)
    raw = re.sub(r"^this is a new task[.!:]?\s+", "", raw, count=1, flags=re.I)
    for _ in range(3):
        raw = re.sub(r"^(?:(?:can|could|would)\s+(?:you|we)|please|let[’']?s)\s+",
                     "", raw, count=1, flags=re.I)
    return raw


def calendar_and_purchase_question(text):
    """Recognize a calendar reference followed by a purchase capability question."""
    return bool(_CALENDAR_AND_PURCHASE_QUESTION_RE.fullmatch(request_text(text)))


def create_shaped(text):
    return bool(re.match(r"^(?:create|add|schedule|book|reserve|make|set\s+up|put)\b",
                         request_text(text), re.I))


def create_candidate(text):
    """Separate event requests from ordinary conversation before dispatch."""
    raw = re.sub(r"^calendar:\s+", "", str(text or '').strip(), count=1, flags=re.I)
    if create_shaped(raw):
        return True  # Incomplete creates retain their clarification path.
    try:
        intent_from_task(raw)
    except CalendarClarification:
        return True
    except CalendarWriterError:
        return False
    return True


def spoken_create_text(text, draft):
    """Interpret a dictated fragment only in an existing create draft."""
    raw = request_text(text)
    if re.fullmatch(r"i (?:had|add) an event to my calendar[.!]?", raw, re.I):
        return "Add an event to my calendar"
    explicit = create_shaped(raw)
    if not draft and not explicit:
        return text
    year = datetime.now(ZoneInfo(TIMEZONE)).year
    normal = re.sub(r"\bon\s+(\d{1,2})[./](\d{1,2})(?=\s|[.!]?\s*$)",
                    lambda m: f"on {year:04d}-{int(m[1]):02d}-{int(m[2]):02d}", raw, flags=re.I)
    if create_reply(normal, draft):
        return normal if normal != raw else text
    if explicit and normal == raw:
        return text
    candidate = normal if explicit else "Add " + re.sub(r"^(?:at|and)\s+", "", normal, count=1, flags=re.I)
    try:
        title = parse_create_request(candidate)["title"]
    except CalendarClarification as exc:
        if not exc.draft.get("date") or not exc.draft.get("time"):
            return text
        title = exc.draft["title"]
    except CalendarWriterError:
        return text
    if explicit or draft.get("missing") == "title" or title.casefold() == str(draft.get("title", "")).casefold():
        return candidate
    return text


def _natural_timed_request(raw, *, now=None):
    """Accept one complete date/range in common orders, retaining event details.

    The old canonical grammar remains supported. This grammar is anchored to
    the entire owner request; extra dates, guests or recurrence cannot become
    a second effect or disappear while extracting the first appointment.
    """
    clock = _CLOCK
    span = r"(?:at|from)\s+(?P<start>" + clock + r")\s+(?:to|[–-])\s+(?P<end>" + clock + r")"
    day = r"(?P<date>" + _REQUEST_DATE + r")"
    verb = r"(?:create|add|schedule|book)\s+"
    patterns = (
        verb + r"(?:an?\s+)?(?:calendar\s+)?event\s+for\s+" + day + r"\s+" + span + r"\s+for\s+(?P<title>.+?)",
        verb + r"(?P<title>.+?)\s+(?:on\s+)?" + day + r"\s+" + span + r"(?P<details>\s+.+)?",
        verb + r"(?P<title>.+?)\s+" + span + r"\s+(?:on\s+)?" + day + r"(?P<details>\s+.+)?",
    )
    match = next((m for pattern in patterns
                  if (m := re.fullmatch(pattern + r"[.]?", raw, re.I))), None)
    if not match:
        return None
    title = _request_title(match["title"])
    details = (match.groupdict().get("details") or "").strip().rstrip(".")
    # Unquoted 'at <place> for <purpose>' is explicit owner-provided metadata.
    # Quoted names such as "Breakfast at Tiffany's" remain intact.
    if not re.search(r'["“”]', match["title"]):
        place = re.fullmatch(r"(.+?)\s+at\s+(.+)", title, re.I)
        if place:
            if details:
                raise CalendarWriterError("Use one event location and one date/time range.")
            title, details = place[1], "at " + place[2]
    metadata = {}
    if details:
        place = re.fullmatch(r"at\s+(.+?)(?:\s+for\s+(.+))?", details, re.I)
        purpose = re.fullmatch(r"for\s+(.+)", details, re.I)
        if place:
            metadata["location"] = place[1]
            if place[2]:
                metadata["description"] = place[2]
        elif purpose:
            metadata["description"] = purpose[1]
        else:
            raise CalendarWriterError("I could not read the details after the time. Use 'at <location> for <purpose>'.")
    facts = " ".join([title, *metadata.values()])
    extra_date = re.search(_EXPLICIT_REQUEST_DATE, facts, re.I) or re.search(
        r"\b(?:on|and)\s+(?:today|tomorrow|(?:next\s+)?" + _WEEKDAY + r")\b", facts, re.I)
    if re.search(r"\b(?:invite|attendees?|every|recurr(?:ing|ence)|location|description)\b", facts, re.I) or extra_date:
        raise CalendarWriterError("Use one event and one date. Guests and recurrence are not supported for creation.")
    intent = build_intent(title, _request_date(match["date"], now),
                          *_parse_range(match["start"], match["end"]), all_day=False)
    return validate_intent({**intent, **metadata})


def _natural_all_day_request(raw, *, now=None):
    """Separate a trailing date and all-day instruction from the owner's title.

    Accept common date prepositions and a separate "Do all day" sentence.
    Full-request matching and extra-fact checks keep this a single-day create;
    leading title numbers are never interpreted as an event duration.
    """
    match = re.fullmatch(
        r"(?:create|add|schedule|book)\s+(?P<title>.{1,500}?)\s+"
        r"(?:(?:on|for|to)\s+)?(?P<date>" + _REQUEST_DATE + r")"
        r"(?:\s*[.,;]\s*|\s+)(?:(?:do|make\s+it|set\s+it\s+to)\s+)?"
        r"all[\s-]*day(?:\s+event)?[.!]?\s*", raw, re.I)
    if not match:
        return None
    title = _request_title(match['title'])
    if (re.search(_EXPLICIT_REQUEST_DATE, title, re.I)
            or re.search(r"\b(?:invite|attendees?|guests?|every|recurr(?:ing|ence))\b", title, re.I)
            or re.search(r"\b(?:on|and)\s+(?:today|tomorrow|(?:next\s+)?" + _WEEKDAY + r")\b", title, re.I)
            or re.search(r"\b(?:at|from)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", title, re.I)):
        raise CalendarWriterError('Use one event date and either all day or specific times. Guests and recurrence are not supported for creation.')
    return build_intent(title, _request_date(match['date'], now), None, None, all_day=True)


def create_reply(text, draft):
    if not isinstance(draft, dict):
        return False
    missing = draft.get("missing", "duration")
    patterns = {
        "date":r"(?:on\s+)?" + _REQUEST_DATE,
        "time":r"(?:at\s+)?" + _CLOCK + r"|all[ -]*day",
    }
    if missing == "duration":
        return bool(_DURATION_REPLY.fullmatch(str(text or "").strip()))
    return bool(re.fullmatch(patterns.get(missing, r"(?!)") + r"[.!]?", str(text or "").strip(), re.I))


def _finish_create_draft(draft):
    title = draft.get("title")
    if not title:
        missing = "title"
        date_known = bool(draft.get("date"))
        time_known = bool(draft.get("time") or draft.get("all_day"))
        question = {
            (False, False): "What’s the event title, date, and start time?",
            (False, True): "What’s the event title and date?",
            (True, False): "What’s the event title and start time?",
            (True, True): "What’s the event title?",
        }[date_known, time_known]
    elif not draft.get("date"):
        missing, question = "date", f"What date should I use for “{title}”?"
    elif not draft.get("time") and not draft.get("all_day"):
        missing, question = "time", f"What time should “{title}” start, or is it all day?"
    elif not (draft.get("duration") or draft.get("end_time") or draft.get("all_day")):
        missing, question = "duration", f"How long should “{title}” last?"
    else:
        details = {key: draft[key] for key in DETAIL_LIMITS if key in draft}
        if draft.get("all_day"):
            return validate_intent({**build_intent(title, draft["date"], None, None, all_day=True), **details})
        minutes = draft.get("duration")
        start = datetime.strptime(f"{draft['date']} {draft['time']}", "%Y-%m-%d %H:%M")
        if draft.get("end_time"):
            end = datetime.strptime(f"{draft['date']} {draft['end_time']}", "%Y-%m-%d %H:%M")
            if end <= start or (minutes is not None and end - start != timedelta(minutes=minutes)):
                clean = {k: v for k, v in draft.items() if k not in {"end_time", "duration"}}
                raise CalendarClarification("What end time should I use for the saved start?", {**clean, "missing": "duration"})
        else:
            if minutes <= 0:
                raise CalendarWriterError("The duration must be positive")
            end = start + timedelta(minutes=minutes)
        return validate_intent({"title":title,"start":start.strftime("%Y-%m-%dT%H:%M:00"),
                                "end":end.strftime("%Y-%m-%dT%H:%M:00"),"all_day":False, **details})
    raise CalendarClarification(question, {**draft,"missing":missing})


def complete_create_draft(text, pending, *, now=None):
    draft = dict(pending)
    missing = draft.get("missing", "duration")
    raw = str(text or "").strip().rstrip(".!?")
    if missing == "date":
        draft["date"] = _request_date(re.sub(r"^on\s+", "", raw, flags=re.I), now)
    elif missing == "time":
        if re.fullmatch(r"all[ -]*day", raw, re.I):
            draft["all_day"] = True
        else:
            draft["time"] = _parse_time(re.sub(r"^at\s+", "", raw, flags=re.I))
    else:
        duration = _DURATION_REPLY.fullmatch(raw)
        if not duration:
            raise CalendarClarification("How long should this appointment last?", draft)
        amount = duration["amount"]
        minutes = int(amount) * (60 if duration["unit"].lower().startswith("h") else 1) if amount else None
        end = duration["end"] or duration["until"]
        if end:
            start = duration["start"]
            saved = datetime.strptime(draft["time"], "%H:%M")
            if start:
                parsed = datetime.strptime(_parse_time(start), "%H:%M")
                if (parsed != saved and not (not re.search(r"[ap]", start, re.I)
                                             and parsed.hour <= 12
                                             and parsed.hour % 12 == saved.hour % 12
                                             and parsed.minute == saved.minute)):
                    raise CalendarClarification("The range starts at a different time. What end time should I use for the saved start?", draft)
            finish = datetime.strptime(_parse_time(end), "%H:%M")
            actual = int((finish - saved).total_seconds() / 60)
            if actual <= 0 or (minutes is not None and actual != minutes):
                raise CalendarClarification("The duration and time range disagree. What end time should I use?", draft)
            minutes = actual
        draft["duration"] = minutes
    return _finish_create_draft(draft)



def _pasted_email_draft(raw, *, now=None):
    """Extract one owner-pasted mail projection, never the selected old email.

    Header dates supply a year only. Cancellation notice periods and source
    instructions are data, never event durations or authority for extra effects.
    """
    wrapper = re.match(
        r"^(?:add|put|schedule|book|create)\s+(?:this|that|it)(?:\s+one)?\s+"
        r"(?:to|on|in)\s+(?:my|the)\s+calendar\b"
        r"(?:\s+with\s+the\s+time)?\s*[:,\-]?\s*", raw, re.I)
    if not wrapper:
        return None
    source = raw[wrapper.end():]
    projection = re.fullmatch(
        r"(?P<subject>.+?)\s+From:\s*(?P<sender>.+?)\s+"
        r"Received:\s*(?P<received>.+?)\s+Details:\s*(?P<body>.+)",
        source, re.I | re.S)
    if not projection:
        return None
    import email_event
    subject, sender, received, body = projection.groups()
    year = email_event._year_from_date_header(received)
    dates, yearless = email_event._collect_dates(subject + "\n" + body, year)
    if len(dates) > 1:
        raise CalendarWriterError("The pasted email mentions more than one event date. Which date should I use?")
    if yearless and year is None:
        raise CalendarWriterError("What year is this appointment?")
    date = _valid_date(next(iter(dates))) if dates else None
    # Validate dated weekdays as well as the numeric calendar date.
    for match in re.finditer(_WEEKDAY + r"(?:,\s*|\s+)" + _EXPLICIT_REQUEST_DATE,
                             subject + "\n" + body, re.I):
        _request_date(match[0], now=datetime(int(year), 1, 1) if year else now)
    # Keep a descriptive event statement when present; headers are not titles.
    statement = re.match(r"(.+?)\s+is\s+(?:today|tomorrow|on\b|" + _WEEKDAY
                         + r"\b)", body.strip(), re.I)
    title = _request_title(statement[1] if statement else subject)
    # Explicit foreign zones cannot silently become Eastern time.
    if re.search(r"\b(?:PT|PST|PDT|CT|CST|CDT|MT|MST|MDT|UTC|GMT)\b", body, re.I):
        raise CalendarWriterError("This calendar writer uses Eastern time. What is the appointment time in ET?")
    clocks = list(email_event._TIME_RE.finditer(body))
    times = {_parse_time(re.sub(r"[.\s]", "", m[0])) for m in clocks}
    ranges, conflicting = email_event._collect_times(body)
    if conflicting or len(ranges) > 1 or (not ranges and len(times) > 1):
        raise CalendarWriterError("The pasted email mentions more than one time. Which start and end time should I use?")
    draft = {"title": title, "date": date, "time": next(iter(times)) if times else None,
             "duration": None}
    if ranges:
        start, end = next(iter(ranges))
        if times != {start, end}:
            raise CalendarWriterError("The pasted email has conflicting appointment times. Which times should I use?")
        draft["time"] = start
        draft["duration"] = (int(end[:2]) * 60 + int(end[3:])
                             - int(start[:2]) * 60 - int(start[3:]))
    elif len(clocks) == 1:
        # Only "at 4pm for 30 minutes" is an explicit event duration.
        duration = re.match(r"\s*(?:ET|Eastern(?:\s+Time)?)?\s+for\s+"
                            r"(\d+)\s*(minutes?|mins?|hours?|hrs?)\b",
                            body[clocks[0].end():], re.I)
        if duration:
            draft["duration"] = int(duration[1]) * (60 if duration[2].lower().startswith("h") else 1)
    return draft


def _reminder_draft(previous_answer, now):
    """Use a single dated event stated in the preceding substantive answer."""
    if not isinstance(previous_answer, str) or len(previous_answer) > 6000:
        return None
    source = previous_answer.replace("**", "").replace("__", "")
    starts = list(_CONTEXT_START_RE.finditer(source))
    if len(starts) > 1:
        raise CalendarWriterError("The previous answer mentions multiple start times. Which event should I use?")
    if not starts:
        return None
    start = starts[0]
    if not re.search(r"\b\d{4}\b", start["date"]):
        return None
    suffix = source[start.end():]
    if start["clock"].lower().endswith(("a.m", "p.m")):
        suffix = suffix.removeprefix(".")
    eastern = re.match(r"\s+(?:ET|Eastern(?: Time)?)\b", suffix, re.I)
    if eastern:
        suffix = suffix[eastern.end():]
    allowed_suffix = r"^[ \t]*(?:$|[.,;!?(\n—–])" if eastern else r"^[ \t]*(?:$|[.!?\n])"
    if not re.match(allowed_suffix, suffix):
        return None
    # Read the title from the same sentence as the start, excluding later
    # prose such as release history, sources and offers to create an event.
    lead = re.split(r"[\r\n]|(?<=[.!?])\s+", source[:start.start()].strip())[-1]
    lead, anchor_count = re.subn(
        r"\s+(?:(?:(?:will be|takes place|is scheduled|is|starts)\s+)?(?:on|for)|will be|is|starts|—|–)$",
        "", lead, flags=re.I)
    lead = re.sub(r"^(?:the\s+)?next\s+.{1,120}?\s+is\s+(?:the\s+)?", "", lead, flags=re.I)
    if (not anchor_count or not lead or len(lead) > MAX_TITLE_CHARS
            or re.search(r"\b(?:not|was|were|tentative|might|maybe|could|unconfirmed)\b", lead, re.I)
            or re.match(r"^(?:i|we|want me|would you)\b", lead, re.I)
            or re.fullmatch(r"(?:it|this|that|the event|the date)", lead, re.I)):
        return None
    return {"title": _request_title(lead), "date": _request_date(start["date"], now),
            "time": _parse_time(start["clock"].replace(".", "")), "duration": None}


def parse_create_request(text: str, *, now=None, pending=None, previous_answer=None) -> dict:
    """Normalise the OWNER'S text into ONE validated create intent.

    Grammar: ``create <title> on <YYYY-MM-DD> from <HH:MM> to <HH:MM>``,
    ``... at <HH:MM> for <N> minutes|hours``, or ``... all day``. Anything else
    (ambiguous/missing time) raises :class:`CalendarWriterError`. Dates may
    also use an English month name with an explicit year, and all-day requests
    may start with ``add an all-day <title> event on <date>`` or end with
    ``<title> to <date>. Do all day``. Yearless named dates use the current
    Eastern calendar year, which remains visible in the exact event review.
    A calendar-reminder reference can read event facts from ``previous_answer``.
    """
    raw = str(text or "").strip()
    if not raw:
        raise CalendarWriterError("the request is empty")
    raw = re.sub(r"^calendar:\s+", "", raw, count=1, flags=re.IGNORECASE)
    if pending:
        return complete_create_draft(raw, pending, now=now)
    raw = request_text(raw)
    if _CREATE_OPENER.fullmatch(raw):
        return _finish_create_draft({})
    if _REMINDER_REFERENCE_RE.fullmatch(raw) or _CALENDAR_AND_PURCHASE_QUESTION_RE.fullmatch(raw):
        reminder = _reminder_draft(previous_answer, now)
        if reminder is not None:
            return _finish_create_draft(reminder)
        raise CalendarWriterError("Which event should I use? Please include its date and start time.")
    if re.match(_CALENDAR_REFERENCE + r"(?:,?\s+and\b|[.?]\s+\S)", raw, re.I):
        raise CalendarWriterError("Please make the calendar request separately so I can identify one event.")
    pasted = _pasted_email_draft(raw, now=now)
    if pasted is not None:
        return _finish_create_draft(pasted)
    # An inline event after this wrapper supplies its own facts. It must not
    # resolve "this" to an older selected email or become part of the title.
    raw = re.sub(
        r"^(?:add|put|schedule|book|create)\s+(?:this|that|it)\s+"
        r"(?:to|on|in)\s+(?:my|the)\s+calendar\b\s*[:,\-]?\s+(?=\S)",
        "create ", raw, count=1, flags=re.I)
    # The bounded writer already uses America/New_York. Accept explicit names
    # for that zone next to a clock; other zones remain unsupported rather
    # than silently interpreting them as Eastern time.
    raw = re.sub(
        r"(\b(?:at|from|to)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+"
        r"(?:ET|Eastern(?:\s+Time)?|America/New_York)"
        r"(?=\s+(?:for|to)\b|[.!]?\s*$)",
        r"\1", raw, flags=re.I)
    raw = re.sub(r"\s+to (?:the|my) calendar\s+for\s+", " on ", raw, flags=re.I)
    no_end = re.search(r"\s+(?:with\s+)?no end time[.!]?\s*$", raw, re.I)
    if no_end:
        # Preserve the title/date/start as a draft, while declining a
        # contradictory request that also supplies an explicit range.
        parse_create_request(raw[:no_end.start()], now=now)
        raise CalendarWriterError("You gave an end time and also said no end time. Which should I use?")
    raw = re.sub(r"^((?:add|create|schedule|book)\s+.+?)\s+at\s+(" + _CLOCK + r")\s+on\s+("
                 + _REQUEST_DATE + r")(?=\s+for\b|[.!]?\s*$)", r"\1 on \3 at \2", raw, flags=re.I)
    raw = re.sub(r"\s+(?:on\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s+(today|tomorrow)$", r" on \2 at \1", raw, flags=re.I)
    if not create_shaped(raw) and re.search(r"\s+on\s+" + _REQUEST_DATE, raw, re.I):
        raw = "create " + raw
    # Direct Kyrex Chat commands use the reserved namespace; delegated Writer
    # tasks already arrive without it. Strip exactly one prefix before applying
    # the same bounded grammar to both paths.
    raw = re.sub(r"^calendar:\s+", "", raw, count=1, flags=re.IGNORECASE)
    all_day = _ALLDAY_REQUEST_RE.fullmatch(raw)
    if all_day:
        return build_intent(_request_title(all_day.group("title")),
                            _request_date(all_day.group("date"), now),
                            None, None, all_day=True)
    m = _INTENT_RE.match(raw)
    if not m:
        natural_all_day = _natural_all_day_request(raw, now=now)
        if natural_all_day is not None:
            return natural_all_day
        natural = _natural_timed_request(raw, now=now)
        if natural is not None:
            return natural
        partial = re.fullmatch(
            r"(?:create|add|schedule|book)\s+(.+?)"
            r"(?:\s+on\s+(" + _REQUEST_DATE + r"))?"
            r"(?:\s+at\s+(" + _CLOCK + r"))?"
            r"(?:\s+for\s+(\d+)\s*(minutes?|mins?|hours?|hrs?))?[.]?", raw, re.I)
        if partial:
            title, date, clock, amount, unit = partial.groups()
            title = _request_title(title)
            # Do not hide an unsupported extra instruction in a partial title.
            if re.search(r"\b(?:on|at|for|invite|every|location|description)\b", title, re.I):
                raise CalendarWriterError("I couldn't read one event with its date and time. Use 'add <title> on <date> from <start> to <end> at <location> for <purpose>'. Guests and recurrence are not supported for creation.")
            draft = {"title":title, "date":_request_date(date, now) if date else None,
                     "time":_parse_time(clock) if clock else None,
                     "duration":int(amount) * (60 if unit.lower().startswith("h") else 1) if amount else None}
            return _finish_create_draft(draft)
        raise CalendarWriterError("What event, date, and start time should I use? Include an end time or duration, or say all day.")
    title = _request_title(m.group("title"))
    date = _request_date(m.group("date"), now)
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
    return build_intent(title, date, *_parse_range(m.group("start"), m.group("end")), all_day=False)


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
            "title, start, end, all_day, location, and description")
    missing = sorted(REQUIRED_FIELDS - set(intent))
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
    out = {"title": title, "start": start, "end": end, "all_day": all_day}
    for key, limit in DETAIL_LIMITS.items():
        if key not in intent:
            continue
        value = intent[key]
        if not isinstance(value, str) or not value.strip() or len(value) > limit or _TITLE_CONTROL_RE.search(value):
            raise CalendarWriterError(f"{key} must be non-empty text without control characters (max {limit} characters)")
        out[key] = value.strip()
    return out


def load_intent(raw) -> dict:
    if isinstance(raw, dict):
        return validate_intent(raw)
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        raise CalendarWriterError("the create intent is not valid JSON")
    return validate_intent(parsed)


def to_google_event(intent: dict) -> dict:
    """The EXACT reviewed Google event body, with a fixed timezone."""
    intent = validate_intent(intent)
    if intent["all_day"]:
        start = {"date": intent["start"]}
        end = {"date": intent["end"]}
    else:
        start = {"dateTime": intent["start"], "timeZone": TIMEZONE}
        end = {"dateTime": intent["end"], "timeZone": TIMEZONE}
    return {"summary": intent["title"], "start": start, "end": end,
            **{key: intent[key] for key in DETAIL_LIMITS if key in intent}}


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
    ] + [f"{key}: {intent[key]}" for key in DETAIL_LIMITS if key in intent])


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
    details = "".join(f"\n{key.capitalize()}: {intent[key]}" for key in DETAIL_LIMITS if key in intent)
    return f"\u2705 Created \u201c{intent['title']}\u201d \u2014 {_human_when(intent)}{ref}{details}"
