#!/usr/bin/env python3
"""Focused tests for the Cloud half of the ``level6: weekly`` command.

Covers ONLY this command, against the REAL production code:

  1. the dedicated fail-closed policy — EXACTLY ``browser:navigate``,
     ``browser:read``, ``browser:screenshot`` and ``glofox:read`` at tier 0;
     wildcards never substitute; extra capabilities disqualify; the EXISTING
     Browser Bot / Glofox Reader presets and predicates are NOT widened;
  2. executor routing — the byte-exact ``level6: weekly`` command selects the
     in-process level6 path and nothing else does;
  3. the fixed-purpose task spec (pinned page, no URL/date argument);
  4. explicit ``WEEK OF MM.DD.YY`` parsing with its PRINTED year, exactly six
     Monday-Saturday dated rows, and every ambiguity/staleness rejection
     (no printed year, wrong weekday, duplicated/missing days, disagreeing
     row dates/years, multiple markers or labels, truncated OCR);
  5. the host-response contract — only structured OCR text + post metadata is
     accepted; page text (``final_response``), artifact lists and ``.png``
     paths are refused; the not-yet-published week is reported clearly;
  6. week equality with the existing Glofox reader, and the handler wiring
     (exact text, bound owner identity, policy denial, browser-bot dispatch
     identity that preserves the caller's owner).

Nothing here mocks DOM text as if it were screenshot OCR: the only OCR text
used is the host's structured ``level6_weekly.ocr_text`` field, which the host
produces from a local Tesseract run over the captured post element (covered by
browser-host/test_level6_post.py).

Run: python3 test_level6_weekly.py
"""
import inspect
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from unittest.mock import patch

os.environ["KYREX_DATA_DIR"] = tempfile.mkdtemp(prefix="kyrex_level6_")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serve  # noqa: E402
import glofox_api  # noqa: E402
import level6_weekly as l6  # noqa: E402
import task_store  # noqa: E402

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
    except l6.Level6Error as exc:
        print(f"  PASS  {name}  ({exc})")
        return
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"wrong error {type(exc).__name__}: {exc}")
        return
    check(name, False, "expected Level6Error, none raised")


# ── Fixtures ───────────────────────────────────────────────────────────

OCR_TEXT = "\n".join([
    "Level 6 Training",
    "THE WEEKLY SIX",
    "WEEK OF 09.21.26",
    "MONDAY 09.21 Back Squat",
    "TUESDAY 09.22 Front Squat",
    "WEDNESDAY 09.23 Deadlift",
    "THURSDAY 09.24 Bench Press",
    "FRIDAY 09.25 Clean & Jerk",
    "SATURDAY 09.26 Snatch",
])

WEEK_DATES = ["2026-09-21", "2026-09-22", "2026-09-23",
              "2026-09-24", "2026-09-25", "2026-09-26"]
NEXT_WEEK_DATES = ["2026-09-28", "2026-09-29", "2026-09-30",
                   "2026-10-01", "2026-10-02", "2026-10-03"]
PREV_WEEK_DATES = ["2026-09-14", "2026-09-15", "2026-09-16",
                   "2026-09-17", "2026-09-18", "2026-09-19"]
TRAINERS = ["Lauren Grabianowski", "Donna Albertone", "Emmitt Terrell",
            "Austin Ross", "Lauren Grabianowski", "Donna Albertone"]

#: Reference days for the plausibility gate (America/New_York calendar days).
MIDWEEK = date(2026, 9, 23)            # Wednesday INSIDE the WEEK_DATES week
CURRENT_WEEK_WED = date(2026, 9, 16)   # Wednesday INSIDE the PREV_WEEK week
SUNDAY_BEFORE = date(2026, 9, 13)      # Sunday before the PREV_WEEK starts
STALE_TODAY = date(2026, 10, 5)        # two weeks AFTER the PREV_WEEK ended

