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
import json
import os
import sys
import tempfile

os.environ["KYREX_DATA_DIR"] = tempfile.mkdtemp(prefix="kyrex_level6_")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serve  # noqa: E402
import level6_weekly as l6  # noqa: E402

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


def run_weekly(result=None, glofox=None, dispatch_error=None):
    seen = {}

    def dispatch(spec):
        seen["spec"] = spec
        if dispatch_error is not None:
            return None, dispatch_error
        return result, None

    lines = l6.run_weekly(dispatch=dispatch,
                          glofox_read=glofox or (lambda: glofox_rows()))
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
             run_weekly, result=host_ok(), glofox=lambda: [])
expect_error("a mismatched Glofox week fails closed",
             run_weekly, result=host_ok(),
             glofox=lambda: glofox_rows(dates=NEXT_WEEK_DATES))


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


# ── Summary ───────────────────────────────────────────────────────────
print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
