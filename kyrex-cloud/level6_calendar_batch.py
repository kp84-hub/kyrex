"""Six reviewed all-day workouts from a verified week or an explicit pasted list.

Pasted lists are owner-provided evidence, never independently verified facts.
Both paths use the owner's preferred calendar and the existing approval gate.
"""
from __future__ import annotations

import re
import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import cal_writer

TZ = ZoneInfo("America/New_York")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
TITLE_PREFIX = "Level 6 Workout: "
PASTED_TASK_PREFIX = 'create-pasted-weekly-calendar '
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


def pasted_week(text):
    """Recognize the explicit add request plus six dated pasted workout rows.

    Whitespace may have been flattened by copying a message on a phone.
    No title, date, trainer or time is supplied by a model or inferred here.
    """
    if not isinstance(text, str) or len(text) > 6000:
        return None
    match = re.fullmatch(
        r"(?is)(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
        r"(?:add|put|save|schedule)\s+(?:these|those|the)(?:\s+workouts)?\s+"
        r"(?:to|on|in)\s+my\s+calendar[.!?]?\s+"
        r"(?:```(?:text)?\s*)?#L6Workout\s+(?:🏋[^\w]*)?"
        r"Level\s+6\s*[—–-]\s*Workout\s+Week\s+(.+?)(?:\s*```)?\s*", text.strip())
    if not match:
        return None
    body = re.sub(r'\s+', ' ', match[1]).strip()
    starts = list(re.finditer(r'\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)\s+\d{4}-\d{2}-\d{2}\s+[—–-]\s+', body))
    if len(starts) != 6 or starts[0].start() != 0:
        raise CalendarBatchError('The pasted workout week needs six dated rows, Monday through Saturday.')
    lines = []
    for index, start in enumerate(starts):
        row = body[start.start():starts[index + 1].start() if index < 5 else len(body)].strip()
        parsed = re.fullmatch(
            r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)\s+(\d{4}-\d{2}-\d{2})\s+[—–-]\s+'
            r'(.{1,200}?)\s+(?:[—–-]\s*)?[Tt]rainer:\s+([\w .,&\x27/()-]{1,100})', row)
        if not parsed:
            raise CalendarBatchError('Each pasted row needs a weekday, date, workout name and trainer; class times are not inferred.')
        lines.append(f'{parsed[1]} {parsed[2]} — {parsed[3].strip()} — trainer: {parsed[4].strip()}')
    intents_from_week(lines)
    return lines


def pasted_task(text):
    lines = pasted_week(text)
    return PASTED_TASK_PREFIX + json.dumps({'source': 'user_pasted', 'lines': lines}, ensure_ascii=False, sort_keys=True) if lines else None


def pasted_intents_from_task(text):
    if not isinstance(text, str) or not text.startswith(PASTED_TASK_PREFIX) or len(text) > 6000:
        raise CalendarBatchError('Invalid pasted workout calendar task.')
    try:
        value = json.loads(text[len(PASTED_TASK_PREFIX):])
        if not isinstance(value, dict) or set(value) != {'source', 'lines'} or value['source'] != 'user_pasted':
            raise ValueError()
        return intents_from_week(value['lines'])
    except (ValueError, TypeError, KeyError) as exc:
        raise CalendarBatchError('Invalid pasted workout calendar rows.') from exc


def is_add_request(text: str) -> bool:
    """Recognize one bounded natural request on the unified Calendar Bot."""
    return bool(_REQUEST.fullmatch(str(text or "").strip()))


def is_workout_followup(text: str, messages) -> bool:
    """Resolve a bounded calendar pronoun against the latest assistant week.

    This selects a workflow, never event facts. The executor still re-reads
    authoritative workouts and shows exact dates/titles at its approval gate.
    A newer email/other answer blocks reuse of an older workout preview.
    """
    if not re.fullmatch(
            r"(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
            r"(?:add|put|save|schedule)\s+(?:those|these|them|that|this|it)"
            r"(?:\s+workouts)?\s+(?:to|on|in)\s+my\s+calendar[.!?]?",
            str(text or "").strip(), re.IGNORECASE):
        return False
    if not isinstance(messages, list):
        return False
    for message in reversed(messages[-12:]):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        # Permit retry after either known erroneous calendar handoff response.
        if content.startswith(("I don't have a selected email to add.",
                               "I could not read that as a calendar request.")):
            continue
        # Coordinators summarize the same result as ordinary bullets, often
        # without the renderer's heading. Recognize the Level 6 reference plus
        # a complete dated week rather than requiring one presentation title.
        if not re.search(r"\bLevel\s*6\b|#L6Workout\b", content, re.I):
            return False
        rows = re.findall(
            r"(?:^|\n)(?:[-*•]\s+)?(?:\*\*)?"
            r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday) "
            r"(\d{4}-\d{2}-\d{2})(?:\*\*)? — [^\n]+", content)
        if len(rows) != 6 or [row[0] for row in rows] != list(WEEKDAYS):
            return False
        try:
            days = [date.fromisoformat(row[1]) for row in rows]
        except ValueError:
            return False
        return days[0].weekday() == 0 and all(
            day == days[0] + timedelta(days=i) for i, day in enumerate(days))
    return False


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