#: An OCR post for the week of 2026-09-14 (Mon) .. 2026-09-19 (Sat). Its week
#: has already STARTED by ``CURRENT_WEEK_WED`` but it is still the NEWEST post.
OCR_CURRENT = "\n".join([
    "Level 6 Training",
    "THE WEEKLY SIX",
    "WEEK OF 09.14.26",
    "MONDAY 09.14 Back Squat",
    "TUESDAY 09.15 Front Squat",
    "WEDNESDAY 09.16 Deadlift",
    "THURSDAY 09.17 Bench Press",
    "FRIDAY 09.18 Clean & Jerk",
    "SATURDAY 09.19 Snatch",
])


def glofox_rows(dates=WEEK_DATES, trainers=TRAINERS):
    return [
        {"date": d, "class_name": "Group Fitness Class", "trainer_id": f"t{i}",
         "trainer_name": trainers[i % len(trainers)], "event_id": f"e{i}"}
        for i, d in enumerate(dates)
    ]


def host_ok(text=OCR_TEXT, truncated=False, **overrides):
    payload = {
        "marker": "THE WEEKLY SIX",
        "page_url": l6.FACEBOOK_PAGE_URL,
        "post_ref": "a1b2c3d4",
        "permalink": "https://www.facebook.com/level6training/posts/1",
        "ocr_engine": "tesseract",
        "ocr_text": text,
        "ocr_truncated": truncated,
    }
    payload.update(overrides)
    return {"status": "no_changes", "final_response": "", "browser_artifacts": [],
            "errors": [], "level6_weekly": payload}


def run_weekly(result=None, glofox=None, dispatch_error=None, today=MIDWEEK):
    seen = {}

    def dispatch(spec):
        seen["spec"] = spec
        if dispatch_error is not None:
            return None, dispatch_error
        return result, None

    inner = glofox if glofox is not None else (lambda dates: glofox_rows())

    def reader(dates):
        seen["dates"] = list(dates)
        return inner(dates)

    lines = l6.run_weekly(dispatch=dispatch, glofox_read=reader, today=today)
    return lines, seen


# ══ 1. dedicated fail-closed policy ═══════════════════════════════════

print("\nTest 1: the dedicated Level 6 Weekly policy grant")

check("preset is exactly the four pinned tier-0 ops",
      serve.LEVEL6_WEEKLY_PRESET == {
          "browser:navigate": 0, "browser:read": 0,
          "browser:screenshot": 0, "glofox:read": 0},
      f"preset={serve.LEVEL6_WEEKLY_PRESET!r}")
check("the dedicated preset IS a Level 6 Weekly grant",
      serve.level6_weekly_granted(serve.LEVEL6_WEEKLY_PRESET) is True)
check("preset copy is a fresh equal dict",
      serve.level6_weekly_preset_policy() == serve.LEVEL6_WEEKLY_PRESET
      and serve.level6_weekly_preset_policy() is not serve.LEVEL6_WEEKLY_PRESET)

print("\nTest 2: wildcards, missing ops and extra capabilities fail closed")
check("browser:/glofox: wildcards are not the grant",
      serve.level6_weekly_granted({"browser:*": 0, "glofox:*": 0}) is False)
check("* catch-all is not the grant",
      serve.level6_weekly_granted({"*": 0}) is False)
check("a missing exact op (screenshot) fails closed",
      serve.level6_weekly_granted({
          "browser:navigate": 0, "browser:read": 0, "glofox:read": 0}) is False)
for extra in ({"browser:click": 0}, {"browser:type": 1}, {"fs:read": 0},
              {"fs:write": 1}, {"repo:push": 2}, {"bot:delegate": 0}):
    policy = dict(serve.LEVEL6_WEEKLY_PRESET)
    policy.update(extra)
    check(f"extra {list(extra)[0]} disqualifies",
          serve.level6_weekly_granted(policy) is False)
check("raised tier on a granted op disqualifies",
      serve.level6_weekly_granted({
          "browser:navigate": 0, "browser:read": 0,
          "browser:screenshot": 1, "glofox:read": 0}) is False)
check("malformed policy fails closed",
      serve.level6_weekly_granted({"browser:screenshot": "yes"}) is False)
check("empty policy fails closed", serve.level6_weekly_granted({}) is False)

print("\nTest 3: the EXISTING presets are NOT widened")
check("Browser preset unchanged",
      serve.BROWSER_PRESET == {"browser:navigate": 0, "browser:read": 0,
                               "glofox:read": 0})
