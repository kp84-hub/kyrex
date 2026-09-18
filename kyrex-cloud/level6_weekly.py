#!/usr/bin/env python3
"""level6_weekly.py — the Cloud half of the ``level6: weekly`` MVP command.

This module never touches a browser, never reads page DOM text, and never
invents a date. It does exactly three things:

1. build the ONE fixed-purpose task spec for the Browser Host operation
   (``browser_operator.LEVEL6_WEEKLY_ACTION``) — a pinned page, no caller
   input;
2. consume the host's STRUCTURED result (OCR text produced by the host's LOCAL
   Tesseract run over a POST-ELEMENT screenshot + stable post metadata), parse
   it into the post's explicit ``WEEK OF MM.DD.YY`` (with its PRINTED year)
   plus exactly six Monday-Saturday dated workout rows, and fail closed on a
   stale or implausible week;
3. request the EXISTING Glofox 8:30 AM reader for EXACTLY those six trusted
   dates — NEVER the reader's own clock-driven "next week" window — and join
   with strict exact-date equality. A newest post whose week has already begun
   still resolves; the six dates come ONLY from the validated post, never from
   user text, a URL, a command suffix, or any other caller input.

What is deliberately NOT here
-----------------------------
* No OCR, no image handling, no screenshot path, no ``body.inner_text``. The
  DOM page text is NOT workout-image content and can never reach the parser:
  ``_assert_host_contract`` refuses any host response that carries page text,
  an artifact path, or a ``.png`` reference.
* No year inference. The year comes from the printed ``WEEK OF`` label; six
  dates are derived arithmetically from that one Monday. A post without a
  printed year fails closed.
* No cross-post mixing. The host returns OCR for ONE located post, and the
  marker/``WEEK OF`` label are required to occur exactly once in that text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

#: The one page the host operation may open (mirrors the host constant). The
#: Level 6 PHOTOS tab — NOT the timeline — because the newest "THE WEEKLY SIX"
#: graphic appears as the FIRST photo on that page each week.
FACEBOOK_PAGE_URL = "https://www.facebook.com/level6training/photos"

#: The branch timezone the weekly dates live in. The post's printed week AND
#: the Glofox schedule are both America/New_York, so the plausibility gate
#: compares the post's week against "today" in this zone.
BRANCH_TZ = ZoneInfo("America/New_York")

#: The visible marker that identifies a "THE WEEKLY SIX" post.
POST_MARKER = "THE WEEKLY SIX"

#: The persistent Browser Host profile the capture runs against. This is the
#: bot id whose ``(owner, bot_id)`` profile the bound Browser Host keeps on
#: disk, so the Facebook session survives across runs. The OWNER always comes
#: from the caller — this module never hard-codes an owner.
BROWSER_BOT_ID = "browser-bot"

#: The six class days, in order. Sunday is never part of the week.
WEEKDAYS: tuple[str, ...] = (
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
)

#: Explicit printed week label: ``WEEK OF 09.21.26`` / ``WEEK OF 9.21.2026``.
_WEEK_LABEL_RE = re.compile(
    r"^week\s*of\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2}|\d{4})\b",
    re.IGNORECASE,
)

#: A dated workout row: ``MONDAY 09.21 Back Squat`` (optionally with a year).
_ROW_RE = re.compile(
    r"^(?P<weekday>[A-Za-z]{3,9})\.?\s+"
    r"(?P<month>\d{1,2})[.\-/](?P<day>\d{1,2})"
    r"(?:[.\-/](?P<year>\d{2}|\d{4}))?\b"
    r"[-–—:|]*\s*(?P<workout>.+?)\s*$",
    re.IGNORECASE,
)

_ROW_SEPARATORS = " -–—:|."


class Level6Error(Exception):
    """The command cannot produce a trustworthy weekly six (fail closed)."""


# ── Data model ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WeeklyDay:
    """One dated workout row: weekday, exact calendar date, workout name."""

    weekday: str
    day: date
    workout: str

    @property
    def iso(self) -> str:
        return self.day.isoformat()


@dataclass(frozen=True)
class WeeklySix:
    """The parsed post: its printed week label and its six ordered days."""

    week_label: str
    days: tuple[WeeklyDay, ...]


# ── Task spec (the ONLY thing the host is ever asked to do) ────────────

def weekly_browser_task_spec() -> str:
    """The fixed-purpose Level 6 weekly task spec.

    Carries no URL argument and no date: the host operation pins the page
    itself and refuses any spec whose ``url`` is not that exact page.
    """
    return json.dumps(
        {"level6_weekly": True, "url": FACEBOOK_PAGE_URL},
        ensure_ascii=False,
    )


# ── Parsing ────────────────────────────────────────────────────────────

def _weekday_name(token: str) -> str | None:
    """Normalise a weekday token (full name or abbreviation)."""
    raw = str(token or "").strip().lower().rstrip(".")
    if len(raw) < 3:
        return None
    for name in WEEKDAYS:
        lower = name.lower()
        if raw == lower or lower.startswith(raw) or raw.startswith(lower[:3]):
            return name
    return None


def _printed_year(token: str | None, *, label: bool = False) -> int:
    """The printed year. 4 digits as-is; 2 digits via a fixed 2000+YY pivot.

    Never inferred from the clock — the value comes only from the post's own
    printed label. Anything else fails closed.
    """
    raw = str(token or "").strip()
    if not raw.isdigit():
        raise Level6Error("the week label has no printed year")
    if len(raw) == 4:
        return int(raw)
    if len(raw) == 2:
        return 2000 + int(raw)
    raise Level6Error(f"printed year {raw!r} is not a 2- or 4-digit year")


def parse_ocr_text(text, *, truncated: bool = False) -> WeeklySix:
    """Parse the host's OCR text into six explicit, dated Monday-Saturday rows.

    Fail closed on: truncated OCR, no/multiple ``THE WEEKLY SIX`` markers, no
    or multiple ``WEEK OF MM.DD.YY`` labels, a label that is not a Monday or
    that carries no printed year, anything other than six row dates, a
    duplicated or absent weekday, a row whose printed date disagrees with the
    label's week, an explicit row year disagreeing with the label's year, or
    a row with no workout name.
    """
    if truncated:
        raise Level6Error(
            "OCR output was truncated — the post could not be read in full"
        )
    if not isinstance(text, str) or not text.strip():
        raise Level6Error("the Browser Host returned no OCR text")

    marker_count = text.lower().count(POST_MARKER.lower())
    if marker_count == 0:
        raise Level6Error(f"no {POST_MARKER!r} marker was found in the post")
    if marker_count > 1:
        raise Level6Error(
            f"the captured text contains {marker_count} {POST_MARKER!r} "
            "markers — content from more than one post is not accepted"
        )

    lines = [line.strip() for line in text.splitlines()]
    labels = [line for line in lines if _WEEK_LABEL_RE.match(line)]
    if not labels:
        raise Level6Error(
            "the post has no explicit WEEK OF MM.DD.YY label with a printed "
            "year"
        )
    if len(labels) > 1:
        raise Level6Error(
            "the post has more than one WEEK OF label: "
            + ", ".join(repr(label) for label in labels)
        )
    week_label = labels[0]
    label_match = _WEEK_LABEL_RE.match(week_label)
    label_month = int(label_match.group(1))
    label_day = int(label_match.group(2))
    label_year = _printed_year(label_match.group(3), label=True)
    try:
        week_monday = date(label_year, label_month, label_day)
    except ValueError as exc:
        raise Level6Error(
            f"the week label {week_label!r} is not a valid calendar date"
        ) from exc
    if week_monday.weekday() != 0:
        raise Level6Error(
            f"the week label {week_label!r} does not fall on a Monday"
        )

    rows: list[tuple[str, str, int, int, int | None]] = []
    for line in lines:
        match = _ROW_RE.match(line)
        if not match:
            continue
        weekday = _weekday_name(match.group("weekday"))
        if weekday is None:
            raise Level6Error(f"unrecognised weekday in post line {line!r}")
        workout = match.group("workout").strip(_ROW_SEPARATORS)
        if not workout:
            raise Level6Error(f"post line has no workout name: {line!r}")
        year_raw = match.group("year")
        rows.append((
            weekday, workout,
            int(match.group("month")), int(match.group("day")),
            int(year_raw) if year_raw else None,
        ))

    if len(rows) != len(WEEKDAYS):
        raise Level6Error(
            f"the post must state exactly {len(WEEKDAYS)} Monday-Saturday "
            f"dated workout rows; found {len(rows)}"
        )

    by_weekday: dict[str, tuple[str, int, int, int | None]] = {}
    for weekday, workout, month, day_number, year_raw in rows:
        if weekday in by_weekday:
            raise Level6Error(f"the post states {weekday} more than once")
        by_weekday[weekday] = (workout, month, day_number, year_raw)
    missing = [name for name in WEEKDAYS if name not in by_weekday]
    if missing:
        raise Level6Error(
            "the post does not cover every Monday-Saturday day: missing "
            + ", ".join(missing)
        )

    days: list[WeeklyDay] = []
    for index, name in enumerate(WEEKDAYS):
        workout, month, day_number, year_raw = by_weekday[name]
        expected = week_monday + timedelta(days=index)
        if (month, day_number) != (expected.month, expected.day):
            raise Level6Error(
                f"the post's {name} row ({month:02d}.{day_number:02d}) does "
                f"not match the printed week "
                f"({expected.month:02d}.{expected.day:02d})"
            )
        if year_raw is not None and _printed_year(str(year_raw)) != expected.year:
            raise Level6Error(
                f"the post's {name} row prints a year that disagrees with the "
                "printed week label"
            )
        days.append(WeeklyDay(weekday=name, day=expected, workout=workout))

    return WeeklySix(week_label=week_label, days=tuple(days))


# ── Post-date plausibility ─────────────────────────────────────────────

def _reference_day(now=None) -> date:
    """The America/New_York calendar day the plausibility gate compares to.

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
    raise Level6Error("the reference day is not a calendar date")


