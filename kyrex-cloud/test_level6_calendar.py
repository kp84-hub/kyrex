#!/usr/bin/env python3
"""Focused tests for the Cloud half of the ``level6: calendar`` command.

Covers ONLY this command, against the REAL production code:

  1. the dedicated fail-closed policy — EXACTLY ``cal:list`` and
     ``glofox:read`` at tier 0; wildcards never substitute; extra
     capabilities disqualify; the EXISTING Calendar Reader / Glofox Reader
     presets and predicates are NOT widened;
  2. executor routing — the byte-exact ``level6: calendar`` command selects
     the in-process level6 path and nothing else does, while the EXISTING
     ``level6: weekly`` command is untouched;
  3. America/New_York week selection — the CURRENT Monday-Saturday on
     Monday-Saturday, the NEXT Monday-Saturday on a Sunday;
  4. the explicit event-title/date contract — the response is FILTERED to
     titles beginning exactly with ``Level 6 Workout: ``; unrelated events
     (timed or all-day, inside or outside the selected week) are ignored,
     and among the matching events EXACTLY one non-empty all-day workout
     per date is required — missing dates, duplicate matching, timed
     matching, malformed matching and out-of-window matching events are
     rejected BEFORE any join;
  5. owner isolation — only a Bot holding the EXACT two-operation grant may
     run it; a Calendar Reader or Glofox Reader alone never qualifies;
  6. exact six-date equality with the existing Glofox trusted-date seam, and
     the handler wiring (exact text, bound owner identity, policy denial,
     in-process calendar read through the owner-scoped connector).

Run: python3 test_level6_calendar.py
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

os.environ["KYREX_DATA_DIR"] = tempfile.mkdtemp(prefix="kyrex_l6cal_")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serve  # noqa: E402
import level6_calendar as l6c  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


def expect_error(name, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except l6c.Level6CalendarError as exc:
        print(f"  PASS  {name}  ({exc})")
        return
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"wrong error {type(exc).__name__}: {exc}")
        return
    check(name, False, "expected Level6CalendarError, none raised")


BRANCH_TZ = ZoneInfo("America/New_York")

# ── Fixtures ───────────────────────────────────────────────────────────

WEEK_DATES = [date(2026, 9, 21) + timedelta(days=i) for i in range(6)]
WEEK_ISO = [d.isoformat() for d in WEEK_DATES]
NEXT_ISO = [(d + timedelta(days=7)).isoformat() for d in WEEK_DATES]
WORKOUTS = ["Back Squat", "Front Squat", "Deadlift",
            "Bench Press", "Clean & Jerk", "Snatch"]
TRAINERS = ["Lauren Grabianowski", "Donna Albertone", "Emmitt Terrell",
            "Austin Ross", "Lauren Grabianowski", "Donna Albertone"]

MONDAY = date(2026, 9, 21)
SUNDAY = date(2026, 9, 20)
TUESDAY = date(2026, 9, 22)


def events_for(dates=WEEK_DATES, workouts=WORKOUTS):
    return [
        {"start": {"date": d.isoformat()},
         "summary": f"Level 6 Workout: {workouts[i % len(workouts)]}"}
        for i, d in enumerate(dates)
    ]


def glofox_rows(dates=WEEK_ISO, trainers=TRAINERS):
    return [
        {"date": d, "class_name": "Group Fitness Class", "trainer_id": f"t{i}",
         "trainer_name": trainers[i % len(trainers)], "event_id": f"e{i}"}
        for i, d in enumerate(dates)
    ]


# ══ 1. week selection (America/New_York) ═══════════════════════════════

print("\nTest 1: week selection — current Mon-Sat; next Mon-Sat on Sunday")

out = l6c.select_week_dates(MONDAY)
check("a Monday run selects the CURRENT Monday..Saturday",
      [d.isoformat() for d in out] == WEEK_ISO, f"{out!r}")

out = l6c.select_week_dates(TUESDAY)
check("a midweek run selects the CURRENT (already started) week",
      [d.isoformat() for d in out] == WEEK_ISO, f"{out!r}")

out = l6c.select_week_dates(date(2026, 9, 26))  # Saturday
check("a Saturday run selects the CURRENT week",
      [d.isoformat() for d in out] == WEEK_ISO, f"{out!r}")

out = l6c.select_week_dates(SUNDAY)
check("a Sunday run selects the NEXT Monday..Saturday (the week after)",
      [d.isoformat() for d in out] == WEEK_ISO, f"{out!r}")

# A tz-aware instant at 2026-09-20 23:30 -04:00 is a Sunday in the branch zone.
out = l6c.select_week_dates(datetime(2026, 9, 20, 23, 30, tzinfo=BRANCH_TZ))
check("an aware Sunday instant selects the NEXT week in the branch zone",
      [d.isoformat() for d in out] == WEEK_ISO, f"{out!r}")

tmin, tmax = l6c.week_window(WEEK_DATES)
check("the window is local Monday 00:00 to Sunday 00:00 (-04:00)",
      tmin == "2026-09-21T00:00:00-04:00"
      and tmax == "2026-09-28T00:00:00-04:00", f"{tmin!r} {tmax!r}")


# ══ 2. exact event-title/date contract ═════════════════════════════════

print("\nTest 2: the explicit event contract (one all-day event per date)")

plan = l6c.parse_week_events(events_for(), WEEK_DATES)
check("six exact all-day contract events parse to a six-day plan",
      len(plan.days) == 6 and plan.days[0].workout == "Back Squat",
      f"{plan!r}")

plan = l6c.parse_week_events(
    [{"start": {"date": d.isoformat()}, "summary": "level 6 workout:  x  "}
     for d in WEEK_DATES], WEEK_DATES)
check("title matching is case-insensitive, whitespace-trimmed",
      plan.days[0].workout == "x", f"{plan!r}")

# ── unrelated events are IGNORED (ordinary calendar events never interfere)
# The owner's primary calendar response is a busy, unfiltered list: unrelated
# events — timed OR all-day, INSIDE or OUTSIDE the selected week — must not
# satisfy, duplicate, or distort any workout date.
unrelated_all_day_out = {"start": {"date": "2026-09-14"},
                         "summary": "Team Lunch"}            # all-day, out
unrelated_timed_in = {"start": {"dateTime": "2026-09-21T08:30:00-04:00"},
                      "summary": "Coffee with Ann"}          # timed, in
unrelated_all_day_in = {"start": {"date": "2026-09-22"},
                        "summary": "Team offsite"}           # all-day, in
unrelated_timed_out = {"start": {"dateTime": "2026-09-28T09:00:00-04:00"},
                       "summary": "Flywheel 9:00"}           # timed, out

plan = l6c.parse_week_events(
    [unrelated_all_day_out, unrelated_timed_in, unrelated_all_day_in,
     unrelated_timed_out, *events_for()], WEEK_DATES)
check("ordinary calendar events (timed/all-day, in/out of window) are "
      "IGNORED and never interfere with the six contract events",
      [d.workout for d in plan.days] == WORKOUTS, f"{plan!r}")

# A title that merely CONTAINS the marker is unrelated too (the marker must
# begin the title).
plan = l6c.parse_week_events(
    [{"start": {"date": d.isoformat()},
      "summary": "Prep Level 6 Workout: review photos"}
     for d in WEEK_DATES] + events_for(), WEEK_DATES)
check("a title that only CONTAINS the marker is unrelated and ignored",
      [d.workout for d in plan.days] == WORKOUTS, f"{plan!r}")

evs = events_for()
evs.insert(0, {"start": {"date": "2026-09-21"}, "summary": "Team Lunch"})
plan = l6c.parse_week_events(evs, WEEK_DATES)
check("an unrelated all-day event on a workout date does not interfere",
      plan.days[0].workout == "Back Squat", f"{plan!r}")

evs = events_for()
evs.insert(0, {"start": {"dateTime": "2026-09-21T12:00:00-04:00"},
               "summary": "Team Lunch"})
plan = l6c.parse_week_events(evs, WEEK_DATES)
check("an unrelated TIMED event on a workout date does not interfere",
      plan.days[0].workout == "Back Squat", f"{plan!r}")

evs = events_for()
evs.append({"start": {"date": "2026-09-21"}, "summary": "Team Lunch"})
evs.append({"start": {"date": "2026-09-21"}, "summary": "Another Lunch"})
plan = l6c.parse_week_events(evs, WEEK_DATES)
check("duplicate UNRELATED events on a workout date do not interfere",
      plan.days[0].workout == "Back Squat", f"{plan!r}")

# Missing/empty titles are unrelated and ignored; the MISSING date is what
# fails closed.
evs = events_for()
del evs[0]["summary"]
expect_error("a missing title is unrelated; the missing date fails closed",
             l6c.parse_week_events, evs, WEEK_DATES)

evs = events_for()
evs[0]["summary"] = "   "
expect_error("an EMPTY title is unrelated; the missing date fails closed",
             l6c.parse_week_events, evs, WEEK_DATES)

# ── malformed MATCHING Level 6 events still fail closed ───────────────
evs = events_for()
evs[0]["summary"] = "Level 6 Workout:Back Squat"  # no separator space
expect_error("malformed MATCHING title (no separator space) fails closed",
             l6c.parse_week_events, evs, WEEK_DATES)

evs = events_for()
evs[0]["summary"] = "Level 6 Workout:  "
expect_error("a MATCHING title with an EMPTY workout name fails closed",
             l6c.parse_week_events, evs, WEEK_DATES)

evs = events_for()
evs[0]["start"] = {"dateTime": "2026-09-21T08:30:00-04:00"}
expect_error("a TIMED MATCHING event fails closed (all-day only)",
             l6c.parse_week_events, evs, WEEK_DATES)

evs = events_for()
evs[0]["start"] = {"date": "2026-09-14"}  # previous week
expect_error("a MATCHING event OUTSIDE the six-date window fails closed",
             l6c.parse_week_events, evs, WEEK_DATES)

evs = events_for()
evs[0]["start"] = {"date": "not-a-date"}
expect_error("a MATCHING event with a malformed date fails closed",
             l6c.parse_week_events, evs, WEEK_DATES)

evs = events_for()
evs[0]["start"] = {}
expect_error("a MATCHING event with no usable start fails closed",
             l6c.parse_week_events, evs, WEEK_DATES)

evs = events_for()
evs.append(dict(evs[0]))
expect_error("DUPLICATE MATCHING events on one date fail closed",
             l6c.parse_week_events, evs, WEEK_DATES)

evs = events_for()[:-1]
expect_error("a MISSING date (only 5 contract events) fails closed",
             l6c.parse_week_events, evs, WEEK_DATES)

expect_error("a malformed (non-list) payload fails closed",
             l6c.parse_week_events, {"items": []}, WEEK_DATES)

expect_error("wrong-size weeks are rejected",
             l6c.parse_week_events, events_for(), WEEK_DATES[:-1])

# The contract holds BEFORE any join: a bad calendar never reaches Glofox.
seen = []
try:
    l6c.run_calendar_week(
        calendar_events=lambda time_min, time_max: events_for()[:-1],
        glofox_read=lambda dates: seen.append(dates) or [],
        today=MONDAY)
    check("the calendar contract gates BEFORE the Glofox join", False,
          "no error")
except l6c.Level6CalendarError:
    check("the calendar contract gates BEFORE the Glofox join", seen == [],
          f"glofox was called with {seen!r}")

# A busy realistic primary-calendar response still yields EXACTLY six
# joined lines: the response is FILTERED to the contract events first.
seen = []
out = l6c.run_calendar_week(
    calendar_events=lambda time_min, time_max: [
        unrelated_all_day_out, unrelated_timed_in, unrelated_all_day_in,
        unrelated_timed_out, *events_for()],
    glofox_read=lambda dates: seen.append(dates) or glofox_rows(dates=dates),
    today=MONDAY)
check("a busy primary-calendar response is filtered to the six events",
      len(out) == 6 and out[0].startswith("Monday 2026-09-21"),
      f"{out!r}")


# ══ 3. owner isolation (exact least-privilege grant) ═══════════════════

print("\nTest 3: owner isolation — EXACTLY cal:list + glofox:read, nothing else")

check("preset exactness", serve.LEVEL6_CALENDAR_PRESET == {
    "cal:list": 0, "glofox:read": 0}, f"{serve.LEVEL6_CALENDAR_PRESET!r}")
check("the preset grants its two ops",
      serve.level6_calendar_granted(serve.LEVEL6_CALENDAR_PRESET) is True)
check("preset_policy returns a fresh copy",
      serve.level6_calendar_preset_policy() == serve.LEVEL6_CALENDAR_PRESET
      and serve.level6_calendar_preset_policy()
      is not serve.LEVEL6_CALENDAR_PRESET)

check("cal:list alone (Calendar Reader) is NOT a Level 6 Calendar Bot",
      serve.level6_calendar_granted(serve.CALENDAR_READER_PRESET) is False)
check("glofox:read alone (Glofox Reader) is NOT a Level 6 Calendar Bot",
      serve.level6_calendar_granted(serve.GLOFOX_READER_PRESET) is False)
check("the Level 6 Weekly preset is NOT a Level 6 Calendar Bot",
      serve.level6_calendar_granted(serve.LEVEL6_WEEKLY_PRESET) is False)

for extra, label in [
    ({"cal:list": 0, "glofox:read": 0, "fs:read": 0}, "extra fs:read"),
    ({"cal:list": 0, "glofox:read": 0, "cal:create": 1}, "extra cal:create"),
    ({"cal:list": 0, "glofox:read": 0, "bot:delegate": 0}, "extra delegate"),
    ({"cal:*": 0, "glofox:read": 0}, "wildcard cal:*"),
    ({"*": 0}, "catch-all"),
    ({"cal:list": 1, "glofox:read": 0}, "raised cal:list tier"),
    ({"cal:list": 0}, "missing glofox:read"),
    ({"glofox:read": 0}, "missing cal:list"),
    ({"cal:list": "deny", "glofox:read": 0}, "denied cal:list"),
    (None, "None policy"),
    ("junk", "malformed policy"),
]:
    check(f"no grant for {label}",
          serve.level6_calendar_granted(extra) is False,
          f"{extra!r}")

# The EXISTING presets and gates are untouched.
check("Calendar Reader preset unchanged (cal:list only)",
      serve.CALENDAR_READER_PRESET == {"cal:list": 0}
      and serve.is_calendar_reader_policy(serve.CALENDAR_READER_PRESET))
check("Glofox Reader preset unchanged (glofox:read only)",
      serve.GLOFOX_READER_PRESET == {"glofox:read": 0}
      and serve.is_glofox_reader_policy(serve.GLOFOX_READER_PRESET))
check("Level 6 Weekly preset unchanged (four capture ops)",
      serve.LEVEL6_WEEKLY_PRESET == {
          "browser:navigate": 0, "browser:read": 0,
          "browser:screenshot": 0, "glofox:read": 0}
      and serve.level6_weekly_granted(serve.LEVEL6_WEEKLY_PRESET))
check("the new preset never qualifies the new gates of the others",
      serve.is_calendar_reader_policy(serve.LEVEL6_CALENDAR_PRESET) is False
      and serve.is_glofox_reader_policy(serve.LEVEL6_CALENDAR_PRESET) is False)


# ══ 4. routing ════════════════════════════════════════════════════════

print("\nTest 4: routing — only the byte-exact command selects level6")

check("level6: calendar routes to the in-process level6 branch",
      serve.resolve_executor("level6: calendar")
      == ("level6", "calendar", None))
check("level6: weekly is unchanged",
      serve.resolve_executor("level6: weekly")
      == ("level6", "weekly", None))
for bad in ("level6: calendar now", "level6: calendar?date=2026-09-21",
            "level6: weeklyx", "level6: schedule", "level6: weekly extra"):
    check(f"variant {bad!r} is rejected",
          serve.resolve_executor(bad) == (None, None, "level6"),
          f"{serve.resolve_executor(bad)!r}")

# Case/whitespace normalization of the PREFIX and the request is the same
# semantics the weekly command already has; the BYTE-EXACT text is what the
# Chat route and the submit gates compare, exactly as weekly.
check("prefix case normalizes exactly as weekly does",
      serve.resolve_executor("LEVEL6: calendar")
      == ("level6", "calendar", None))
check("request whitespace normalizes exactly as weekly does",
      serve.resolve_executor("level6:  calendar")
      == ("level6", "calendar", None))
# No separating space / no colon are NOT level6 commands (as with weekly).
check("'level6:calendar' is never the level6 command",
      serve.resolve_executor("level6:calendar") != ("level6", "calendar", None))
check("bare 'level6' is never the level6 command",
      serve.resolve_executor("level6") != ("level6", "calendar", None))


# ══ 5. exact Glofox join ═══════════════════════════════════════════════

print("\nTest 5: exact six-date equality with the pinned Glofox read")

plan = l6c.parse_week_events(events_for(), WEEK_DATES)
lines = l6c.join_week(plan, glofox_rows())
check("six readable dated lines with workout + trainer",
      len(lines) == 6
      and lines[0]
      == "Monday 2026-09-21 — Back Squat — trainer: Lauren Grabianowski",
      f"{lines!r}")

expect_error("no Glofox rows fails closed",
             l6c.join_week, plan, [])
expect_error("a mismatched date fails closed",
             l6c.join_week, plan, glofox_rows(dates=NEXT_ISO))
rows = glofox_rows()
rows[0]["date"] = "2026-09-28"
expect_error("a single out-of-week row fails closed",
             l6c.join_week, plan, rows)
rows = glofox_rows()
rows[0]["trainer_name"] = ""
expect_error("a missing trainer fails closed", l6c.join_week, plan, rows)
rows = glofox_rows()
rows.append(dict(rows[0]))
expect_error("ambiguous (duplicate) rows fail closed",
             l6c.join_week, plan, rows)
expect_error("a malformed row fails closed",
             l6c.join_week, plan, ["junk"])

# The Glofox read is driven by EXACTLY the six calendar-derived dates.
seen = []
out = l6c.run_calendar_week(
    calendar_events=lambda time_min, time_max: events_for(),
    glofox_read=lambda dates: seen.append(dates) or glofox_rows(dates=dates),
    today=MONDAY)
check("the command requests EXACTLY the six calendar dates",
      seen and seen[0] == WEEK_ISO, f"{seen!r}")
check("the command returns six joined lines",
      len(out) == 6 and out[0].startswith("Monday 2026-09-21"),
      f"{out!r}")

# A Sunday run requests the NEXT week's dates (the trusted selection).
seen = []
out = l6c.run_calendar_week(
    calendar_events=lambda time_min, time_max: events_for(),
    glofox_read=lambda dates: seen.append(dates) or glofox_rows(dates=dates),
    today=SUNDAY)
check("a Sunday run requests the NEXT Monday-Saturday (the week after)",
      seen and seen[0] == WEEK_ISO, f"{seen!r}")


# ══ 6. handler wiring ═════════════════════════════════════════════════

print("\nTest 6: the in-process handler (routing, policy denial, relay)")

SENT = []


def _ctx(policy=None, owner="alice", bot_id="l6calbot"):
    return serve.ExecutionContext(
        session_id="sess", policy=serve.LEVEL6_CALENDAR_PRESET
        if policy is None else policy, bot_id=bot_id, bot_owner=owner)


def _send(chat_id, text):
    SENT.append(text)
    return 1


SENT.clear()
serve._run_level6_calendar_task(_ctx(policy={}), 1, "calendar", _send)
check("a Bot without the grant is denied",
      any("denied" in s for s in SENT), f"{SENT!r}")

SENT.clear()
serve._run_level6_calendar_task(_ctx(), 1, "calendar now", _send)
check("a non-exact task text fails closed",
      any("unsupported level6 request" in s for s in SENT), f"{SENT!r}")

SENT.clear()
serve._run_level6_calendar_task(_ctx(owner=""), 1, "calendar", _send)
check("an ownerless context fails closed",
      any("bound Bot with an owner" in s for s in SENT), f"{SENT!r}")

SENT.clear()
captured = []
real_run = l6c.run_calendar_week
try:
    l6c.run_calendar_week = (
        lambda **kw: ["Monday 2026-09-21 — Back Squat — trainer: X"] * 6)
    serve._run_level6_calendar_task(
        _ctx(), 1, "calendar", _send,
        on_result=lambda r: captured.append(r))
finally:
    l6c.run_calendar_week = real_run
check("a granted, bound Bot relays the six lines",
      SENT and "WORKOUT WEEK" in SENT[-1] and SENT[-1].count("\n") == 6,
      f"{SENT!r}")
check("the durable terminal result is captured",
      captured and captured[0]["count"] == 6, f"{captured!r}")

# The in-process calendar read is owner-scoped: the connector is asked for the
# CALLER's owner, and only for the exact workout window.
called = []
real_read = serve._level6_calendar_read_events
real_select = l6c.select_week_dates
try:
    l6c.select_week_dates = lambda today=None: tuple(WEEK_DATES)
    serve._level6_calendar_read_events = (
        lambda owner, time_min, time_max:
        called.append((owner, time_min, time_max)) or [])
    serve._run_level6_calendar_task(_ctx(owner="alice"), 1, "calendar", _send)
finally:
    serve._level6_calendar_read_events = real_read
    l6c.select_week_dates = real_select
check("the connector read is owner-scoped and window-bounded",
      called and called[0][0] == "alice"
      and called[0][1].startswith("2026-09-21T00:00:00-04:00")
      and called[0][2].startswith("2026-09-28T00:00:00-04:00"),
      f"{called!r}")


# ══ summary ═══════════════════════════════════════════════════════════

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)