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
MAX_DETAIL_CHARS = 200
#: The most "other short event-specific instructions" surfaced (bounded).
MAX_DETAILS = 4

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


# ── supported event-specific DETAILS ───────────────────────────────────
#
# Only SUPPORTED facts are collected: transportation, cost/payment, a
# permission/form deadline, and other short event-specific instructions. Each
# is a cue-gated sentence/line over the already-bounded body -- deterministic,
# model-free, never a guess. A category with more than one distinct reading is
# reported AMBIGUOUS (value left None), exactly like a date/time conflict, so a
# conflicting detail is SURFACED rather than silently picked.
_TRANSPORT_RE = re.compile(
    r"\b(?:bus(?:es)?|transport(?:ation)?|shuttle|carpool|"
    r"drop[\s-]?off|pick[\s-]?up|charter(?:ed)?\s+bus)\b", re.IGNORECASE)
_COST_RE = re.compile(
    r"(?:\$\s?\d|\b(?:cost|costs|fee|fees|price|prices|payment|prepay|"
    r"donation|tickets?|free)\b)", re.IGNORECASE)
_DEADLINE_RE = re.compile(
    r"\b(?:due|deadline|rsvp|permission\s+slips?|consent\s+forms?|"
    r"register(?:ed)?\s+by|sign[\s-]?up\s+by|no\s+later\s+than)\b",
    re.IGNORECASE)
_INSTRUCTION_RE = re.compile(
    r"\b(?:bring|wear|pack|remember|please|note|chaperone|volunteer|"
    r"snack|lunch|water|sunscreen|supplies|slips?)\b", re.IGNORECASE)

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _sentences(text):
    """Ordered, cleaned, non-empty sentences/lines of *text* (no model)."""
    out = []
    for raw in _SENT_SPLIT_RE.split(str(text or "")):
        s = _clean(raw)
        if s:
            out.append(s)
    return out


def _collect_category(text, rx):
    """Distinct, order-preserving cue sentences for ONE detail category."""
    out, seen = [], set()
    for s in _sentences(text):
        if not rx.search(s):
            continue
        v = _bounded(s, MAX_DETAIL_CHARS)
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _reduce_category(values):
    """``(value, ambiguous)``: a single reading, or None when missing/ambiguous."""
    if len(values) == 1:
        return values[0], False
    if len(values) > 1:
        return None, True
    return None, False


def _collect_details(text):
    """The 'other short event-specific instructions' (bounded, cue-gated).

    Only short sentences that carry an instruction cue and are NOT already a
    transport/cost/deadline line survive -- so "relevant Details" never becomes
    a dump of newsletter boilerplate.
    """
    out = []
    for s in _sentences(text):
        if len(out) >= MAX_DETAILS:
            break
        if (_TRANSPORT_RE.search(s) or _COST_RE.search(s)
                or _DEADLINE_RE.search(s)):
            continue
        if not _INSTRUCTION_RE.search(s):
            continue
        v = _bounded(s, MAX_DETAIL_CHARS)
        if v and v not in out:
            out.append(v)
    return out


# ── target-event LOCALIZATION inside a dense/flattened schedule ────────
#
# A newsletter "IMPORTANT DATES" block flattens many events into one run of
# text ("Oct 2 First Look Friday Oct 2 4th Grade Field Trip Oct 5-9 Spirit
# Week ..."). Feeding that whole block to the extractor makes the TARGET's date
# ambiguous with every neighbour. So, BEFORE extraction, the exact event named
# by the focus anchor is LOCALIZED: the nearest date attached to the strong
# target phrase is found, that ONE entry is isolated from the adjacent entries,
# and only its context (plus the phrase as the title) is extracted. This is a
# pure, model-free reducer -- it never invents a date and FAILS CLOSED when two
# same-strength target entries remain plausible.

#: The least number of strong (non-temporal) terms a target may have before
#: localization is trusted: a lone word is too generic to isolate one entry.
MIN_TARGET_TERMS = 2

#: The bounded window around a localized target entry (characters).
MAX_TARGET_WINDOW = 800
#: The furthest a target's date may sit from its phrase and still be "attached".
MAX_TARGET_DATE_DISTANCE = 240

#: Words that are TEMPORAL (a month or a weekday), never part of a target NAME.
_WEEKDAYS = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri",
    "sat", "sun"})
#: Generic filler that never anchors a target NAME.
_ANCHOR_FILLER = frozenset({
    "the", "a", "an", "of", "in", "on", "to", "for", "with", "about",
    "and", "or", "at", "by", "this", "next", "my", "our", "please"})