check("Browser preset is still a Browser Bot",
      serve.is_browser_bot_policy(serve.BROWSER_PRESET) is True)
check("Glofox Reader preset unchanged",
      serve.GLOFOX_READER_PRESET == {"glofox:read": 0})
check("Glofox Reader preset is still a Glofox Reader",
      serve.is_glofox_reader_policy(serve.GLOFOX_READER_PRESET) is True)
check("Browser preset is NOT a Level 6 Weekly grant",
      serve.level6_weekly_granted(serve.BROWSER_PRESET) is False)
check("Glofox Reader preset is NOT a Level 6 Weekly grant",
      serve.level6_weekly_granted(serve.GLOFOX_READER_PRESET) is False)
check("Level 6 Weekly preset is NOT a Browser Bot",
      serve.is_browser_bot_policy(serve.LEVEL6_WEEKLY_PRESET) is False)
check("Level 6 Weekly preset is NOT a Glofox Reader",
      serve.is_glofox_reader_policy(serve.LEVEL6_WEEKLY_PRESET) is False)


# ══ 2. routing ════════════════════════════════════════════════════════

print("\nTest 4: routing — only the byte-exact command selects level6")
check("constant is the byte-exact command",
      serve.LEVEL6_TASK_TEXT == "level6: weekly")
check("exact command routes to level6",
      serve.resolve_executor("level6: weekly") == ("level6", "weekly", None))
check("suffixed command is rejected",
      serve.resolve_executor("level6: weekly now") == (None, None, "level6"))
check("sibling task word is rejected",
      serve.resolve_executor("level6: schedule") == (None, None, "level6"))
check("no URL/date surface exists in the command",
      all(t in (None, "level6", "weekly")
          for t in serve.resolve_executor("level6: weekly?url=https://evil.example/")))
check("glofox routing unchanged",
      serve.resolve_executor("glofox: schedule") == ("glofox", "schedule", None))


# ══ 3. task spec ═════════════════════════════════════════════════════

print("\nTest 5: the fixed-purpose task spec")
spec = json.loads(l6.weekly_browser_task_spec())
check("spec is the level6_weekly flag + pinned url",
      spec == {"level6_weekly": True, "url": "https://www.facebook.com/level6training/"},
      f"spec={spec!r}")
check("spec carries no date", not any("date" in str(k).lower() for k in spec))


# ══ 4. parsing ════════════════════════════════════════════════════════

print("\nTest 6: explicit WEEK OF MM.DD.YY parsing with the PRINTED year")
six = l6.parse_ocr_text(OCR_TEXT)
check("six days parsed", len(six.days) == 6)
check("the printed year is used (2026), never inferred",
      [d.iso for d in six.days] == WEEK_DATES, f"{[d.iso for d in six.days]}")
check("weekday order is Monday..Saturday",
      [d.weekday for d in six.days] == list(l6.WEEKDAYS))
check("workouts parsed in order",
      [d.workout for d in six.days] ==
      ["Back Squat", "Front Squat", "Deadlift", "Bench Press",
       "Clean & Jerk", "Snatch"])
check("label captured", "09.21.26" in six.week_label)

check("4-digit printed year parses too",
      l6.parse_ocr_text(OCR_TEXT.replace("09.21.26", "09.21.2026")).days[0].iso
      == "2026-09-21")
check("row dates may carry a matching printed year",
      len(l6.parse_ocr_text(
          OCR_TEXT.replace("MONDAY 09.21 Back Squat",
                           "MONDAY 09.21.26 Back Squat")).days) == 6)

expect_error("truncated OCR is rejected",
             l6.parse_ocr_text, OCR_TEXT, truncated=True)
expect_error("empty OCR text is rejected", l6.parse_ocr_text, "   ")
expect_error("a missing marker is rejected",
             l6.parse_ocr_text, OCR_TEXT.replace("THE WEEKLY SIX", "SOMETHING ELSE"))
expect_error("a second marker (multiple posts) is rejected",
             l6.parse_ocr_text, OCR_TEXT + "\nTHE WEEKLY SIX")
expect_error("a missing WEEK OF label is rejected",
             l6.parse_ocr_text, OCR_TEXT.replace("WEEK OF 09.21.26", ""))