def assert_plausible_week(six: WeeklySix, *, today=None) -> None:
    """Fail closed on a STALE or IMPLAUSIBLE post week.

    The newest published post must be for the CURRENT America/New_York ISO week
    or the IMMEDIATELY FOLLOWING one. A week that has already STARTED is still
    valid — a midweek post is the newest post and must NOT be rejected merely
    because its Monday is in the past. A week that is wholly in the past
    (stale) or two-or-more weeks ahead (implausible — never the "newest" post)
    is refused. The comparison uses ONLY the post's own printed dates plus the
    reference day; no user text, URL, or command suffix is involved.
    """
    reference = _reference_day(today)
    current_monday = reference - timedelta(days=reference.weekday())
    post_monday = six.days[0].day
    if post_monday < current_monday:
        raise Level6Error(
            "stale weekly post: the newest post is for the week of "
            f"{post_monday.isoformat()}, before the current week "
            f"({current_monday.isoformat()})"
        )
    if post_monday > current_monday + timedelta(days=7):
        raise Level6Error(
            "implausible weekly post: the newest post is for the week of "
            f"{post_monday.isoformat()}, ahead of the current week "
            f"({current_monday.isoformat()})"
        )


#: Human-facing messages for each structured Browser-Host failure code. Every
#: message is a FIXED string — no host-supplied text is ever interpolated, so a
#: host error can never leak a path, secret, or raw DOM into the Chat reply.
#: The messages are deliberately DISTINCT so a reader can tell "no candidate"
#: (nothing credible posted yet) from an OCR failure, an ambiguous/malformed
#: newest post, and (via :func:`assert_plausible_week`) a stale post.
_HOST_FAILURE_MESSAGES: dict[str, str] = {
    # Nothing credible was published / visible.
    "post_not_available":
        "weekly post not available",
    "no_candidate":
        "weekly post not available: no recent visible post carried the "
        "weekly-six marker",
    # A newer post had the marker but could not be resolved to one week.
    "ambiguous":
        "weekly post not available: the newest weekly-six candidates are "
        "ambiguous",
    "malformed_newest":
        "weekly post not available: the newest weekly-six post is malformed",
    # Feed / page / capture integrity.
    "ordering_untrusted":
        "weekly post not available: the feed order could not be trusted",
    "page_identity":
        "weekly post not available: the final page was not the Level 6 page",
    "capture_failed":
        "weekly post not available: the post capture failed",
    "capture_missing":
        "weekly post not available: the post capture produced no image",
    "locate_failed":
        "weekly post not available: the post lookup failed",
    # OCR outcomes.
    "ocr_unavailable":
        "weekly post not available: the OCR engine is unavailable",
    "ocr_timeout":
        "weekly post not available: the OCR engine timed out",
    "ocr_failed":
        "weekly post not available: the OCR engine failed",
    "ocr_truncated":
        "weekly post not available: the OCR output was truncated",
    # Policy denials / navigation.
    "not_allowlisted":
        "weekly post not available: the pinned page is not allowlisted",
    "navigate_denied":
        "weekly post not available: navigation was denied",
    "navigate_failed":
        "weekly post not available: navigation failed",
    "read_denied":
        "weekly post not available: reading the page was denied",
    "screenshot_denied":
        "weekly post not available: capture was denied",
}