#: Function words that ATTACH a following date to the text before it, so a
#: "...due by October 10" deadline stays INSIDE the target entry while a bare
#: "...Field Trip Oct 5" starts a NEW entry.
_DATE_ATTACHERS = frozenset({
    "by", "on", "of", "to", "at", "from", "until", "till", "through",
    "before", "after", "due", "no", "later", "than", "and", "or", "in",
    "for"})


def _target_terms(anchor):
    """The strong, NON-temporal NAME terms of a focus anchor (order kept)."""
    out: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9'&./-]*",
                          str(anchor or "").lower()):
        word = raw.strip(".'&-/")
        if not word or word in out:
            continue
        if word in _MONTHS or word in _WEEKDAYS or word in _ANCHOR_FILLER:
            continue
        if re.fullmatch(r"\d{4}", word):     # a bare year is temporal
            continue
        out.append(word)
    return out


def _phrase_re(terms):
    """A strong (contiguous) target-phrase matcher, or None below the floor."""
    if len(terms) < MIN_TARGET_TERMS:
        return None
    return re.compile(r"\b" + r"[\s\W_]+".join(re.escape(t) for t in terms)
                      + r"\b", re.IGNORECASE)


def _date_spans(text):
    """Every date-like span in *text*, ordered and containment-pruned."""
    spans = []
    for rx in (_ISO_DATE_RE, _US_DATE_RE, _MONTH_DATE_RE):
        spans.extend(m.span() for m in rx.finditer(text))
    spans.sort()
    out: list[tuple] = []
    for s in spans:
        if out and s[0] >= out[-1][0] and s[1] <= out[-1][1]:
            continue
        out.append(s)
    return out


def _span_distance(span, start, end):
    """Character distance from *span* to the ``[start, end)`` match (0 inside)."""
    if span[1] < start:
        return start - span[1]
    if span[0] > end:
        return span[0] - end
    return 0


def _date_identity(text, span):
    """A calendar identity for a date span, so the SAME date groups together.

    Two spellings of one date ("Oct 2" vs "2026-10-02") must not read as two
    plausible target entries; a yearless month/day and a year-bearing one still
    group by month+day.
    """
    piece = text[span[0]:span[1]]
    m = _ISO_DATE_RE.search(piece)
    if m:
        return ("md", int(m.group(2)), int(m.group(3)))
    m = _US_DATE_RE.search(piece)
    if m:
        return ("md", int(m.group(1)), int(m.group(2)))
    m = _MONTH_DATE_RE.search(piece)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return ("md", month, int(m.group(2)))
    return ("raw", piece.strip().lower())


def _date_starts_entry(text, start):
    """True when the date at *start* BEGINS a new entry (not attached)."""
    before = text[:start].rstrip()
    if not before or before[-1] in ".!?;:\n":
        return True
    word = re.search(r"([A-Za-z0-9']+)\s*$", before)
    if not word:
        return True
    return word.group(1).lower() not in _DATE_ATTACHERS


def _target_window(text, match, date, dates):
    """The bounded text of the ONE entry holding *match* and its *date*.

    Left is cut at the attached date (so the PREVIOUS entry's label never leaks
    in) or at the line start; right runs on across an attached deadline date to
    the next ENTRY date, a blank line, or the window ceiling.
    """
    m_start, m_end = match
    if date[0] < m_start:
        lo = date[0]                      # the date leads this entry
    else:
        line_start = text.rfind("\n", 0, m_start) + 1
        prev_end = 0
        for s, e in dates:
            if e <= m_start:
                prev_end = max(prev_end, e)
        lo = max(line_start, prev_end)
    b = max(m_end, date[1])
    hi = min(len(text), b + MAX_TARGET_WINDOW)
    for s, _e in dates:
        if s < b or s >= hi:
            continue
        if _date_starts_entry(text, s):
            hi = s
            break
    blank = re.search(r"\n[ \t]*\n", text[b:hi])
    if blank:
        hi = b + blank.start()
    return text[lo:hi].strip()[:MAX_TARGET_WINDOW].strip()


def _title_case_phrase(text):
    """A deterministic Title Case that leaves digit-led tokens ("4th") alone."""
    out = []
    for word in _clean(text).split(" "):
        if not word:
            continue
        out.append(word if word[0].isdigit()
                   else word[:1].upper() + word[1:])
    return " ".join(out)