expect_error("a label WITHOUT a printed year is rejected (no inference)",
             l6.parse_ocr_text, OCR_TEXT.replace("WEEK OF 09.21.26", "WEEK OF 09.21"))
expect_error("a label that is not a Monday is rejected",
             l6.parse_ocr_text, OCR_TEXT.replace("WEEK OF 09.21.26", "WEEK OF 09.23.26"))
expect_error("two WEEK OF labels are rejected (ambiguous)",
             l6.parse_ocr_text, OCR_TEXT + "\nWEEK OF 09.28.26")
expect_error("five rows are rejected",
             l6.parse_ocr_text, "\n".join(OCR_TEXT.splitlines()[:-1]))
expect_error("a duplicated weekday is rejected",
             l6.parse_ocr_text, OCR_TEXT.replace("TUESDAY 09.22", "MONDAY 09.21"))
expect_error("a row date disagreeing with the printed week is rejected",
             l6.parse_ocr_text, OCR_TEXT.replace("TUESDAY 09.22", "TUESDAY 09.23"))
expect_error("a row year disagreeing with the label year is rejected",
             l6.parse_ocr_text,
             OCR_TEXT.replace("MONDAY 09.21 Back Squat",
                              "MONDAY 09.21.25 Back Squat"))
expect_error("a non-consecutive week (label/week mismatch) is rejected",
             l6.parse_ocr_text, OCR_TEXT.replace("WEEK OF 09.21.26", "WEEK OF 09.14.26"))


# ══ 5. week equality + host contract ══════════════════════════════════

print("\nTest 7: exact week equality with the Glofox reader")
lines = l6.join_week(six, glofox_rows())
check("exact equality produces six dated lines", len(lines) == 6)
check("lines carry date, workout and trainer",
      lines[0] == "Monday 2026-09-21 — Back Squat — trainer: Lauren Grabianowski",
      f"line0={lines[0]!r}")

expect_error("a Glofox week AHEAD of the post is rejected (upcoming week not yet published)",
             l6.join_week, six, glofox_rows(dates=NEXT_WEEK_DATES))
expect_error("a Glofox week BEHIND the post is rejected (mismatch)",
             l6.join_week, six, glofox_rows(dates=PREV_WEEK_DATES))
expect_error("a missing Glofox date is rejected",
             l6.join_week, six, glofox_rows(dates=WEEK_DATES[:-1]))
expect_error("an extra Glofox date is rejected",
             l6.join_week, six, glofox_rows(dates=WEEK_DATES + ["2026-09-27"]))
expect_error("a duplicate Glofox date is rejected (ambiguous)",
             l6.join_week, six, glofox_rows(dates=WEEK_DATES + ["2026-09-23"]))
expect_error("a trainer-less Glofox row is rejected",
             l6.join_week, six,
             glofox_rows(trainers=[""] + TRAINERS[1:]))
expect_error("an empty Glofox week is rejected", l6.join_week, six, [])

print("\nTest 8: run_weekly consumes ONLY the host's structured contract")
out, seen = run_weekly(result=host_ok())
check("happy path returns six lines", len(out) == 6, f"{out!r}")
check("the pinned spec was dispatched",
      json.loads(seen["spec"]) == {"level6_weekly": True,
                                   "url": l6.FACEBOOK_PAGE_URL})

expect_error("dispatch failure fails closed",
             run_weekly, dispatch_error="no Browser Host is bound")
expect_error("a non-dict host result fails closed",
             run_weekly, result="not-a-dict")
expect_error("a missing level6 payload fails closed",
             run_weekly, result={"status": "no_changes", "errors": []})
expect_error("post_not_available surfaces 'weekly post not available'",
             run_weekly,
             result={"status": "error", "final_response": "", "errors": [],
                     "browser_artifacts": [],
                     "level6_weekly": {"error_code": "post_not_available"}})
def _na_phrase():
    try:
        run_weekly(result={"status": "error", "errors": [],
                           "browser_artifacts": [],
                           "level6_weekly": {"error_code": "post_not_available"}})
    except l6.Level6Error as exc:
        return str(exc)
    return ""