def _host_failure_message(code: str, errors=None) -> str:
    """The fixed, non-secret message for a host failure *code*.

    Unknown codes fall back to a generic message that never echoes the code, so
    a hostile/misbehaving host cannot smuggle text into the surfaced error.
    """
    # Live diagnostics use a deliberately tiny grammar containing only
    # a fixed phase and a Python exception class. Never surface arbitrary host
    # text, even for locate_failed.
    if str(code or "") in {"locate_failed", "capture_failed"} and isinstance(errors, list) and len(errors) == 1:
        diagnostic = str(errors[0])
        if re.fullmatch(
            r"(?:photos_list|photo_capture):[A-Za-z][A-Za-z0-9_]{0,79}",
            diagnostic,
        ):
            return f"weekly post not available: diagnostic {diagnostic}"
    safe_code = str(code or "")
    bounded_ocr_messages = {
        "week_label_missing":
            "weekly post not available: OCR could not read the WEEK OF label",
        "week_label_ambiguous":
            "weekly post not available: OCR found multiple WEEK OF labels",
        "week_label_invalid":
            "weekly post not available: OCR read an invalid WEEK OF date",
        "week_label_not_monday":
            "weekly post not available: the WEEK OF date was not a Monday",
    }
    if safe_code in bounded_ocr_messages:
        return bounded_ocr_messages[safe_code]
    row_match = re.fullmatch(r"workout_rows_(\d{1,2})", safe_code)
    if row_match:
        return (
            "weekly post not available: OCR read "
            f"{int(row_match.group(1))} of 6 workout rows"
        )
    return _HOST_FAILURE_MESSAGES.get(
        safe_code,
        "weekly post not available: the Browser Host reported a capture "
        "failure",
    )


