#!/usr/bin/env python3
"""level6_calendar.py — the Cloud half of the ``level6: calendar`` command.

A sibling of :mod:`level6_weekly` with NO browser and NO OCR: the six workout
dates come from the OWNER's primary Google Calendar, not from a Facebook post.
It does exactly four things, each fail-closed:

1. select the trusted America/New_York workout week: the CURRENT
   Monday-Saturday when *today* is Monday-Saturday, otherwise (a Sunday run)
   the NEXT Monday-Saturday;
2. filter the owner's primary-calendar response to the events whose titles
   begin EXACTLY with ``Level 6 Workout: `` — every other event, timed or
   all-day, inside or outside the selected week, is ignored. Among the
   matching events the explicit contract demands EXACTLY one non-empty
   all-day ``Level 6 Workout: <workout name>`` event on each of the six
   dates; missing dates, duplicate matching events, timed matching events,
   malformed matching events and matching events outside the six-date
   window are rejected BEFORE any join;
3. request the EXISTING Glofox 8:30 AM reader for EXACTLY those six
   calendar-derived dates through the private trusted-date seam
   (``glofox_api._week_0830_classes_for_dates``) — NEVER the reader's own
   clock-driven "next week" window;
4. join calendar dates with Glofox rows on exact six-date equality and return
   six readable dated lines: workout name plus Glofox trainer.

What is deliberately NOT here
-----------------------------
* No browser, no OCR, no image path: the owner creates the six tagged all-day
  events separately through the approval-gated Calendar Writer; this module
  only READS them.
* No clock-driven week selection beyond the fixed rule above, and no year
  inference — every date is a real America/New_York calendar date.
* No caller-controlled surface: no user text, URL, command suffix, calendar
  id, or Glofox branch reaches this module. The calendar read (window bounds)
  and the Glofox read (the six exact dates) are derived ONLY from the
  validated week.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

#: The branch timezone the workout week lives in (matches level6_weekly and
#: the Glofox schedule: America/New_York).
BRANCH_TZ = ZoneInfo("America/New_York")

#: The six class days, in order. Sunday is never part of the week.
WEEKDAYS: tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
)

#: The explicit all-day event-title marker. A title MATCHES when it BEGINS
#: with the marker: ``_TITLE_PREFIX_RE`` decides which events are Level 6
#: candidates and which are unrelated (ignored); ``_TITLE_RE`` then enforces
#: the separator whitespace and the non-empty workout name, so a title that
#: begins with the marker but violates the grammar is a MALFORMED MATCHING
#: event and fails closed.
TITLE_PREFIX = "Level 6 Workout:"

_TITLE_PREFIX_RE = re.compile(r"^\s*Level 6 Workout:", re.IGNORECASE)

_TITLE_RE = re.compile(
    r"^\s*Level 6 Workout:\s+(?P<name>\S.*?)\s*$", re.IGNORECASE,
)

#: The redacted provider-event shape (connectors._calendar_event) carries the
#: title under ``summary`` and the start under ``start`` (``{"date": ...}``
#: for an all-day event, ``{"dateTime": ...}`` for a timed one).
SUMMARY_KEY = "summary"


class Level6CalendarError(Exception):
    """The command cannot produce a trustworthy workout week (fail closed)."""


# ── Week selection ─────────────────────────────────────────────────────

def _reference_day(now=None) -> date:
    """The America/New_York calendar day the week selection uses.

    ``now`` is a test seam only (production passes ``None``): a tz-aware
    datetime is converted to the branch zone, a naive datetime or a plain
    date is taken as-is. Anything else fails closed.
    """
    if now is None:
        return datetime.now(BRANCH_TZ).date()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.date()
        return now.astimezone(BRANCH_TZ).date()
    if isinstance(now, date):
        return now
    raise Level6CalendarError("the reference day is not a calendar date")


def select_week_dates(today=None) -> tuple[date, ...]:
    """The trusted workout week: Monday-Saturday, America/New_York.

    When *today* is Monday-Saturday the week is the CURRENT one (the workout
    week may already be underway — a midweek run still resolves); when today
    is Sunday the week is the NEXT Monday-Saturday. Exactly six consecutive
    dates, Monday through Saturday, in order.
    """
    reference = _reference_day(today)
    monday = reference - timedelta(days=reference.weekday())
    if reference.weekday() == 6:
        # A Sunday run targets the NEXT Monday-Saturday — the week that has
        # not yet started — never the current (already underway) week.
        monday += timedelta(days=7)
    return tuple(monday + timedelta(days=i) for i in range(6))


def week_window(dates) -> tuple[str, str]:
    """(timeMin, timeMax) covering exactly the six workout dates.

    Boundaries are LOCAL America/New_York midnights — Monday 00:00 to the
    following Sunday 00:00 — rendered as ISO-8601 with offset, the same day
    semantics ``calendar_windows`` uses for a "day" query.
    """
    ordered = tuple(dates)
    if len(ordered) != 6:
        raise Level6CalendarError(
            "the workout week must be exactly six dates"
        )
    start = datetime.combine(ordered[0], datetime.min.time(), BRANCH_TZ)
    end = start + timedelta(days=7)
    return start.isoformat(), end.isoformat()


# ── Calendar event contract ────────────────────────────────────────────

@dataclass(frozen=True)
class WorkoutDay:
    """One validated workout day: exact calendar date and workout name."""

    day: date
    workout: str

    @property
    def iso(self) -> str:
        return self.day.isoformat()


@dataclass(frozen=True)
class WorkoutWeek:
    """The validated week: the six dates and their one workout each."""

    days: tuple[WorkoutDay, ...]


def _event_date(event: dict) -> date:
    """The America/New_York calendar date of an all-day event start.

    Returns the date; raises :class:`Level6CalendarError` for a timed event
    (``dateTime``), a malformed/missing start, or a non-ISO date.
    """
    if not isinstance(event, dict):
        raise Level6CalendarError(
            "a calendar event is malformed (not an object)"
        )
    start = event.get("start")
    if not isinstance(start, dict):
        raise Level6CalendarError("a calendar event has no usable start")
    raw = start.get("date")
    if not raw:
        if str(start.get("dateTime") or "").strip():
            raise Level6CalendarError(
                "a workout event is TIMED — only all-day events are accepted"
            )
        raise Level6CalendarError("a calendar event has no usable start")
    text = str(raw).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise Level6CalendarError(
            f"a calendar event has a malformed date {text!r}"
        ) from exc


def _event_workout(event: dict) -> str | None:
    """Classify one event against the Level 6 title contract.

    Returns the workout name when the title MATCHES — it begins EXACTLY with
    the ``Level 6 Workout:`` marker (case insensitive, leading/trailing
    whitespace trimmed) followed by separator whitespace and a non-empty
    workout name. Returns ``None`` for an UNRELATED event — a missing or
    EMPTY title, or a title that does not begin with the marker — which the
    caller IGNORES (timed or all-day, inside or outside the selected week).
    Raises for a MALFORMED MATCHING event: a title that begins with the
    marker but has no separator space (``Level 6 Workout:Back Squat``) or an
    EMPTY workout name (``Level 6 Workout:  ``).
    """
    if not isinstance(event, dict):
        raise Level6CalendarError(
            "a calendar event is malformed (not an object)"
        )
    summary = event.get(SUMMARY_KEY)
    if summary is None or not str(summary).strip():
        return None               # unrelated: no usable title -> ignored
    text = str(summary)
    prefix = _TITLE_PREFIX_RE.match(text)
    if prefix is None:
        return None               # unrelated: does not begin with the marker
    match = _TITLE_RE.match(text)
    if match is None:
        name = text[prefix.end():].strip()
        if not name:
            raise Level6CalendarError(
                f"a Level 6 event title {text!r} carries an EMPTY workout "
                "name"
            )
        raise Level6CalendarError(
            f"a Level 6 event title {text!r} is malformed -- it must begin "
            f"with {TITLE_PREFIX} <workout name> (separator space required)"
        )
    return match.group("name").strip()


def parse_week_events(events, week_dates) -> WorkoutWeek:
    """Filter the primary-calendar response, then validate the six-day week.

    The owner's primary-calendar response is FILTERED to the events whose
    titles begin EXACTLY with ``Level 6 Workout: `` (case insensitive).
    Unrelated events — timed or all-day, inside or outside the selected
    week — are IGNORED: they can never satisfy, duplicate, or distort a
    workout date.

    Fail closed (BEFORE any Glofox join) on:
      * a malformed event record (not an object);
      * a MALFORMED MATCHING event — a title that begins with the marker
        but violates the grammar (no separator space, EMPTY workout name),
        a matching event with a malformed/missing start, or a TIMED
        matching event (only all-day events are accepted);
      * a MATCHING event OUTSIDE the six-date window;
      * a DUPLICATE date — more than one matching event on one date;
      * a MISSING date — one of the six workout dates with no matching
        event.
    """
    if not isinstance(events, list):
        raise Level6CalendarError(
            "the calendar returned a malformed event list"
        )
    expected = tuple(
        d if isinstance(d, date) else date.fromisoformat(str(d))
        for d in week_dates
    )
    if len(expected) != 6 or len(set(expected)) != 6:
        raise Level6CalendarError(
            "the workout week must be exactly six distinct dates"
        )
    expected_set = set(expected)

    by_date: dict[date, list[tuple[dict, str]]] = {d: [] for d in expected}
    for event in events:
        workout = _event_workout(event)
        if workout is None:
            # Unrelated: not a Level 6 event — ignored, no matter its
            # timing (timed or all-day) or its position (inside or outside
            # the selected week).
            continue
        day = _event_date(event)
        if day not in expected_set:
            raise Level6CalendarError(
                f"a Level 6 workout event on {day.isoformat()} is "
                "OUT-OF-WINDOW -- the workout week is exactly the selected "
                f"{expected[0].isoformat()}..{expected[-1].isoformat()} "
                "Monday-Saturday"
            )
        by_date[day].append((event, workout))

    days: list[WorkoutDay] = []
    for day in expected:
        hits = by_date[day]
        if not hits:
            raise Level6CalendarError(
                f"missing workout event on {day.isoformat()} -- every one of "
                "the six dates must carry exactly one all-day "
                f"{TITLE_PREFIX} <workout name> event"
            )
        if len(hits) > 1:
            raise Level6CalendarError(
                f"DUPLICATE workout events on {day.isoformat()}: "
                f"{len(hits)} events -- exactly one all-day event per date "
                "is required"
            )
        days.append(WorkoutDay(day=day, workout=hits[0][1]))

    return WorkoutWeek(days=tuple(days))


# ── Glofox join ────────────────────────────────────────────────────────

def _glofox_date(row: dict) -> date:
    raw = str((row or {}).get("date") or "").strip()
    if not raw:
        raise Level6CalendarError("a Glofox week row carries no date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise Level6CalendarError(
            f"Glofox week row date {raw!r} is not an America/New_York "
            "calendar date"
        ) from exc


def join_week_entries(plan: WorkoutWeek, glofox_rows) -> list[tuple[str, str, str, str]]:
    """Join the six calendar-derived dates with the Glofox reader's dates.

    Returns one ``(weekday, iso, workout, trainer)`` entry per date, in the
    validated Monday-Saturday order. Equality is EXACT (all six calendar
    dates, including the year). The rows are normally the result of reading
    EXACTLY the calendar's dates, so any difference is a fail-closed mismatch
    — a short/extra week or a duplicated date refuses the whole command.

    This is the ONE validation path; :func:`join_week` (the legacy structured
    line list) and :func:`render_week_markdown` (the Kyrex Chat Markdown) both
    render from its output, so the two presentations can never disagree about
    dates, ordering, workout names, trainers, or fail-closed validation.
    """
    if not glofox_rows:
        raise Level6CalendarError("the Glofox week returned no classes")

    by_date: dict[str, dict] = {}
    for row in glofox_rows:
        if not isinstance(row, dict):
            raise Level6CalendarError(
                "the Glofox week returned a malformed row"
            )
        key = _glofox_date(row).isoformat()
        if key in by_date:
            raise Level6CalendarError(f"ambiguous Glofox rows for {key}")
        by_date[key] = row

    week_dates = {entry.iso for entry in plan.days}
    glofox_dates = set(by_date)

    if week_dates != glofox_dates:
        missing = sorted(week_dates - glofox_dates)
        extra = sorted(glofox_dates - week_dates)
        raise Level6CalendarError(
            "the calendar week does not match the Glofox week exactly "
            f"(missing from Glofox: {missing or 'none'}; "
            f"unexpected in Glofox: {extra or 'none'})"
        )

    entries: list[tuple[str, str, str, str]] = []
    for entry in plan.days:
        row = by_date[entry.iso]
        trainer = str(row.get("trainer_name") or "").strip()
        if not trainer:
            raise Level6CalendarError(
                f"the Glofox class on {entry.iso} has no resolvable trainer"
            )
        weekday = WEEKDAYS[entry.day.weekday()]
        entries.append((weekday, entry.iso, entry.workout, trainer))
    return entries


def join_week(plan: WorkoutWeek, glofox_rows) -> list[str]:
    """The legacy structured line list: one ``"{weekday} {iso} — {workout} — trainer: {trainer}"`` line per date.

    Preserved verbatim as the durable ``lines`` result field. The Markdown
    presentation (:func:`render_week_markdown`) is a SEPARATE rendering of the
    same validated entries — this contract is unchanged.
    """
    return [
        f"{weekday} {iso} — {workout} — trainer: {trainer}"
        for weekday, iso, workout, trainer
        in join_week_entries(plan, glofox_rows)
    ]


#: The compact Markdown heading Kyrex Chat renders above the six workout
#: bullets. Mobile-first: a small (level-3) heading, never a wrapping banner,
#: and one tight bullet group instead of six single-newline lines.
WEEK_HEADING = "### 🏋️ Level 6 — Workout Week"


def render_week_markdown(plan: WorkoutWeek, glofox_rows) -> str:
    """Render the joined week as compact, mobile-friendly Kyrex Chat Markdown.

    The Kyrex Chat renderer (react-markdown + remark-gfm) collapses bare
    single-newline lines into ONE paragraph, so the legacy line list showed as
    an unreadable run-on. This renderer emits a small heading plus one bullet
    per day, each bullet carrying a bold ``weekday date``, the workout name,
    and a second ``Trainer:`` line::

        ### 🏋️ Level 6 — Workout Week

        - **Monday 2026-09-21** — Back Squat
          Trainer: Lauren Grabianowski
        - **Tuesday 2026-09-22** — Deadlift
          Trainer: Lauren Grabianowski
        ...

    Two trailing spaces on the workout line create a CommonMark hard break;
    the indented continuation therefore renders ``Trainer:`` on a visibly
    separate line inside the same compact list item. It renders from the SAME
    validated entries as :func:`join_week`, so dates, ordering, workout names,
    trainer data, and fail-closed validation are identical to the legacy
    lines.
    """
    entries = join_week_entries(plan, glofox_rows)
    blocks = [WEEK_HEADING, ""]
    for weekday, iso, workout, trainer in entries:
        # Two trailing spaces create a CommonMark hard break, so Trainer is
        # visibly rendered on its own line inside the same compact list item.
        blocks.append(f"- **{weekday} {iso}** — {workout}  ")
        blocks.append(f"  Trainer: {trainer}")
    return "\n".join(blocks)


# ── Orchestration ──────────────────────────────────────────────────────

def run_calendar_week(*, calendar_events, glofox_read, today=None) -> list[str]:
    """Run the command: select the week, read the calendar, validate, join.

    Order, each fail-closed: select the America/New_York workout week →
    read the owner's primary calendar for EXACTLY that window via
    *calendar_events* (the window and the six dates are derived only from the
    validated week) → FILTER the response to titles beginning exactly with
    ``Level 6 Workout: `` and validate the matching events (unrelated events
    are ignored; missing dates, duplicate matching, timed matching, malformed
    matching and out-of-window matching events are rejected BEFORE any join)
    → read the Glofox 8:30 classes for EXACTLY the six
    calendar-derived dates through *glofox_read* (production wires
    ``glofox_api._week_0830_classes_for_dates``; it never receives a
    recomputed "next week") → join on exact six-date equality.

    *calendar_events* is called with the local-midnight window bounds
    ``(time_min, time_max)``; production wires it to the owner-scoped
    encrypted connector store. ``today`` is an optional reference calendar
    day for the week selection (production passes ``None`` → the current
    America/New_York date).

    Returns ONLY the legacy structured line list; the Kyrex Chat presentation
    uses :func:`run_calendar_week_rendered`, which performs the SAME single
    read and returns both the lines and the Markdown.
    """
    lines, _markdown = run_calendar_week_rendered(
        calendar_events=calendar_events, glofox_read=glofox_read, today=today
    )
    return lines


def run_calendar_week_rendered(
    *, calendar_events, glofox_read, today=None
) -> tuple[list[str], str]:
    """Run the command ONCE and return BOTH presentations of the same week.

    Returns ``(lines, markdown)``: the durable structured line list (the
    ``lines`` result field) and the compact Kyrex Chat Markdown body. The
    calendar and Glofox reads happen EXACTLY once — the two presentations are
    rendered from the one validated :func:`join_week_entries` result, so they
    can never diverge and no read is repeated.
    """
    dates = select_week_dates(today)
    time_min, time_max = week_window(dates)
    events = calendar_events(time_min=time_min, time_max=time_max)
    plan = parse_week_events(events, dates)
    rows = glofox_read([day.iso for day in plan.days])
    lines = join_week(plan, rows)
    markdown = render_week_markdown(plan, rows)
    return lines, markdown