check("the not-available failure says exactly 'weekly post not available'",
      _na_phrase() == "weekly post not available", f"{_na_phrase()!r}")

expect_error("a host error status fails closed",
             run_weekly,
             result={"status": "error", "errors": ["browser.read denied"],
                     "browser_artifacts": [], "level6_weekly": {}})
expect_error("truncated OCR from the host fails closed",
             run_weekly, result=host_ok(truncated=True))
expect_error("missing OCR text fails closed",
             run_weekly, result=host_ok(text=""))
expect_error("DOM page text in final_response is refused",
             run_weekly,
             result=dict(host_ok(), final_response="MONDAY 09.21 Back Squat"))
expect_error("an artifact path is refused",
             run_weekly,
             result=dict(host_ok(), browser_artifacts=["browser-artifacts/x.png"]))
expect_error("a .png reference in the payload is refused",
             run_weekly, result=host_ok(ocr_text=OCR_TEXT, permalink="a.png"))
expect_error("an empty Glofox week fails closed",
             run_weekly, result=host_ok(), glofox=lambda dates: [])
expect_error("a mismatched Glofox week fails closed",
             run_weekly, result=host_ok(),
             glofox=lambda dates: glofox_rows(dates=NEXT_WEEK_DATES))


# ══ 6. handler wiring ═════════════════════════════════════════════════

print("\nTest 9: the in-process handler (routing, policy denial, dispatch identity)")

SENT = []


def _ctx(policy=None, owner="alice", bot_id="level6bot"):
    return serve.ExecutionContext(
        session_id="sess", policy=serve.LEVEL6_WEEKLY_PRESET if policy is None
        else policy, bot_id=bot_id, bot_owner=owner)


def _send(chat_id, text):
    SENT.append(text)
    return 1


SENT.clear()
serve._run_level6_weekly_task(_ctx(policy={}), 1, "weekly", _send)
check("a Bot without the grant is denied",
      any("denied" in s for s in SENT), f"{SENT!r}")

SENT.clear()
serve._run_level6_weekly_task(_ctx(), 1, "weekly now", _send)
check("a non-exact task text fails closed",
      any("unsupported level6 request" in s for s in SENT), f"{SENT!r}")

SENT.clear()
serve._run_level6_weekly_task(_ctx(owner=""), 1, "weekly", _send)
check("an ownerless context fails closed",
      any("bound Bot with an owner" in s for s in SENT), f"{SENT!r}")

SENT.clear()
captured = []
real_run = l6.run_weekly
try:
    l6.run_weekly = lambda **kw: ["Monday 2026-09-21 — Back Squat — trainer: X"] * 6
    serve._run_level6_weekly_task(_ctx(), 1, "weekly", _send,
                                  on_result=lambda r: captured.append(r))
finally:
    l6.run_weekly = real_run
check("a granted, bound Bot relays the six lines",
      SENT and "THE WEEKLY SIX" in SENT[-1] and SENT[-1].count("\n") == 6,
      f"{SENT!r}")
check("the durable terminal result is captured",
      captured and captured[0]["count"] == 6, f"{captured!r}")

# Dispatch identity: the capture must run as the PERSISTENT browser-bot
# profile while PRESERVING the caller's owner.
captured_ctx = []
real_dispatch = serve.browser_host_dispatch
try:
    serve.browser_host_dispatch = (
        lambda ctx, text, **kw: (captured_ctx.append(ctx) or ({"status": "error"},
                                                             "stub")))
    result, error = serve._level6_browser_dispatch(
        _ctx(owner="alice"), json.loads(l6.weekly_browser_task_spec()))
finally:
    serve.browser_host_dispatch = real_dispatch
check("dispatch uses the persistent browser-bot profile id",
      captured_ctx and captured_ctx[0].bot_id == "browser-bot",
      f"{captured_ctx!r}")
check("dispatch preserves the caller's owner (never another owner)",
      captured_ctx and captured_ctx[0].bot_owner == "alice",
      f"{captured_ctx!r}")


# ══ 7. trusted-date pipeline: current / upcoming / stale weeks ════════