# ── Host-response contract guards ──────────────────────────────────────

def _assert_host_contract(result: dict, payload: dict) -> None:
    """Refuse any host response that smuggles page text or image data.

    The ONLY workout content the Cloud accepts is the host's local OCR of the
    located post element. A populated ``final_response`` is DOM page text, an
    artifact list or a ``.png`` reference is an image path: both must stay on
    the host.
    """
    if result.get("browser_artifacts"):
        raise Level6Error(
            "the Browser Host returned an artifact path; image data must stay "
            "on the host"
        )
    if str(result.get("final_response") or "").strip():
        raise Level6Error(
            "the Browser Host returned page text instead of post OCR; page "
            "DOM text is not workout-image content"
        )
    if ".png" in json.dumps(payload, default=str).lower():
        raise Level6Error(
            "the Browser Host response contains an image path"
        )


# ── Glofox join ────────────────────────────────────────────────────────

def _glofox_date(row: dict) -> date:
    raw = str((row or {}).get("date") or "").strip()
    if not raw:
        raise Level6Error("a Glofox week row carries no date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise Level6Error(
            f"Glofox week row date {raw!r} is not an America/New_York "
            "calendar date"
        ) from exc


def join_week(six: WeeklySix, glofox_rows) -> list[str]:
    """Join the post's six dates with the Glofox reader's six dates.

    Equality is EXACT (all six calendar dates, including the year). The rows
    are normally the result of reading EXACTLY the post's dates, so any
    difference is a fail-closed mismatch — a short/extra week, a duplicated
    date, or (defensively) a week that is BEHIND the reader's dates is
    reported as "weekly post not available".
    """
    if not glofox_rows:
        raise Level6Error("the Glofox week returned no classes")

    by_date: dict[str, dict] = {}
    for row in glofox_rows:
        if not isinstance(row, dict):
            raise Level6Error("the Glofox week returned a malformed row")
        key = _glofox_date(row).isoformat()
        if key in by_date:
            raise Level6Error(f"ambiguous Glofox rows for {key}")
        by_date[key] = row

    post_dates = {entry.iso for entry in six.days}
    glofox_dates = set(by_date)

    if post_dates != glofox_dates:
        missing = sorted(post_dates - glofox_dates)
        extra = sorted(glofox_dates - post_dates)
        if missing and min(post_dates) < min(glofox_dates):
            raise Level6Error(
                "weekly post not available: the newest published post is for "
                f"the week of {min(post_dates)}, but the upcoming Glofox week "
                f"starts {min(glofox_dates)}"
            )
        raise Level6Error(
            "the post's week does not match the Glofox week exactly "
            f"(missing from Glofox: {missing or 'none'}; "
            f"unexpected in Glofox: {extra or 'none'})"
        )

    lines: list[str] = []
    for entry in six.days:
        row = by_date[entry.iso]
        trainer = str(row.get("trainer_name") or "").strip()
        if not trainer:
            raise Level6Error(
                f"the Glofox class on {entry.iso} has no resolvable trainer"
            )
        lines.append(
            f"{entry.weekday} {entry.iso} — {entry.workout} — trainer: {trainer}"
        )
    return lines