def localize_target_event(text, anchor) -> dict:
    """Isolate the ONE target event entry *anchor* names, or fail closed.

    Returns ``{"status", "text", "title"}``:

      * ``"matched"`` -- a single strong entry: *text* is its bounded context
        and *title* is the target phrase (Title Cased), for extraction.
      * ``"ambiguous"`` -- TWO same-strength entries remain plausible (their
        nearest dates differ): do NOT guess -- the caller offers no event.
      * ``"none"`` -- no strong target phrase, so localization does not apply
        and the caller keeps its ordinary section behavior.
    """
    result = {"status": "none", "text": "", "title": ""}
    source = str(text or "")
    rx = _phrase_re(_target_terms(anchor))
    if rx is None or not source:
        return result
    dates = _date_spans(source)
    if not dates:
        return result
    groups: dict = {}
    for m in rx.finditer(source):
        date = min(dates, key=lambda d: _span_distance(d, m.start(), m.end()))
        if _span_distance(date, m.start(), m.end()) > MAX_TARGET_DATE_DISTANCE:
            continue                      # no attached date -> not localized
        key = _date_identity(source, date)
        groups.setdefault(key, []).append(
            (date, m.start(), m.end(), m.group(0)))
    if not groups:
        return result
    if len(groups) > 1:
        result["status"] = "ambiguous"
        return result
    _key, entries = next(iter(groups.items()))
    date, m_start, m_end, phrase = entries[0]
    window = _target_window(source, (m_start, m_end), date, dates)
    if not window:
        return result
    result["status"] = "matched"
    result["text"] = window
    result["title"] = _title_case_phrase(phrase)
    return result


# ── the extractor ──────────────────────────────────────────────────────

def extract_event_facts(*, subject=None, sender=None, date=None, body=None,
                        title=None) -> dict:
    """Reduce ONE message to SAFE event facts plus its missing/ambiguous set.

    Returns a dict with ``title``/``date``/``start``/``end``/``all_day``/
    ``location``/``text`` (each ``None`` when not reliably present) and the
    diagnostic ``date_ambiguous``/``time_ambiguous``/``missing``/``ambiguous``/
    ``needs`` keys. Never raises.
    """
    body = str(body or "")
    sender = _clean(sender)

    # Title: an explicit HINT (the LOCALIZED target phrase) wins; else an
    # explicit labelled line; else the (cleaned) subject.
    resolved = _bounded(title, MAX_TITLE_CHARS) if title else None
    if not resolved:
        label = _TITLE_LABEL_RE.search(body)
        if label and _clean(label.group(1)):
            resolved = _bounded(label.group(1), MAX_TITLE_CHARS)
    if not resolved:
        subj = _LEADING_REPLY_RE.sub("", _clean(subject)).strip()
        resolved = _bounded(subj, MAX_TITLE_CHARS)
    title = resolved

    # A deadline/forms line carries the DEADLINE's date, not the event's: drop
    # those sentences before collecting the event's date/time/location so a
    # "permission slips due Oct 10" line never makes the event date ambiguous.
    event_text = "\n".join(
        s for s in _sentences(body) if not _DEADLINE_RE.search(s))

    default_year = _year_from_date_header(date)
    dates, yearless = _collect_dates(event_text, default_year)
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

    ranges, time_ambiguous = _collect_times(event_text)
    all_day = bool(_ALL_DAY_RE.search(event_text))
    start = end = None
    if len(ranges) == 1 and not time_ambiguous:
        start, end = next(iter(ranges))

    locations = _collect_locations(event_text)
    location_ambiguous = len(locations) > 1
    location = None
    if len(locations) == 1:
        location = next(iter(locations))

    transportation, transportation_ambiguous = _reduce_category(
        _collect_category(body, _TRANSPORT_RE))
    cost, cost_ambiguous = _reduce_category(_collect_category(body, _COST_RE))
    deadline, deadline_ambiguous = _reduce_category(
        _collect_category(body, _DEADLINE_RE))
    details = _collect_details(body)

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
        "transportation": transportation,
        "transportation_ambiguous": transportation_ambiguous,
        "cost": cost,
        "cost_ambiguous": cost_ambiguous,
        "deadline": deadline,
        "deadline_ambiguous": deadline_ambiguous,
        "details": details,
        "conflicts": [],
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


# ── event answer: presentation + bounded same-event enrichment ──────────