def ocr_for_week(monday):
    """Build a valid OCR post for the Monday-start week beginning *monday*."""
    names = ["Back Squat", "Front Squat", "Deadlift", "Bench Press",
             "Clean & Jerk", "Snatch"]
    lines = ["Level 6 Training", "THE WEEKLY SIX",
             f"WEEK OF {monday.month:02d}.{monday.day:02d}."
             f"{str(monday.year)[2:]}"]
    for i, name in enumerate(names):
        d = monday + timedelta(days=i)
        lines.append(
            f"{l6.WEEKDAYS[i].upper()} {d.month:02d}.{d.day:02d} {name}")
    return "\n".join(lines)


def _raised(fn):
    try:
        fn()
    except l6.Level6Error as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return f"WRONG:{type(exc).__name__}:{exc}"
    return ""


print("\nTest 10: the Glofox read is driven by the POST's own dates, not the clock")

# The live failure: the newest post is for THIS week (already started) while
# the reader's own clock-driven window is NEXT week. The pipeline must request
# EXACTLY the post's six dates.
out, seen = run_weekly(result=host_ok(text=OCR_CURRENT),
                       glofox=lambda dates: glofox_rows(dates=PREV_WEEK_DATES),
                       today=CURRENT_WEEK_WED)
check("a midweek CURRENT post resolves (its week already started)",
      len(out) == 6, f"{out!r}")
check("the reader was asked for EXACTLY the post's six dates",
      seen.get("dates") == PREV_WEEK_DATES, f"{seen.get('dates')!r}")
check("the midweek lines carry the post's OWN dates",
      out[0] == "Monday 2026-09-14 — Back Squat — trainer: Lauren Grabianowski",
      f"{out[0]!r}")

out, seen = run_weekly(result=host_ok(), today=CURRENT_WEEK_WED)
check("an UPCOMING post resolves midweek too", len(out) == 6, f"{out!r}")
check("the upcoming post's dates are the ones requested",
      seen.get("dates") == WEEK_DATES, f"{seen.get('dates')!r}")

print("\nTest 11: Sunday / upcoming post")

# Sunday 2026-09-13: the newest post is for the week beginning tomorrow.
out, seen = run_weekly(result=host_ok(text=OCR_CURRENT),
                       glofox=lambda dates: glofox_rows(dates=PREV_WEEK_DATES),
                       today=SUNDAY_BEFORE)
check("a Sunday run accepts the upcoming (next-Monday) post", len(out) == 6,
      f"{out!r}")
check("the Sunday run requests the upcoming week's dates",
      seen.get("dates") == PREV_WEEK_DATES, f"{seen.get('dates')!r}")

print("\nTest 12: stale / implausible post weeks fail closed BEFORE the read")

_stale_calls = []


def _stale_reader(dates):
    _stale_calls.append(list(dates))
    return glofox_rows(dates=PREV_WEEK_DATES)


STALE_MSG = _raised(lambda: run_weekly(
    result=host_ok(text=OCR_CURRENT), glofox=_stale_reader,
    today=STALE_TODAY)[0])
check("a stale post week is rejected", "stale weekly post" in STALE_MSG,
      STALE_MSG)
check("a stale post never reaches the Glofox read",
      _stale_calls == [], f"{_stale_calls!r}")

IMPLAUSIBLE_MSG = _raised(lambda: run_weekly(
    result=host_ok(text=ocr_for_week(date(2026, 10, 5))),
    today=date(2026, 9, 15))[0])
check("an implausible (far-future) post week is rejected",
      "implausible weekly post" in IMPLAUSIBLE_MSG, IMPLAUSIBLE_MSG)

six_prev = l6.parse_ocr_text(OCR_CURRENT)
six_next = l6.parse_ocr_text(OCR_TEXT)
check("gate accepts the current (already-started) week",
      l6.assert_plausible_week(six_prev, today=date(2026, 9, 14)) is None)
check("gate accepts the LAST day of a current week",
      l6.assert_plausible_week(six_prev, today=date(2026, 9, 19)) is None)
check("gate accepts the immediately upcoming week",
      l6.assert_plausible_week(six_next, today=date(2026, 9, 17)) is None)
check("gate rejects the week that ended before the current week",
      "stale" in _raised(lambda: l6.assert_plausible_week(
          six_prev, today=date(2026, 9, 21))))
