#!/usr/bin/env python3
"""Glofox schedule connector — smallest fail-closed read-only surface.

Retrieves the upcoming week's classes for ONE pinned branch (Level 6
Training, branch id ``67239a5afaac4eea10045d29``) from the Glofox member
API and returns only the validated 8:30 AM slots.

Design constraints (KX security boundary, from the completed
reconnaissance — see test_glofox_api.py for the pinned evidence):

* No generalized HTTP client: the ONLY requests issued are

  1. ``POST https://api.glofox.com/2.0/login`` — body is the FIXED
     ``{"branch_id": BRANCH_ID, "login": "GUEST", "password": "GUEST"}``
     (Glofox's own public guest mechanism; no user account exists).
  2. ``GET  https://api.glofox.com/2.0/branches/{BRANCH_ID}``
  3. ``GET  https://api.glofox.com/2.0/branches/{BRANCH_ID}/events``
     (only the fixed query string built inside :func:`get_week_events`)
  4. ``GET  https://api.glofox.com/2.0/staff`` type=trainer

  Every other (method, URL, body, branch id, filter, page) combination is
  unreachable from this module — there is no parameter that can change
  them.

* The guest token is memory-only: held in locals during one invocation,
  never logged, never stored, never returned, and scrubbed from every
  error message.  A fresh token is fetched per :func:`week_0830_classes`
  call (one guest login per invocation, nothing persists).

* Fail closed on authentication failure, transport failure, TLS failure,
  oversized responses, non-dict envelopes, missing/incorrect envelope
  keys, total_count/has_more inconsistencies, malformed events, timezone
  mismatch, duplicate event ids, pagination beyond the hard cap,
  unresolvable trainer references, or malformed time values.  Every
  failure raises :class:`GlofoxError` (or a subclass); nothing is
  guessed, and no prior data is silently reused.

* Timezone: all week boundaries and the 8:30 AM slot comparison are
  computed in the branch's own timezone, pinned to ``America/New_York``
  (DST-aware via :mod:`zoneinfo`).  A branch whose metadata reports any
  other timezone fails closed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Pinned facts — the ONLY values this module will ever talk to.
# ---------------------------------------------------------------------------

BRANCH_ID = "67239a5afaac4eea10045d29"
API_HOST = "api.glofox.com"
API_BASE = f"https://{API_HOST}/2.0"

#: The exact guest credentials embedded in Glofox's own member app.  Not
#: secrets — they ship in the public bundle and yield only a read-scoped
#: GUEST token (no booking, no write surface).
GUEST_LOGIN = "GUEST"
GUEST_PASSWORD = "GUEST"

#: Branch timezone observed in ``GET /branches/{id}`` (``address.timezone_id``).
BRANCH_TZ = ZoneInfo("America/New_York")

#: Bounded timeouts (seconds).  Fail fast, never hang.
REQUEST_TIMEOUT = 10.0

#: Response hard cap.  Anything larger is treated as malformed/fail-closed.
MAX_RESPONSE_BYTES = 256 * 1024

#: Pagination hard caps: 100 per page, at most 2 pages (200 events / week).
PAGE_LIMIT = 100
MAX_PAGES = 2

#: The only slot time this module reports.
SLOT_HOUR = 8
SLOT_MINUTE = 30

API_LOGIN_URL = f"{API_BASE}/login"
_BRANCH_URL = f"{API_BASE}/branches/{BRANCH_ID}"
_EVENTS_URL = f"{API_BASE}/branches/{BRANCH_ID}/events"
#: Exact staff-roster query from the web-portal frontend: the type value is
#: the UPPERCASE constant (``CONSTANTS.USER_TYPES.TRAINER == "TRAINER"``) —
#: lowercase ``type=trainer`` returns an EMPTY roster (verified live).
_STAFF_URL = f"{API_BASE}/staff?type=TRAINER&sort_by=first_name"

#: The one class the workflow reports (exact name binding, not a guess).
#: The Level 6 branch schedules its "Level 6 Training" class at 8:30 AM;
#: the name is pinned and any other 8:30 class fails the whole request.
SCHEDULED_CLASS_NAME = "Group Fitness Class"

#: Pinned request headers observed during the reconnaissance.  The API
#: 403s requests without an app-origin header; these are constants, not a
#: configurable surface.
PINNED_HEADERS = {
    "Origin": "https://app.glofox.com",
    "User-Agent": "Mozilla/5.0 (Kyrex glofox:read connector)",
}


class GlofoxError(Exception):
    """Base failure — raised instead of ever returning partial data."""


class GlofoxAuthError(GlofoxError):
    """Guest token refused or unusable."""


class GlofoxTransportError(GlofoxError):
    """Network, timeout, TLS, HTTP-status, or oversized-response failure."""


class GlofoxSchemaError(GlofoxError):
    """Response envelope/event/staff records failed structural validation."""


class GlofoxDataError(GlofoxError):
    """Semantically invalid data: duplicates, unresolvable trainers."""


# ---------------------------------------------------------------------------
# Internal HTTP helpers — deliberately NOT a generalized client.
# ---------------------------------------------------------------------------

def _bounded_raw_response(request: urllib.request.Request) -> bytes:
    """Fetch a response under strict transport constraints.

    TLS verification is urllib's default and is NOT disabled.  Read
    timeouts are tight.  The body is read capped at ``MAX_RESPONSE_BYTES``;
    a larger body is a transport failure, never a truncated success.
    """
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise GlofoxTransportError(f"HTTP {exc.code} from {API_HOST}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GlofoxTransportError(_scrubbed_transport_reason(exc)) from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GlofoxTransportError(f"response exceeds {MAX_RESPONSE_BYTES}-byte cap")
    return raw


def _scrubbed_transport_reason(exc: Exception) -> str:
    """Human-safe reason text that can never contain a token value."""
    reason = getattr(exc, "reason", None)
    if isinstance(reason, Exception):
        reason = repr(reason)
    text = str(reason if reason is not None else exc)
    return text if len(text) <= 200 else text[:200] + "…"


def _post_guest_login() -> str:
    """Perform the single allowed POST: the fixed GUEST login.

    Returns the raw JWT string (memory-only, handled by the caller).
    Raises :class:`GlofoxAuthError` on any failure — transport is
    normalized to auth here because the token is the point of the call.
    """
    body = json.dumps(
        {"branch_id": BRANCH_ID, "login": GUEST_LOGIN, "password": GUEST_PASSWORD}
    ).encode("utf-8")
    request = urllib.request.Request(
        API_LOGIN_URL,
        data=body,
        headers={**PINNED_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        raw = _bounded_raw_response(request)
    except GlofoxTransportError as exc:
        raise GlofoxAuthError(f"guest login transport failure: {exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise GlofoxAuthError("guest login response is not JSON") from exc
    if payload is True:  # Glofox wraps simple success payloads as `true`
        raise GlofoxAuthError("guest login response has no token")
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), str) or not payload["token"]:
        raise GlofoxAuthError("guest login returned no usable token")
    return payload["token"]


def _get_json_list(
    url: str,
    token: str,
) -> tuple[list, int, bool]:
    """GET ``url`` and validate the Glofox list envelope.

    Returns ``(data, total_count, has_more)``.  Fails closed on anything
    that is not exactly the observed envelope shape.
    """
    request = urllib.request.Request(
        url,
        headers={**PINNED_HEADERS, "Authorization": f"Bearer {token}"},
        method="GET",
    )
    raw = _bounded_raw_response(request)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise GlofoxSchemaError(f"{API_HOST} response is not JSON") from exc
    if not isinstance(payload, dict):
        raise GlofoxSchemaError(f"{API_HOST} response is not an object")
    # Order matters: an auth-refusal is a transport-class failure and must
    # be reported as such even when other envelope keys are also malformed.
    if payload.get("success") is False:
        raise GlofoxTransportError("endpoint reported success:false (auth refused)")
    if payload.get("object") != "list":
        raise GlofoxSchemaError("missing/incorrect 'object' envelope key")
    data = payload.get("data")
    if not isinstance(data, list):
        raise GlofoxSchemaError("list envelope has no 'data' array")
    total_count = payload.get("total_count")
    has_more = payload.get("has_more")
    if not isinstance(total_count, int) or total_count < 0:
        raise GlofoxSchemaError("list envelope total_count invalid")
    if not isinstance(has_more, bool):
        raise GlofoxSchemaError("list envelope has_more invalid")
    if len(data) > PAGE_LIMIT:
        raise GlofoxSchemaError("page exceeds declared PAGE_LIMIT")
    return data, total_count, has_more


def _get_json_record(url: str, token: str) -> dict:
    """GET ``url`` and validate a single-record (object) response."""
    request = urllib.request.Request(
        url,
        headers={**PINNED_HEADERS, "Authorization": f"Bearer {token}"},
        method="GET",
    )
    raw = _bounded_raw_response(request)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise GlofoxSchemaError(f"{API_HOST} response is not JSON") from exc
    if not isinstance(payload, dict):
        raise GlofoxSchemaError("record response is not an object")
    if payload.get("success") is False:
        raise GlofoxTransportError("endpoint reported success:false (auth refused)")
    return payload


# ---------------------------------------------------------------------------
# The three reads.
# ---------------------------------------------------------------------------

def get_branch(token: str) -> dict:
    """Fetch and validate branch metadata (required GET #2).

    Returns the branch record as a single object (the branches detail
    endpoint is not a list envelope).  Fails closed unless the branch
    reports the pinned timezone.
    """
    branch = _get_json_record(_BRANCH_URL, token)
    if not isinstance(branch.get("_id"), str) or branch["_id"] != BRANCH_ID:
        raise GlofoxSchemaError("branch id missing or mismatch (pinned violation)")
    address = branch.get("address")
    if not isinstance(address, dict):
        raise GlofoxSchemaError("branch record missing address")
    if address.get("timezone_id") != "America/New_York":
        raise GlofoxSchemaError(
            f"branch timezone {address.get('timezone_id')!r} is not the pinned "
            "America/New_York"
        )
    return branch


def get_trainers(token: str) -> dict[str, str]:
    """Fetch the trainer roster (required GET #3 → ``{staff_id: name}``).

    Fixed query from the frontend staff filter: ``type=TRAINER`` (the
    UPPERCASE user-type constant — lowercase returns an empty roster) with
    ``sort_by=first_name``.  Records are validated strictly: staff id,
    exact pinned ``branch_id``, ``type == "trainer"``, ``active is True``,
    and a non-empty display name (``name`` field or first/last join).
    Duplicates, foreign-branch records, inactive records, or missing-branch
    records fail the whole fetch closed.
    """
    trainers: dict[str, str] = {}
    url = _STAFF_URL
    for page in range(1, MAX_PAGES + 1):
        data, total, has_more = _get_json_list(url, token)
        for record in data:
            if not isinstance(record, dict):
                raise GlofoxSchemaError("staff record is not an object")
            staff_id = record.get("_id")
            if not isinstance(staff_id, str) or not staff_id:
                raise GlofoxSchemaError("staff record missing _id")
            if record.get("branch_id") != BRANCH_ID:
                raise GlofoxDataError(
                    f"staff record {staff_id!r} belongs to a foreign branch"
                )
            if record.get("type") != "trainer":
                raise GlofoxDataError(
                    f"staff record {staff_id!r} type is not 'trainer'"
                )
            if record.get("active") is not True:
                raise GlofoxDataError(
                    f"staff record {staff_id!r} is not active"
                )
            if staff_id in trainers:
                raise GlofoxDataError(f"duplicate staff id {staff_id!r}")
            name = record.get("name") or _display_name(record)
            if not (isinstance(name, str) and name.strip()):
                raise GlofoxSchemaError(f"staff record {staff_id!r} has no name")
            trainers[staff_id] = name.strip()
        if not has_more:
            if len(trainers) != total:
                raise GlofoxSchemaError(
                    f"trainer total_count {total} != fetched {len(trainers)}"
                )
            return trainers
        if page == MAX_PAGES:
            break
        url = f"{_STAFF_URL}&page={page + 1}"
    raise GlofoxSchemaError("pagination exceeded MAX_PAGES")


def _display_name(record: dict) -> str:
    parts = [record.get(f) for f in ("first_name", "last_name")]
    return " ".join(p for p in parts if isinstance(p, str)).strip()


def get_week_events(
    token: str,
    reference_date: str | datetime | None = None,
) -> list[dict]:
    """Validate the next Monday-Saturday's class events (required GET #1).

    The window is the next Monday 00:00:00 through Saturday 23:59:59 in
    America/New_York strictly after the reference instant (future-facing
    for the Sunday-evening workflow; Sunday is excluded).  The query is
    the EXACT class-day-view request proven in the web-portal bundle:
    ``?start=&end=&include=trainers,facility,program,users_booked
    &sort_by=time_start&private=false&page=N`` — NO ``filter`` and NO
    ``model`` parameter (the appointment variant returns only the
    trainer-less self-serve timeslot rows).  Page following is bounded
    by MAX_PAGES and total_count must reconcile exactly.
    """
    reference = _reference_in_branch_tz(reference_date)
    week_start, week_end = _next_monday_to_saturday_bounds(reference)
    return _query_week_events(token, week_start, week_end)


def _query_week_events(
    token: str,
    week_start: datetime,
    week_end: datetime,
) -> list[dict]:
    """Fetch + validate events across ONE already-validated week window.

    This is the ONLY events-query builder in the module; the caller supplies
    an already-validated Monday..Saturday instant pair — either the
    future-facing "next week" from :func:`get_week_events`, or an explicit
    trusted week from :func:`_week_0830_classes_for_dates`. No clock value and
    no caller-supplied string ever reaches the query here.
    """
    start_unix = int(week_start.timestamp())
    end_unix = int(week_end.timestamp())
    base = (
        f"{_EVENTS_URL}"
        f"?start={start_unix}&end={end_unix}"
        f"&include=trainers,facility,program,users_booked"
        f"&sort_by=time_start&private=false"
    )

    events: list[dict] = []
    seen_ids: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        data, total, has_more = _get_json_list(f"{base}&page={page}", token)
        for record in data:
            _validate_event_schema(record)
            event_id = record["_id"]
            if event_id in seen_ids:
                raise GlofoxDataError(f"duplicate event id {event_id!r}")
            seen_ids.add(event_id)
            events.append(record)
        if not has_more:
            if len(events) != total:
                raise GlofoxSchemaError(
                    f"event total_count {total} != fetched {len(events)}"
                )
            return events
    raise GlofoxSchemaError("pagination exceeded MAX_PAGES")


def _reference_in_branch_tz(
    reference_date: str | datetime | None,
) -> datetime:
    """Interpret the reference date in the pinned branch timezone."""
    if reference_date is None:
        return datetime.now(BRANCH_TZ)
    if isinstance(reference_date, str):
        try:
            parsed = datetime.strptime(reference_date, "%Y-%m-%d")
        except ValueError as exc:
            raise GlofoxSchemaError(
                f"reference_date {reference_date!r} is not YYYY-MM-DD"
            ) from exc
        # Noon anchor: immune to DST shifts when converting days, and
        # structurally can never fall inside a 1-2 AM DST transition.
        reference = parsed.replace(tzinfo=BRANCH_TZ, hour=12)
        fold0 = reference.replace(fold=0)
        fold1 = reference.replace(fold=1)
        if fold0.utcoffset() != fold1.utcoffset():
            raise GlofoxSchemaError(
                f"reference_date maps to a DST-ambiguous or nonexistent "
                f"local time ({reference_date!r})"
            )
        return reference
    if isinstance(reference_date, datetime):
        if reference_date.tzinfo is None:
            raise GlofoxSchemaError(
                "naive datetime reference_date is ambiguous — pass a date "
                "string or a tz-aware datetime"
            )
        return reference_date.astimezone(BRANCH_TZ)
    raise GlofoxSchemaError("unsupported reference_date type")


def _validate_window_edges(start: datetime, end: datetime) -> None:
    """Fail closed on malformed or DST-ambiguous Mon/Sat window edges.

    Purely structural: the edges must be a Monday 00:00:00 and a Saturday
    23:59:59 that each map to exactly one America/New_York instant. There is
    deliberately NO "strictly in the future" requirement here — that belongs
    only to the clock-driven "next week" path (see
    :func:`_validate_window_boundaries`); an EXPLICIT trusted week may already
    have begun and is still valid.
    """
    if start.weekday() != 0 or end.weekday() != 5:
        raise GlofoxSchemaError(
            "schedule window is not Monday-through-Saturday (internal error)"
        )
    for label, instant in (("week start", start), ("week end", end)):
        fold0 = instant.replace(fold=0)
        fold1 = instant.replace(fold=1)
        if fold0.utcoffset() != fold1.utcoffset():
            raise GlofoxSchemaError(
                f"{label} falls on a DST-ambiguous or nonexistent local time"
            )


def _validate_window_boundaries(
    start: datetime, end: datetime, reference: datetime
) -> None:
    """Fail closed on malformed, ambiguous, or non-future window edges."""
    _validate_window_edges(start, end)
    if reference in (start, end) or not start > reference:
        raise GlofoxSchemaError(
            "schedule window does not begin strictly after the reference instant"
        )


def _next_monday_to_saturday_bounds(
    reference: datetime,
) -> tuple[datetime, datetime]:
    """Next Mon 00:00:00 → Sat 23:59:59 strictly after *reference*, branch tz.

    Future-facing for the Sunday-evening workflow: a Sunday run selects
    tomorrow (Monday) through Saturday; any weekday run selects the
    FOLLOWING calendar Monday through Saturday.  Sunday is never part of
    the window, and every boundary is validated as an existing,
    unambiguous America/New_York instant.
    """
    reference = reference.astimezone(BRANCH_TZ)
    days_ahead = 7 - reference.weekday()  # Mon→7 … Sun→1
    monday_date = (reference + timedelta(days=days_ahead)).date()
    saturday_date = monday_date + timedelta(days=5)
    start = datetime.combine(monday_date, time.min, BRANCH_TZ)
    end = datetime.combine(saturday_date, time(23, 59, 59), BRANCH_TZ)
    _validate_window_boundaries(start, end, reference)
    return start, end


def _validate_event_schema(record: object) -> None:
    """Strict per-event schema; anything unexpected fails closed.

    Pinned to the verified class-day-view record: ``type == "event"``
    (the day-view schedule rows — the ``timeslot`` type belongs to the
    separate appointment/self-serve variant this slice does NOT use).
    ``model`` may be None or a string indifferently (observe only).
    """
    if not isinstance(record, dict):
        raise GlofoxSchemaError("event is not an object")
    for key in (
        "_id",
        "name",
        "time_start",
        "type",
        "duration",
        "trainers",
        "trainers_obj",
        "branch_id",
    ):
        if key not in record:
            raise GlofoxSchemaError(f"event missing required key {key!r}")
    if not isinstance(record["_id"], str) or not record["_id"]:
        raise GlofoxSchemaError("event _id invalid")
    if not isinstance(record["name"], str) or not record["name"]:
        raise GlofoxSchemaError("event name invalid")
    if record["type"] != "event":
        raise GlofoxSchemaError(
            f"event type {record['type']!r} is not the class-day-view 'event'"
        )
    if record["branch_id"] != BRANCH_ID:
        raise GlofoxSchemaError("event branch_id mismatch (pinned violation)")
    trainers = record["trainers"]
    if not isinstance(trainers, list) or not all(isinstance(t, str) for t in trainers):
        raise GlofoxSchemaError("event trainers is not a list of ids")
    trainers_obj = record["trainers_obj"]
    if not isinstance(trainers_obj, list) or not all(
        isinstance(o, dict) for o in trainers_obj
    ):
        raise GlofoxSchemaError("event trainers_obj is not a list of records")
    for obj in trainers_obj:
        if not isinstance(obj.get("_id"), str) or not obj["_id"]:
            raise GlofoxSchemaError("trainers_obj entry missing _id")
        if obj.get("branch_id") != BRANCH_ID:
            raise GlofoxSchemaError(
                "trainers_obj entry branch mismatch (pinned violation)"
            )
        # NOTE: the embedded trainers_obj snapshot carries NO 'active'
        # flag and type 'user' — the full roster record from the
        # type=TRAINER staff endpoint is the authority for active/type;
        # here we require only id, pinned branch, and a display name.
        if not isinstance(obj.get("first_name"), str):
            raise GlofoxSchemaError("trainers_obj entry missing first_name")
        if not isinstance(obj.get("last_name"), str):
            raise GlofoxSchemaError("trainers_obj entry missing last_name")
    if not isinstance(record["duration"], int) or record["duration"] <= 0:
        raise GlofoxSchemaError("event duration invalid")
    if not isinstance(record["time_start"], int) or record["time_start"] <= 0:
        raise GlofoxSchemaError("event time_start invalid")


# ---------------------------------------------------------------------------
# The one caller-facing entry point: validated 8:30 AM classes.
# ---------------------------------------------------------------------------

#: America/New_York weekdays the workflow requires an 8:30 class for
#: (Monday 0 … Saturday 5).  Sunday is structurally excluded from the
#: window and is never an "expected class day".
EXPECTED_CLASS_WEEKDAYS = frozenset(range(0, 6))

#: The exact number of days a trusted Level 6 weekly week must contain: one
#: Monday through one Saturday. Enforced when an EXPLICIT week (from a
#: validated Facebook post) is used, so a truncated/padded date set can never
#: reach the query.
TRUSTED_WEEK_LENGTH = 6


def week_0830_classes(*, _guest_login=None) -> list[dict]:
    """Return the validated 8:30 AM classes for the next Mon-Sat.

    The schedule reference is ALWAYS "now" in the branch timezone — there
    is NO caller-controlled date parameter (test-only entry points
    separately use :func:`_week_0830_classes_for`).  Window selection is
    future-facing: an America/New_York Sunday run selects tomorrow
    (Monday) through Saturday; a weekday run selects the following
    Monday through Saturday.  Sunday is never reported.

    Result rows contain exactly: ``date`` (``YYYY-MM-DD``), ``class_name``,
    ``trainer_id``, ``trainer_name``, ``event_id``.  Completeness is
    REQUIRED: every one of the six expected class days must have exactly
    one 8:30 event whose trainer id resolves to exactly one staff record.
    Missing trainer, unresolved/ambiguous mappings, or a missing expected
    day fail the ENTIRE request with a GlofoxDataError that lists only
    the affected dates.  A successful result NEVER contains a null
    trainer and never reuses prior data.  The guest token is never
    returned, logged, or persisted.
    """
    return _week_0830_classes_for(None, _guest_login=_guest_login)


def _week_0830_classes_for(
    reference_date: str | datetime | None,
    *,
    _guest_login=None,
) -> list[dict]:
    """Test-only reference-date seam; production passes ``None``."""
    token = (_guest_login if _guest_login is not None else _post_guest_login)()
    try:
        get_branch(token)
        events = get_week_events(token, reference_date)
        trainers = get_trainers(token)
    finally:
        # The token lives only inside this call frame; drop it promptly.
        token = ""

    window_days = _expected_window_dates(events, reference_date)
    return _resolve_0830_rows(events, window_days, trainers)


def _week_0830_classes_for_dates(
    dates,
    *,
    _guest_login=None,
) -> list[dict]:
    """Read the validated 8:30 classes for EXACTLY the trusted week's dates.

    This is the internal seam the ``level6: weekly`` pipeline uses INSTEAD of
    the clock-driven "next Monday-Saturday" window. The SIX dates come from
    :mod:`level6_weekly` parsing a VALIDATED Facebook weekly post — never from
    user text, a URL, a command suffix, or any other caller input (the only
    production caller is ``serve._run_level6_weekly_task``, which passes the
    parsed post's own dates).

    Fail closed unless the input is exactly six UNIQUE, CONSECUTIVE
    Monday-through-Saturday America/New_York calendar dates; then read
    EXACTLY that window (a newest post whose week has already begun is still
    valid — there is deliberately NO future requirement) and return the SAME
    row shape as :func:`week_0830_classes`. Missing classes, duplicate events,
    an incomplete week, or an unresolvable trainer fail the whole request
    closed via :func:`_resolve_0830_rows`.

    The standalone ``glofox: schedule`` command never calls this: it keeps
    :func:`week_0830_classes` and its future-facing window unchanged.
    """
    window_days = _trusted_week_dates(dates)
    week_start, week_end = _explicit_week_bounds(window_days)
    token = (_guest_login if _guest_login is not None else _post_guest_login)()
    try:
        get_branch(token)
        events = _query_week_events(token, week_start, week_end)
        trainers = get_trainers(token)
    finally:
        # The token lives only inside this call frame; drop it promptly.
        token = ""
    return _resolve_0830_rows(events, window_days, trainers)


def _resolve_0830_rows(
    events: list[dict],
    window_days: list[str],
    trainers: dict[str, str],
) -> list[dict]:
    """Filter to the exact 8:30 slot and join trainers for ONE week.

    Shared by BOTH window selectors — the clock-driven "next week"
    (:func:`_week_0830_classes_for`) and the explicit trusted week
    (:func:`_week_0830_classes_for_dates`). *window_days* is the
    authoritative set of ISO class days: the completeness and duplicate
    checks run ONLY against it, so a week can never be silently padded or
    trimmed, and a missing expected day fails the whole request closed.
    """
    qualifying: list[tuple[str, dict]] = []
    for event in sorted(events, key=lambda e: e["time_start"]):
        start = datetime.fromtimestamp(event["time_start"], BRANCH_TZ)
        if start.hour != SLOT_HOUR or start.minute != SLOT_MINUTE:
            continue
        if event["name"] != SCHEDULED_CLASS_NAME:
            # Strict class binding: only the one scheduled class counts.
            raise GlofoxDataError(
                f"unexpected 8:30 class name on {start:%Y-%m-%d}: "
                f"{event['name']!r} (event {event['_id']!r})"
            )
        qualifying.append((start.strftime("%Y-%m-%d"), event))

    # Completeness: every Mon-Sat day of the window must have EXACTLY ONE
    # 8:30 AM Level 6 Training class.  A missing day, or two events claiming
    # the same expected day, fail the entire request.
    present_days = {day for day, _ev in qualifying}
    missing = sorted(d for d in window_days if d not in present_days)
    duplicates = sorted(
        d for d in window_days
        if sum(1 for day, _ in qualifying if day == d) > 1
    )

    # Trainer resolution failures — collected per date, reported together.
    # Every qualifying event must resolve to exactly one ACTIVE trainer id,
    # present BOTH in the embedded trainers_obj (id + matching display
    # name) AND in the type=TRAINER staff roster.
    trainer_problems: list[str] = []
    resolved: list[tuple[str, dict, str, str]] = []
    for day, event in qualifying:
        trainer_ids = event["trainers"]
        if len(trainer_ids) != 1:
            trainer_problems.append(
                f"{day} ({len(trainer_ids)} trainer ids -> event {event['_id']!r})"
            )
            continue
        trainer_id = trainer_ids[0]
        embedded = [o for o in event.get("trainers_obj", []) if o.get("_id") == trainer_id]
        if len(embedded) == 0:
            trainer_problems.append(
                f"{day} (trainer id {trainer_id!r} missing from trainers_obj)"
            )
            continue
        if len(embedded) > 1:
            trainer_problems.append(
                f"{day} (ambiguous trainers_obj entries for {trainer_id!r})"
            )
            continue
        staff_name = trainers.get(trainer_id)
        if staff_name is None:
            trainer_problems.append(
                f"{day} (trainer id {trainer_id!r} has no staff record)"
            )
            continue
        # Staff record + trainers_obj must agree (same id/name resolution);
        # any mismatch is ambiguous data, not inferable truth.
        obj_name = (
            embedded[0].get("name")
            if isinstance(embedded[0].get("name"), str)
            else _display_name(embedded[0])
        )
        if not obj_name or obj_name.strip() != staff_name:
            trainer_problems.append(
                f"{day} (trainer {trainer_id!r} name mismatched between "
                "staff roster and trainers_obj)"
            )
            continue
        # NOTE: the embedded trainers_obj snapshot does NOT carry the
        # 'active'/'type' flags; get_trainers' roster gate is the sole
        # active/inactive authority.
        resolved.append((day, event, trainer_id, staff_name))

    if missing or duplicates or trainer_problems:
        affected = sorted(
            [d for d in missing]
            + [d for d in duplicates if d in (window_days or [])]
            + [p.split(" ", 1)[0] for p in trainer_problems]
        )
        if duplicates:
            raise GlofoxDataError(
                "duplicate 8:30 AM Level 6 Training class for dates: "
                + ", ".join(duplicates)
            )
        raise GlofoxDataError(
            "incomplete trainer resolution for dates: " + ", ".join(affected)
        )

    rows: list[dict] = []
    for day, event, trainer_id, trainer_name in resolved:
        rows.append(
            {
                "date": day,
                "class_name": event["name"],
                "trainer_id": trainer_id,
                "trainer_name": trainer_name,
                "event_id": event["_id"],
            }
        )
    return rows


def _expected_window_dates(
    events: list[dict],
    reference_date: str | datetime | None,
) -> list[str]:
    """Mon-Sat dates of the computed window (deterministic recompute).

    Recomputing from the SAME reference guarantees the completeness check
    matches the query window exactly — the API could not omit the window
    anyway (branch_id and bounds are pinned), so this never silently
    reuses prior data.
    """
    reference = _reference_in_branch_tz(reference_date)
    week_start, week_end = _next_monday_to_saturday_bounds(reference)
    dates = []
    cursor = week_start.date()
    end_date = week_end.date()
    while cursor <= end_date:
        if cursor.weekday() in EXPECTED_CLASS_WEEKDAYS:
            dates.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return dates


def _trusted_week_dates(dates) -> list[str]:
    """Validate an EXPLICIT trusted week from a validated Facebook post.

    The one and only way a non-clock-driven window reaches the query. Values
    originate in :mod:`level6_weekly` parsing a validated post; they are never
    user text, a URL, a command suffix, or another caller surface. Fail
    closed (``GlofoxSchemaError``) unless the input is EXACTLY six UNIQUE,
    CONSECUTIVE Monday-through-Saturday America/New_York calendar dates — so
    a missing day, a duplicate, a Sunday, a non-week run, or a stray value can
    never be silently reconciled into a week. Returns the canonical
    Monday..Saturday ISO date list.
    """
    if isinstance(dates, (str, bytes)) or not isinstance(dates, (list, tuple)):
        raise GlofoxSchemaError("a trusted week must be a list of dates")
    parsed: list[date] = []
    for raw in dates:
        # A datetime is a date SUBCLASS: reject it explicitly so a stray
        # timestamp can never be treated as a bare calendar day.
        if isinstance(raw, datetime):
            raise GlofoxSchemaError("a trusted week date is not a plain date")
        if isinstance(raw, date):
            parsed.append(raw)
            continue
        text = str(raw or "").strip()
        try:
            parsed.append(date.fromisoformat(text))
        except ValueError as exc:
            raise GlofoxSchemaError(
                f"trusted week date {text!r} is not an ISO calendar date"
            ) from exc
    if len(parsed) != TRUSTED_WEEK_LENGTH:
        raise GlofoxSchemaError(
            f"a trusted week must be exactly {TRUSTED_WEEK_LENGTH} "
            f"Monday-Saturday dates; got {len(parsed)}"
        )
    ordered = sorted(parsed)
    if len(set(ordered)) != len(ordered):
        raise GlofoxSchemaError("a trusted week contains duplicate dates")
    if ordered[0].weekday() != 0 or ordered[-1].weekday() != 5:
        raise GlofoxSchemaError(
            "a trusted week must run Monday through Saturday"
        )
    for offset, day in enumerate(ordered):
        if (day - ordered[0]).days != offset:
            raise GlofoxSchemaError(
                "a trusted week's dates are not six consecutive days"
            )
    return [day.isoformat() for day in ordered]


def _explicit_week_bounds(window_days: list[str]) -> tuple[datetime, datetime]:
    """Mon 00:00:00 → Sat 23:59:59 instants for an EXPLICIT trusted week.

    Unlike the future-facing "next week" window there is NO "strictly after
    now" requirement: the dates came from a validated post, not the clock, and
    a newest post whose week has already started is still valid. Only the
    month/day edges and DST unambiguous-ness are enforced.
    """
    monday = date.fromisoformat(window_days[0])
    saturday = date.fromisoformat(window_days[-1])
    start = datetime.combine(monday, time.min, BRANCH_TZ)
    end = datetime.combine(saturday, time(23, 59, 59), BRANCH_TZ)
    _validate_window_edges(start, end)
    return start, end