# ── Orchestration ──────────────────────────────────────────────────────

def run_weekly(*, dispatch, glofox_read, today=None) -> list[str]:
    """Run the command: host capture, parse, trusted-date Glofox read, join.

    Order, each fail-closed: dispatch the fixed spec to the Browser Host →
    require a structured ``level6_weekly`` payload → refuse page text, image
    paths, or truncated OCR → parse the printed week + six dated rows → refuse
    a stale/implausible week → read the Glofox 8:30 classes for EXACTLY the
    post's six trusted dates → join on exact date equality.

    ``glofox_read`` is called with the six ISO dates taken ONLY from the
    validated post; production wires it to
    ``glofox_api._week_0830_classes_for_dates``. It never receives a
    recomputed "next week", user text, a URL, or a command suffix. ``today``
    is an optional reference calendar day for the plausibility gate
    (production passes ``None`` → the current America/New_York date).
    """
    result, error = dispatch(weekly_browser_task_spec())
    if error:
        raise Level6Error(f"browser capture failed: {error}")
    if not isinstance(result, dict):
        raise Level6Error("the Browser Host returned no result")

    payload = result.get("level6_weekly")
    if not isinstance(payload, dict):
        raise Level6Error(
            "the Browser Host returned no Level 6 post data"
        )

    error_code = str(payload.get("error_code") or "")
    if error_code:
        # A structured host failure. A KNOWN code maps to its fixed,
        # distinguishable message (no candidate vs OCR failure vs ambiguity vs
        # malformed-newest), so the surfaced error tells the reader WHICH
        # fail-closed condition was hit without exposing any host detail.
        raise Level6Error(_host_failure_message(error_code, result.get("errors")))

    errors = result.get("errors")
    if str(result.get("status") or "").strip() == "error" or errors:
        detail = ("; ".join(str(e) for e in errors)
                  if isinstance(errors, list) else "")
        raise Level6Error(
            "the Browser Host reported an error"
            + (f": {detail}" if detail else "")
        )

    _assert_host_contract(result, payload)

    if payload.get("ocr_truncated"):
        raise Level6Error(
            "OCR output was truncated — the post could not be read in full"
        )
    text = payload.get("ocr_text")
    if not isinstance(text, str) or not text.strip():
        raise Level6Error("the Browser Host returned no OCR text")

    six = parse_ocr_text(text)

    # The post's OWN validated dates drive the schedule read: a newest post
    # whose week has already started must still resolve, so the reader is
    # asked for EXACTLY these six dates rather than a recomputed next week.
    # This is the ONLY source of the dates — never user text, a URL, a command
    # suffix, or any other caller input.
    assert_plausible_week(six, today=today)
    rows = glofox_read([entry.iso for entry in six.days])
    return join_week(six, rows)