check("gate rejects a week two weeks ahead",
      "implausible" in _raised(lambda: l6.assert_plausible_week(
          six_next, today=date(2026, 9, 7))))
check("gate rejects a bad reference type",
      "reference day" in _raised(lambda: l6.assert_plausible_week(
          six_next, today="tomorrow")))

print("\nTest 13: the standalone glofox: schedule path is unchanged")

_gl_public = {n for n, fn in inspect.getmembers(glofox_api, inspect.isfunction)
              if not fn.__name__.startswith("_")}
check("glofox public surface is still the pinned read set",
      _gl_public == {"get_branch", "get_trainers", "get_week_events",
                     "week_0830_classes"}, str(_gl_public))
check("week_0830_classes still has no date parameter",
      set(inspect.signature(glofox_api.week_0830_classes).parameters)
      <= {"_guest_login"},
      str(inspect.signature(glofox_api.week_0830_classes)))
check("the trusted-date reader is a PRIVATE internal seam",
      hasattr(glofox_api, "_week_0830_classes_for_dates")
      and not hasattr(l6, "week_0830_classes_for_dates"))

_GL_SENT = []
_gl_calls = {}
_gl_ctx = serve.ExecutionContext(
    session_id="glofox-sess", policy=dict(serve.GLOFOX_READER_PRESET),
    bot_id="level6bot", bot_owner="alice")
_real_clock = glofox_api.week_0830_classes
_real_dates = glofox_api._week_0830_classes_for_dates


def _fake_clock(*args, **kwargs):
    _gl_calls["clock"] = (args, kwargs)
    return [{"date": "2026-09-21", "class_name": "Group Fitness Class",
             "trainer_id": "t1", "trainer_name": "Lauren Grabianowski",
             "event_id": "e1"}]


def _fake_dates(*args, **kwargs):
    _gl_calls["dates"] = (args, kwargs)
    return []


try:
    with patch.object(task_store.CloudTaskStore, "get",
                      lambda self, t: {"task_id": t,
                                       "status": task_store.STATUS_RUNNING,
                                       "cancel_requested": False,
                                       "text": ""}), \
         patch.object(task_store.CloudTaskStore, "is_cancel_requested",
                      lambda self, t: False):
        glofox_api.week_0830_classes = _fake_clock
        glofox_api._week_0830_classes_for_dates = _fake_dates
        serve._run_glofox_schedule_task(
            _gl_ctx, chat_id=1, task_text=serve.GLOFOX_TASK_TEXT,
            task_id="task-1", send=lambda cid, txt: _GL_SENT.append(txt))
finally:
    glofox_api.week_0830_classes = _real_clock
    glofox_api._week_0830_classes_for_dates = _real_dates

check("glofox: schedule still reads the CLOCK-driven week (no dates passed)",
      _gl_calls.get("clock") == ((), {}) and "dates" not in _gl_calls,
      str(_gl_calls))

# The Level 6 handler, in contrast, wires the post's OWN dates into the read.
_ref = l6._reference_day()
_mon = _ref - timedelta(days=_ref.weekday())
_l6_dates = [(_mon + timedelta(days=i)).isoformat() for i in range(6)]
_L6_SENT = []
_l6_calls = {}
_real_dispatch = serve.browser_host_dispatch
try:
    serve.browser_host_dispatch = lambda ctx, text, **kw: (
        host_ok(text=ocr_for_week(_mon)), None)

    def _read_dates(dates, **kwargs):
        _l6_calls["dates"] = list(dates)
        return glofox_rows(dates=_l6_dates)

    glofox_api._week_0830_classes_for_dates = _read_dates
    serve._run_level6_weekly_task(
        _ctx(), 1, "weekly", lambda cid, txt: _L6_SENT.append(txt))
finally:
    serve.browser_host_dispatch = _real_dispatch
    glofox_api._week_0830_classes_for_dates = _real_dates

check("the level6 handler wires the post's OWN dates into the Glofox read",
      _l6_calls.get("dates") == _l6_dates, f"{_l6_calls!r}")
check("the level6 handler relays the weekly six",
      any("THE WEEKLY SIX" in s for s in _L6_SENT), f"{_L6_SENT!r}")


# ── Summary ───────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