#: Short month names for a friendly "Oct 2, 2025" rendering.
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: Scalar facts a SAME-EVENT sibling may fill (never overwrite).
_MERGE_SCALAR_KEYS = ("date", "start", "end", "location", "transportation",
                      "cost", "deadline")


def event_like(facts) -> bool:
    """True when *facts* name a dated event worth presenting/enriching.

    A bare snippet with no date is NOT event-like: the read keeps its ordinary
    rendering rather than inventing an event.
    """
    return bool((facts or {}).get("date"))


def same_event(primary, other) -> bool:
    """True when *other* provably refers to the SAME event as *primary*.

    Deliberately strict: both must carry the SAME explicit date. An undated or
    differently-dated message is never treated as the same event, so a nearby
    unrelated event in another newsletter can never fill the target's facts.
    """
    p = str((primary or {}).get("date") or "")
    o = str((other or {}).get("date") or "")
    return bool(p) and p == o


def _human_date(iso):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", str(iso or ""))
    if not m:
        return str(iso or "")
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not 1 <= month <= 12:
        return str(iso)
    return f"{_MONTH_NAMES[month - 1]} {day}, {year}"


def _human_time(hm):
    m = re.match(r"(\d{1,2}):(\d{2})$", str(hm or ""))
    if not m:
        return str(hm or "")
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return str(hm)
    return f"{hour % 12 or 12}:{minute:02d} {'am' if hour < 12 else 'pm'}"


def merge_event_facts(primary, secondaries):
    """Fill MISSING facts of *primary* from SAME-EVENT *secondaries*.

    The primary (the SELECTED email) is AUTHORITATIVE and is never overwritten.
    A missing scalar is filled ONLY when exactly one same-event sibling offers a
    single, unambiguous value; two different values leave it missing and record
    the key in ``conflicts`` so the caller can SURFACE the ambiguity. Pure and
    bounded -- no model, no network, nothing invented.
    """
    out = dict(primary or {})
    conflicts = [c for c in (out.get("conflicts") or []) if isinstance(c, str)]
    candidates = {k: [] for k in _MERGE_SCALAR_KEYS}
    for other in (secondaries or []):
        if not isinstance(other, dict) or not same_event(out, other):
            continue
        for key in _MERGE_SCALAR_KEYS:
            value = other.get(key)
            if not out.get(key) and value and value not in candidates[key]:
                candidates[key].append(value)
    for key in _MERGE_SCALAR_KEYS:
        values = candidates[key]
        if out.get(key) or not values:
            continue
        if len(values) == 1:
            out[key] = values[0]
        elif key not in conflicts:
            conflicts.append(key)
    if not out.get("details"):
        for other in (secondaries or []):
            if (isinstance(other, dict) and same_event(out, other)
                    and other.get("details")):
                out["details"] = list(other["details"])[:MAX_DETAILS]
                break
    out["conflicts"] = conflicts
    out["needs"] = required_needs(out)
    return out


def render_event_answer(facts) -> str:
    """A COMPACT event answer: "Title — Date", then Time, Location, Details.

    Missing time/location are stated EXPLICITLY (never filled from boilerplate),
    and a fact that conflicts across the emails is surfaced as ambiguous.
    """
    facts = facts or {}
    conflicts = facts.get("conflicts") or []
    lines = [
        f"{facts.get('title') or '(no title)'} \u2014 "
        f"{_human_date(facts.get('date')) or '(date needed)'}"
    ]
    if facts.get("all_day"):
        lines.append("Time: all day")
    elif facts.get("start") and facts.get("end"):
        lines.append(f"Time: {_human_time(facts['start'])} \u2013 "
                     f"{_human_time(facts['end'])}")
    elif (facts.get("time_ambiguous") or "time" in conflicts
          or "start" in conflicts or "end" in conflicts):
        lines.append("Time: conflicting across the emails \u2014 please confirm")
    else:
        lines.append("Time: not found")
    if facts.get("location"):
        lines.append(f"Location: {facts['location']}")
    elif facts.get("location_ambiguous") or "location" in conflicts:
        lines.append("Location: conflicting across the emails \u2014 "
                     "please confirm")
    else:
        lines.append("Location: not found")
    details: list[str] = []
    for key in ("deadline", "cost", "transportation"):
        if facts.get(key) and facts[key] not in details:
            details.append(facts[key])
    for detail in (facts.get("details") or []):
        if detail not in details:
            details.append(detail)
    if details:
        lines.append("Details:")
        lines.extend(f"  - {detail}" for detail in details)
    return "\n".join(lines)
