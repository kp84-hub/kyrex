#!/usr/bin/env python3
"""Deterministic tests for glofox_api.py + its production Bot-task wiring.

Mocked suite (always runs) proves: exact URL/method/body restrictions of
the CORRECTED class-day-view variant (no filter=timeslot;
include=trainers,facility,program,users_booked; private=false; uppercase
type=TRAINER staff roster), token redaction, timeout/size/page bounds,
FUTURE Monday-Saturday week selection for every weekday and Sunday
(DST-aware), Sunday exclusion, past-date exclusion, exact 8:30 Group
Fitness filtering, trainer joins against BOTH the embedded trainers_obj
AND the type=TRAINER staff roster, fail-closed trainer/data matrix, the
production routing guards (resolve_executor + in-process task path),
the on_result terminal-status capture, dev_bot.submit_glofox_task /
glofox_route_ready gates, the pinned routine step shape, and the absence
of generalized inputs.

Live suite (opt-in): KYREX_GLOFOX_LIVE=1 (read-only GETs against the
real pinned Level 6 branch; tokens redacted).

Run: python3 test_glofox_api.py   (test_flux.py harness style)
"""
import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

_TMPDIR = tempfile.mkdtemp(prefix="kyrex_glofox_")
os.environ["KYREX_DATA_DIR"] = _TMPDIR  # durable-task + audit store stays isolated

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "web", "backend"))

import glofox_api  # noqa: E402
import serve  # noqa: E402
import task_store  # noqa: E402
import bots as _bots  # noqa: E402
import dev_bot  # noqa: E402
import routines as routines_core  # noqa: E402
from glofox_api import (  # noqa: E402
    BRANCH_ID,
    GlofoxAuthError,
    GlofoxDataError,
    GlofoxSchemaError,
    GlofoxTransportError,
    MAX_PAGES,
    _get_json_list,
    _next_monday_to_saturday_bounds,
    _reference_in_branch_tz,
    get_branch,
    get_trainers,
    get_week_events,
    week_0830_classes,
    _week_0830_classes_for,
)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


TEST_TOKEN = "eyJhbGciOiJIUzI1NiJ9.TV9URUNBVA.TESTSIG"
EDT = ZoneInfo("America/New_York")

# Live-verified trainer IDs → names (level6training roster, type=TRAINER).
T_LAUREN = "677abba49dadb13693001a0a"
T_DONNA = "67a926444b6ceeaa8d087514"
T_EMMITT = "674bc1628c9c1a7ef30d0bcd"
T_AUSTIN = "674bc5a6ec4430b21d083767"
T_LEVEL6_STAFF = "68a8a37c5f45af6f82018065"


def ts(hour, minute, day=21, month=9, year=2026):
    return int(datetime(year, month, day, hour, minute, tzinfo=EDT).timestamp())


def envelope(data, total=None, has_more=False):
    return {
        "object": "list", "page": 1, "limit": 100,
        "total_count": total if total is not None else len(data),
        "has_more": has_more, "data": data,
    }


def trainer_obj(tid, first, last):
    """The embedded trainers_obj shape (user type, NO active field — the
    day-view snippet; area authority for active/type is the roster)."""
    return {"_id": tid, "branch_id": BRANCH_ID, "type": "user",
            "first_name": first, "last_name": last,
            "name": f"{first} {last}".strip()}


def event(eid, when, name="Group Fitness Class", trainers=(),
          obj=None, branch=BRANCH_ID, etype="event", duration=45):
    if obj is None:
        obj = [trainer_obj_obj(trainer_id) for trainer_id in trainers]
    return {
        "_id": eid, "namespace": "leveltraining", "branch_id": branch,
        "name": name, "time_start": when, "type": etype,
        "duration": duration, "trainers": list(trainers),
        "trainers_obj": list(obj),
        "size": 1, "booked": 0, "status": "AVAILABLE",
    }


def trainer_obj_obj(tid):
    """One embedded trainers_obj entry (static, mirrors the live schema)."""
    names = {
        T_LAUREN: ("Lauren", "Grabianowski"),
        T_DONNA: ("Donna", "Albertone"),
        T_EMMITT: ("Emmitt", "Terrell"),
        T_AUSTIN: ("Austin", "Ordonez"),
        T_LEVEL6_STAFF: ("Staff", "Level 6"),
    }
    first, last = names.get(tid, ("Unknown", "Trainer"))
    return trainer_obj(tid, first, last)


def branch_record(tz="America/New_York"):
    return {"_id": BRANCH_ID, "address": {"timezone_id": tz}}


def staff_record(tid, first, last, *, active=True, btype="trainer",
                 branch=BRANCH_ID):
    return {"_id": tid, "branch_id": branch, "type": btype,
            "active": active, "first_name": first, "last_name": last,
            "name": f"{first} {last}".strip(), "description": ""}


# The 8:30 Group Fitness slots of a Mon 09-21 .. Sat 09-26 window with the
# LIVE-verified trainer assignment per weekday.
WEEK_SLOTS = [(21, "M1", T_DONNA, "Donna", "Albertone"),        # Mon
              (22, "T2", T_EMMITT, "Emmitt", "Terrell"),        # Tue
              (23, "W3", T_DONNA, "Donna", "Albertone"),        # Wed
              (24, "R4", T_AUSTIN, "Austin", "Ordonez"),        # Thu
              (25, "F5", T_AUSTIN, "Austin", "Ordonez"),        # Fri
              (26, "S6", T_LEVEL6_STAFF, "Staff", "Level 6")]   # Sat


def full_week():
    return envelope([
        event(eid, ts(8, 30, day=d), trainers=[tid],
              obj=[trainer_obj(tid, first, last)])
        for d, eid, tid, first, last in WEEK_SLOTS
    ], total=len(WEEK_SLOTS))


def full_roster():
    seen = set()
    records = []
    for _d, _eid, tid, first, last in WEEK_SLOTS:
        if tid in seen:
            continue  # the same trainer may cover several weekdays
        seen.add(tid)
        records.append(staff_record(tid, first, last))
    return envelope(records)


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, size=-1):
        return self._body[:size] if isinstance(size, int) and size > 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def as_body(payload):
    return payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def capture_calls(routes):
    calls = []

    def wrapper(request, timeout=None):
        url = request.full_url
        calls.append({
            "method": request.get_method(), "url": url,
            "data": request.data.decode() if request.data else None,
        })
        for prefix in sorted(routes, key=len, reverse=True):
            if url.startswith(prefix):
                return routes[prefix](request, timeout)
        raise AssertionError(f"unexpected URL {url}")

    return patch.object(urllib.request, "urlopen", wrapper), calls


def responder(payload):
    return lambda request, timeout=None: FakeResponse(as_body(payload))


LOGIN_BODY = {"branch_id": BRANCH_ID, "login": "GUEST", "password": "GUEST"}
GOOD_BRANCH = branch_record()
GOOD_EVENTS = full_week()
GOOD_STAFF = full_roster()


def routes_for(branch, staff, events, token_response=None):
    return {
        "https://api.glofox.com/2.0/login":
            responder(token_response if token_response is not None
                      else {"token": TEST_TOKEN}),
        "https://api.glofox.com/2.0/branches/": responder(branch),
        "https://api.glofox.com/2.0/staff": responder(staff),
        f"https://api.glofox.com/2.0/branches/{BRANCH_ID}/events":
            responder(events),
    }


def run_week(branch, staff, events, reference="2026-09-16"):
    """Run the connector (real guest-login path, mocked network)."""
    routes = routes_for(branch, staff, events)
    patcher, calls = capture_calls(routes)
    with patcher:
        rows = _week_0830_classes_for(reference)
    return rows, calls


# ---------------------------------------------------------------------------
print("\n1. URL/method/body pinning + future window (CORRECTED variant)")


def test_exact_request_set():
    rows, calls = run_week(GOOD_BRANCH, GOOD_STAFF, GOOD_EVENTS)
    check("exactly 4 calls", len(calls) == 4, str(calls))
    methods = sorted(c["method"] for c in calls)
    check("POST + 3 GET only", methods == ["GET", "GET", "GET", "POST"], methods)
    check("all pinned host",
          all(c["url"].startswith("https://api.glofox.com/2.0/") for c in calls))
    post = next(c for c in calls if c["method"] == "POST")
    check("POST URL fixed", post["url"] == "https://api.glofox.com/2.0/login")
    check("POST body fixed GUEST", json.loads(post["data"] or "{}") == LOGIN_BODY)
    ev_calls = [c for c in calls if "/events" in c["url"]]
    check("events URL pinned", bool(ev_calls) and all(
        f"https://api.glofox.com/2.0/branches/{BRANCH_ID}/events" in c["url"]
        and "include=trainers,facility,program,users_booked" in c["url"]
        and "sort_by=time_start" in c["url"]
        and "private=false" in c["url"]
        and "filter=" not in c["url"]          # the appointments variant
        and "model=" not in c["url"]
        for c in ev_calls), ev_calls[0]["url"] if ev_calls else "")
    # Independent future-window recompute for the Wednesday reference.
    ref = datetime(2026, 9, 16, tzinfo=EDT)
    monday = (ref + timedelta(days=7 - ref.weekday()))
    expect_start = int(monday.timestamp())
    expect_end = int((monday + timedelta(days=5, hours=23, minutes=59,
                                         seconds=59)).timestamp())
    check("events window = next Mon-Sat",
          f"start={expect_start}" in ev_calls[0]["url"]
          and f"end={expect_end}" in ev_calls[0]["url"], ev_calls[0]["url"])
    staff_calls = [c for c in calls if "/staff" in c["url"]]
    check("staff URL pinned type=TRAINER (UPPERCASE)", any(
        "https://api.glofox.com/2.0/staff?type=TRAINER" in c["url"]
        and "sort_by=first_name" in c["url"]
        for c in staff_calls))
    check("no lowercase type=trainer anywhere",
          all("type=trainer" not in c["url"] for c in calls))
    dates = [r["date"] for r in rows]
    check("six 8:30 rows Mon-Sat", len(rows) == 6, str(dates))
    # Live-verified per-weekday trainer joins.
    expected = {
        "2026-09-21": ("Donna Albertone", T_DONNA),      # Mon
        "2026-09-22": ("Emmitt Terrell", T_EMMITT),      # Tue
        "2026-09-23": ("Donna Albertone", T_DONNA),      # Wed
        "2026-09-24": ("Austin Ordonez", T_AUSTIN),      # Thu  ← pinned probe
        "2026-09-25": ("Austin Ordonez", T_AUSTIN),      # Fri
        "2026-09-26": ("Staff Level 6", T_LEVEL6_STAFF), # Sat
    }
    ok = all(r["trainer_name"] == expected[r["date"]][0]
             and r["trainer_id"] == expected[r["date"]][1]
             and r["class_name"] == "Group Fitness Class"
             for r in rows)
    check("Mon→Donna, Tue→Emmitt, Wed→Donna, Thu→Austin, Fri→Austin, Sat→Staff",
          ok, str(rows[:2]))


# ---------------------------------------------------------------------------
print("\n2. Token redaction")


def test_token_never_leaks():
    routes = routes_for(branch_record("Mars/Pole"), GOOD_STAFF,
                        envelope([], total=0))
    patcher, _ = capture_calls(routes)
    with patcher:
        try:
            _week_0830_classes_for("2026-09-16", _guest_login=lambda: TEST_TOKEN)
            check("timezone failure raised", False)
        except GlofoxSchemaError as exc:
            check("timezone failure raised", True)
            check("token absent from error", TEST_TOKEN not in str(exc))
    rows, _ = run_week(GOOD_BRANCH, GOOD_STAFF, GOOD_EVENTS)
    check("rows carry only declared fields", rows and all(
        set(r) == {"date", "class_name", "trainer_id", "trainer_name",
                   "event_id"} for r in rows))
    reason = glofox_api._scrubbed_transport_reason(
        TimeoutError("connect timeout x" * 90))
    check("scrubbed reason bounded", len(reason) <= 201)


# ---------------------------------------------------------------------------
print("\n3. Bounds: response size, pagination, timeouts")


def test_bounds():
    big = glofox_api.MAX_RESPONSE_BYTES + 1024
    try:
        with patch.object(urllib.request, "urlopen",
                          lambda r, timeout=None: FakeResponse(b"x" * big)):
            _get_json_list("https://api.glofox.com/2.0/staff", TEST_TOKEN)
        check("oversize body rejected", False)
    except GlofoxTransportError:
        check("oversize body rejected", True)

    def has_more_forever(request, timeout=None):
        page = int(request.full_url.rsplit("page=", 1)[-1])
        return FakeResponse(as_body(
            envelope([event(f"p{page}e", ts(8, 30, day=21),
                            trainers=[T_DONNA])], has_more=True)))

    try:
        with patch.object(urllib.request, "urlopen", has_more_forever):
            get_week_events(TEST_TOKEN, "2026-09-16")
        check("pagination beyond MAX_PAGES fails", False)
    except GlofoxSchemaError:
        check("pagination beyond MAX_PAGES fails", True)
    check("MAX_PAGES is 2", MAX_PAGES == 2)

    partial = envelope([event("e1", ts(8, 30, day=21), trainers=[T_DONNA])],
                       total=3, has_more=False)
    try:
        with patch.object(urllib.request, "urlopen",
                          lambda r, timeout=None: FakeResponse(as_body(partial))):
            get_week_events(TEST_TOKEN, "2026-09-16")
        check("partial data rejected", False)
    except GlofoxSchemaError:
        check("partial data rejected", True)

    check("REQUEST_TIMEOUT bounded", glofox_api.REQUEST_TIMEOUT <= 10.0)
    check("MAX_RESPONSE_BYTES bounded",
          glofox_api.MAX_RESPONSE_BYTES <= 256 * 1024)


# ---------------------------------------------------------------------------
print("\n4. Week selection: every weekday, Sunday, past exclusion, DST")


def test_week_selection():
    # Sunday-evening workflow: Sunday chooses TOMORROW (the next Monday).
    s, e = _next_monday_to_saturday_bounds(_reference_in_branch_tz("2026-09-13"))
    check("Sunday -> tomorrow", (s.strftime("%F"), e.strftime("%F")) ==
          ("2026-09-14", "2026-09-19"), s.strftime("%F"))

    expectations = {
        "2026-09-14": ("2026-09-21", "2026-09-26"),   # Mon
        "2026-09-15": ("2026-09-21", "2026-09-26"),   # Tue
        "2026-09-16": ("2026-09-21", "2026-09-26"),   # Wed
        "2026-09-17": ("2026-09-21", "2026-09-26"),   # Thu
        "2026-09-18": ("2026-09-21", "2026-09-26"),   # Fri
        "2026-09-19": ("2026-09-21", "2026-09-26"),   # Sat
        "2026-09-20": ("2026-09-21", "2026-09-26"),   # Sun
    }
    ok = True
    for day, want in expectations.items():
        s, e = _next_monday_to_saturday_bounds(_reference_in_branch_tz(day))
        if (s.strftime("%F"), e.strftime("%F")) != want \
                or s.weekday() != 0 or e.weekday() != 5 \
                or not s > _reference_in_branch_tz(day):
            ok = False
            break
    check("every weekday -> next Mon-Sat, start strictly future", ok, day)

    check("Sunday always excluded from the window",
          all(_next_monday_to_saturday_bounds(
              _reference_in_branch_tz(d))[1].strftime("%a") == "Sat"
              for d in expectations))

    for day in ("2026-03-08", "2026-11-01"):   # 2026 DST transitions
        s, e = _next_monday_to_saturday_bounds(_reference_in_branch_tz(day))
        check(f"DST {day}: unambiguous Mon-Sat window",
              s.weekday() == 0 and e.weekday() == 5
              and s.utcoffset() == e.utcoffset()
              and s.strftime("%F") in ("2026-03-09", "2026-11-02"),
              s.strftime("%F"))
    s, e = _next_monday_to_saturday_bounds(_reference_in_branch_tz("2026-11-01"))
    check("fall-back Sunday: Sat end is wall-clock sane EST",
          e.utcoffset().total_seconds() == -5 * 3600)

    # Exact 8:30 Group Fitness filter over the complete future week,
    # with decoy 8:29/8:31/other-hour events and non-target classes.
    evs = [
        event("d1", ts(8, 29, day=21), name="Group Fitness Class"),
        event("d2", ts(8, 31, day=22), name="Group Fitness Class"),
        event("d3", ts(9, 15, day=23), name="Group Fitness Class"),
        event("d4", ts(7, 45, day=25), name="Group Fitness Class"),
        *[event(f"m{i}", ts(8, 30, day=d), trainers=[tid], obj=[obj])
          for i, (d, eid, tid, first, last) in enumerate(WEEK_SLOTS)
          for obj in [trainer_obj_for(tid)]],
    ]
    rows, _ = run_week(GOOD_BRANCH, GOOD_STAFF, envelope(evs, total=len(evs)))
    got = [(r["date"], r["event_id"]) for r in rows]
    check("exact 08:30 filter over complete week", got == [
        (f"2026-09-{d}", f"m{i}")
        for i, (d, eid_1, tid, first, last) in enumerate(WEEK_SLOTS)], got)
    check("per-weekday trainer joins correct", all(
        r["trainer_name"] == {
            "2026-09-21": "Donna Albertone",
            "2026-09-22": "Emmitt Terrell",
            "2026-09-23": "Donna Albertone",
            "2026-09-24": "Austin Ordonez",
            "2026-09-25": "Austin Ordonez",
            "2026-09-26": "Staff Level 6",
        }[r["date"]] for r in rows), str([r["trainer_name"] for r in rows]))


def trainer_obj_for(tid):
    """Same embedded trainers_obj shape (helper to keep fixtures tight)."""
    names = {
        T_LAUREN: ("Lauren", "Grabianowski"),
        T_DONNA: ("Donna", "Albertone"),
        T_EMMITT: ("Emmitt", "Terrell"),
        T_AUSTIN: ("Austin", "Ordonez"),
        T_LEVEL6_STAFF: ("Staff", "Level 6"),
    }
    first, last = names.get(tid, ("Unknown", "Trainer"))
    return {"_id": tid, "branch_id": BRANCH_ID, "type": "user",
            "first_name": first, "last_name": last,
            "name": f"{first} {last}".strip()}


# ---------------------------------------------------------------------------
print("\n5. Trainer/data fail-closed matrix")


def test_trainer_failures():
    # Missing trainer id → whole request fails with ONLY the affected date.
    evs = full_week()
    evs["data"][3]["trainers"] = []                # Thu 09-24: Austin missing
    try:
        run_week(GOOD_BRANCH, GOOD_STAFF, evs)
        check("missing trainer id fails whole request", False)
    except GlofoxDataError as exc:
        check("unresolved trainer fails whole request", True)
        check("only affected date listed",
              "2026-09-24" in str(exc)
              and "2026-09-21" not in str(exc), str(exc))

    # Trainer-less 8:30 class → NO successful null-trainer row exists.
    evs = full_week()
    for row in evs["data"]:
        row["trainers"] = []
        row["trainers_obj"] = []
    try:
        rows = run_week(GOOD_BRANCH, GOOD_ROSTER_EMPTY(), evs)[0]
        check("no null-trainer success row", not rows, str(rows))
    except GlofoxDataError:
        check("no null-trainer success row", True)

    # Ambiguous mapping (two trainer ids) → fail.
    evs = full_week()
    evs["data"][1]["trainers"] = [T_EMMITT, T_AUSTIN]
    try:
        run_week(GOOD_BRANCH, GOOD_STAFF, evs)
        check("ambiguous trainer ids fail", False)
    except GlofoxDataError:
        check("ambiguous trainer ids fail", True)

    # Inactive trainer (roster active=False) → fail.
    evs = full_week()
    inactive = full_roster()
    for i, rec in enumerate(inactive["data"]):
        if rec["_id"] == T_AUSTIN:
            inactive["data"][i] = {**rec, "active": False}
    try:
        run_week(GOOD_BRANCH, inactive, evs)
        check("inactive trainer fails", False)
    except GlofoxDataError:
        check("inactive trainer fails", True)

    # Foreign-branch trainer in the roster → fail.
    foreign = full_roster()
    foreign["data"][0]["branch_id"] = "999999999999999999999999"
    try:
        run_week(GOOD_BRANCH, foreign, full_week())
        check("foreign-branch trainer fails", False)
    except GlofoxDataError:
        check("foreign-branch trainer fails", True)

    # trainers_obj missing for the day → fail (even though roster has it).
    evs = full_week()
    evs["data"][4]["trainers_obj"] = []            # Friday
    try:
        run_week(GOOD_BRANCH, GOOD_STAFF, evs)
        check("trainers_obj missing fails", False)
    except GlofoxDataError:
        check("trainers_obj missing fails", True)

    # trainers_obj name ≠ roster name → fail (ambiguous, not inferable).
    evs = full_week()
    evs["data"][3]["trainers_obj"] = [trainer_obj(
        T_AUSTIN, "Somebody", "Else")]
    try:
        run_week(GOOD_BRANCH, GOOD_STAFF, evs)
        check("name mismatch fails", False)
    except GlofoxDataError:
        check("name mismatch fails", True)

    # Duplicate 8:30 Group Fitness on one day → duplicate fail.
    evs = full_week()
    evs["data"] = list(evs["data"]) + [
        event("extra", ts(8, 30, day=21), trainers=[T_DONNA],
              obj=[trainer_obj(T_DONNA, "Donna", "Albertone")])]
    evs["total_count"] = len(evs["data"])
    try:
        run_week(GOOD_BRANCH, GOOD_STAFF, evs)
        check("duplicate 8:30 class fails", False)
    except GlofoxDataError as exc:
        check("duplicate 8:30 class fails", True)
        check("duplicate message is date-scoped",
              "duplicate 8:30" in str(exc) and "2026-09-21" in str(exc),
              str(exc))

    # Incomplete week: Thursday's 8:30 event missing → fail, date listed.
    evs = full_week()
    evs["data"] = [ev for ev in evs["data"] if ev["_id"] != "R4"]
    evs["total_count"] = len(evs["data"])
    try:
        run_week(GOOD_BRANCH, GOOD_STAFF, evs)
        check("incomplete week fails", False)
    except GlofoxDataError as exc:
        check("incomplete week fails", True)
        check("missing day listed", "2026-09-24" in str(exc), str(exc))

    # Complete week with resolved trainers succeeds — six joined rows.
    rows, _ = run_week(GOOD_BRANCH, GOOD_STAFF, full_week())
    check("complete week succeeds with trainers", len(rows) == 6 and all(
        r["trainer_id"] and r["trainer_name"] for r in rows),
        str([r["trainer_name"] for r in rows]))

    # Wrong 8:30 class NAME (non-target) → fail closed (exact binding).
    evs = full_week()
    evs["data"][0]["name"] = "Mystery Class"
    try:
        run_week(GOOD_BRANCH, GOOD_STAFF, evs)
        check("wrong 8:30 class name fails", False)
    except GlofoxDataError:
        check("wrong 8:30 class name fails", True)

    # Duplicate roster record rejected.
    dup = full_roster()
    dup["data"].append(dict(dup["data"][0]))
    try:
        with patch.object(urllib.request, "urlopen",
                          lambda r, timeout=None: FakeResponse(as_body(dup))):
            get_trainers(TEST_TOKEN)
        check("duplicate staff id rejected", False)
    except GlofoxDataError:
        check("duplicate staff id rejected", True)

    # Duplicate event id rejected.
    dup_ev = event("d1", ts(8, 30, day=21), trainers=[T_DONNA],
                   obj=[trainer_obj(T_DONNA, "Donna", "Albertone")])
    try:
        with patch.object(urllib.request, "urlopen",
                          lambda r, timeout=None: FakeResponse(as_body(
                              envelope([dup_ev, dup_ev], total=2)))):
            get_week_events(TEST_TOKEN, "2026-09-16")
        check("duplicate event id rejected", False)
    except GlofoxDataError:
        check("duplicate event id rejected", True)


def GOOD_ROSTER_EMPTY():
    return envelope([])


# ---------------------------------------------------------------------------
print("\n6. Fail-closed: schema, transport, auth")


def test_failures_closed():
    cases = [
        ("not JSON", b"nope", GlofoxSchemaError),
        ("wrong object key", {"object": "other", "data": [], "total_count": 0,
                              "has_more": False}, GlofoxSchemaError),
        ("negative total", {"object": "list", "data": [], "total_count": -1,
                            "has_more": False}, GlofoxSchemaError),
        ("has_more non-bool", {"object": "list", "data": [], "total_count": 0,
                               "has_more": 0}, GlofoxSchemaError),
        ("data non-list", {"object": "list", "data": {}, "total_count": 0,
                           "has_more": False}, GlofoxSchemaError),
        ("success:false", {"success": False}, GlofoxTransportError),
    ]
    for label, payload, expected in cases:
        body = as_body(payload)
        try:
            with patch.object(urllib.request, "urlopen",
                              lambda r, timeout=None: FakeResponse(body)):
                _get_json_list("https://api.glofox.com/2.0/staff", TEST_TOKEN)
            check(label, False)
        except expected:
            check(label, True)
        except Exception as exc:  # noqa: BLE001
            check(label, False, f"unexpected {exc!r}")

    # Event type must be the class-day-view 'event' (NOT the legacy
    # 'timeslot' type of the appointment variant).
    try:
        glofox_api._validate_event_schema(
            event("x", ts(8, 30, day=21), etype="timeslot", trainers=[T_DONNA],
                  obj=[trainer_obj(T_DONNA, "Donna", "Albertone")]))
        check("legacy timeslot type rejected", False)
    except GlofoxSchemaError:
        check("legacy timeslot type rejected", True)

    try:
        glofox_api._validate_event_schema(
            event("x", ts(8, 30, day=21), branch="other", trainers=[T_DONNA],
                  obj=[trainer_obj(T_DONNA, "Donna", "Albertone")]))
        check("foreign branch event rejected", False)
    except GlofoxSchemaError:
        check("foreign branch event rejected", True)

    try:
        with patch.object(urllib.request, "urlopen",
                          lambda r, timeout=None: FakeResponse(as_body(
                              branch_record("Asia/Tokyo")))):
            get_branch(TEST_TOKEN)
        check("non-pinned timezone rejected", False)
    except GlofoxSchemaError:
        check("non-pinned timezone rejected", True)

    try:
        _reference_in_branch_tz("17-09-2026")
        check("bad reference date format rejected", False)
    except GlofoxSchemaError:
        check("bad reference date format rejected", True)

    try:
        with patch.object(urllib.request, "urlopen",
                          lambda r, timeout=None: FakeResponse(as_body(
                              {"success": False}))):
            glofox_api._post_guest_login()
        check("guest token absent rejected", False)
    except GlofoxAuthError:
        check("guest token absent rejected", True)
    try:
        with patch.object(urllib.request, "urlopen",
                          lambda r, timeout=None: FakeResponse(as_body(
                              {"token": TEST_TOKEN}))):
            got = glofox_api._post_guest_login()
        check("guest token accepted", got == TEST_TOKEN)
    except GlofoxAuthError as exc:
        check("guest token accepted", False, str(exc))

    try:
        with patch.object(urllib.request, "urlopen",
                          lambda r, timeout=None: FakeResponse(as_body(
                              {"_id": "other", "address": {}}))):
            get_branch("wrong-token")
        check("branch id mismatch rejected", False)
    except GlofoxSchemaError:
        check("branch id mismatch rejected", True)


# ---------------------------------------------------------------------------
print("\n7. No write-capable surface")


def test_no_write_surface():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(glofox_api))
    posts = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "Request"
             and any(kw.arg == "method" and isinstance(kw.value, ast.Constant)
                     and kw.value.value == "POST" for kw in n.keywords)]
    check("exactly one POST constructor", len(posts) == 1)
    if posts:
        check("POST targets login only",
              "API_LOGIN_URL" in ast.unparse(posts[0]))
    sig = inspect.signature(week_0830_classes)
    check("public entry has no date parameter",
          set(sig.parameters) <= {"_guest_login"}, str(sig))
    public = [n for n, fn in inspect.getmembers(glofox_api, inspect.isfunction)
              if not fn.__name__.startswith("_")]
    check("public surface is the pinned read set",
          set(public) == {"get_branch", "get_trainers", "get_week_events",
                          "week_0830_classes"}, str(public))
    check("no mutating verbs in public API",
          not any(v in " ".join(public)
                  for v in ("delete", "push", "create", "send")))


# ---------------------------------------------------------------------------
print("\n8. Production routing: exact Bot task path")


def _bounded_ctx(policy):
    return serve.ExecutionContext(
        session_id="glofox-op", rift_path=None, policy=dict(policy),
        bot_id="level6bot", bot_owner="owner1",
    )


def _relay_store(task_id="task-1"):
    return patch.object(
        task_store.CloudTaskStore, "get",
        lambda self, t: {"task_id": t, "status": task_store.STATUS_RUNNING,
                         "cancel_requested": False, "text": ""},
    )


def test_production_routing():
    # resolve_executor accepts ONLY the exact structured request.
    p, r, unk = serve.resolve_executor("glofox: schedule")
    check("glofox: schedule routes to glofox", (p, r) == ("glofox", "schedule"))
    p, r, unk = serve.resolve_executor("glofox: delete everything")
    check("other glofox task text rejected", p is None and unk == "glofox")
    p, r, unk = serve.resolve_executor("weather: tomorrow")
    check("unknown prefixes still rejected", p is None and unk == "weather")

    ctx = _bounded_ctx({"glofox:read": 0})
    deterministic_rows = [
        {"date": "2026-09-24", "class_name": "Group Fitness Class",
         "trainer_id": T_AUSTIN, "trainer_name": "Austin Ordonez",
         "event_id": "R4"},
        {"date": "2026-09-26", "class_name": "Group Fitness Class",
         "trainer_id": T_LEVEL6_STAFF, "trainer_name": "Staff Level 6",
         "event_id": "S6"},
    ]

    # Bound bot + exact grant + deterministic connector rows →
    # BOTH result capture (durable terminal state) AND relay.
    sent = []
    captured = {}
    with _relay_store(), \
         patch.object(task_store.CloudTaskStore, "is_cancel_requested",
                      lambda self, t: False), \
         patch.object(glofox_api, "week_0830_classes",
                      return_value=deterministic_rows):
        serve._run_glofox_schedule_task(
            ctx, chat_id=1, task_text=serve.GLOFOX_TASK_TEXT,
            task_id="task-1",
            send=lambda cid, txt: sent.append(txt),
            on_result=lambda result: captured.append(result))
    check("bound bot + exact grant relays result",
          len(sent) == 1 and "Glofox" in sent[0]
          and "failed closed" not in sent[0], str(sent))
    check("on_result captured the structured rows", len(captured) == 1
          and captured[0].get("rows") == deterministic_rows
          and captured[0].get("count") == 2, str(captured))

    # No exact grant (even with wildcards) → explicit policy denial.
    sent.clear()
    ctx_wild = serve.ExecutionContext(
        session_id="anon", policy={"fs:read": 0, "*": 0},
        bot_id="smuggler", bot_owner="owner2",
    )
    serve._run_glofox_schedule_task(
        ctx_wild, chat_id=1, task_text=serve.GLOFOX_TASK_TEXT,
        task_id="task-1", send=lambda cid, txt: sent.append(txt),
    )
    check("wildcard-only policy denied", len(sent) == 1
          and "no exact glofox:read grant" in sent[0], str(sent))

    # Unbound identity (bot_id == executor prefix, no owner) denied.
    sent.clear()
    ctx_unbound = serve.ExecutionContext(
        session_id="glofox", policy={"glofox:read": 0}, bot_id="glofox",
        bot_owner="",
    )
    serve._run_glofox_schedule_task(
        ctx_unbound, chat_id=1, task_text=serve.GLOFOX_TASK_TEXT,
        task_id="task-1", send=lambda cid, txt: sent.append(txt),
    )
    check("unbound identity denied",
          bool(sent) and "failed closed" in sent[0], str(sent))

    # Non-structured task text denied.
    sent.clear()
    serve._run_glofox_schedule_task(
        ctx, chat_id=1, task_text="glofox: delete everything",
        task_id="task-1", send=lambda cid, txt: sent.append(txt),
    )
    check("non-structured request denied",
          bool(sent) and "unsupported Glofox request" in sent[0], str(sent))

    # Cancelled BEFORE the network work → nothing relayed.
    sent.clear()
    with patch.object(task_store.CloudTaskStore, "get",
                      lambda self, t: {"task_id": t, "status": "running",
                                       "cancel_requested": True, "text": ""}):
        serve._run_glofox_schedule_task(
            ctx, chat_id=1, task_text=serve.GLOFOX_TASK_TEXT,
            task_id="task-1", send=lambda cid, txt: sent.append(txt),
        )
    check("cancelled (pre-run) relays nothing",
          bool(sent) and "cancelled" in sent[0], str(sent))

    # Cancelled AFTER the network work → nothing relayed.
    sent.clear()
    with _relay_store(), \
         patch.object(task_store.CloudTaskStore, "is_cancel_requested",
                      lambda self, t: True), \
         patch.object(glofox_api, "week_0830_classes",
                      return_value=deterministic_rows):
        serve._run_glofox_schedule_task(
            ctx, chat_id=1, task_text=serve.GLOFOX_TASK_TEXT,
            task_id="task-1", send=lambda cid, txt: sent.append(txt),
        )
    check("cancelled (post-run) relays nothing",
          bool(sent) and "cancelled" in sent[0], str(sent))

    # Connector failure → the REAL terminal error is reported.
    sent.clear()
    with _relay_store(), \
         patch.object(task_store.CloudTaskStore, "is_cancel_requested",
                      lambda self, t: False), \
         patch.object(glofox_api, "week_0830_classes",
                      side_effect=glofox_api.GlofoxTransportError(
                          "HTTP 503 from api.glofox.com")):
        serve._run_glofox_schedule_task(
            ctx, chat_id=1, task_text=serve.GLOFOX_TASK_TEXT,
            task_id="task-1", send=lambda cid, txt: sent.append(txt),
        )
    check("connector error is the real terminal reason",
          bool(sent) and "HTTP 503" in sent[0]
          and "failed closed" in sent[0], str(sent))

    # Incomplete-week data failure surfaces the affected dates verbatim.
    sent.clear()
    with _relay_store(), \
         patch.object(task_store.CloudTaskStore, "is_cancel_requested",
                      lambda self, t: False), \
         patch.object(glofox_api, "week_0830_classes",
                      side_effect=glofox_api.GlofoxDataError(
                          "incomplete trainer resolution for dates: 2026-09-24")):
        serve._run_glofox_schedule_task(
            ctx, chat_id=1, task_text=serve.GLOFOX_TASK_TEXT,
            task_id="task-1", send=lambda cid, txt: sent.append(txt),
        )
    check("incomplete week report relays the affected dates",
          bool(sent) and "2026-09-24" in sent[0]
          and "failed closed" in sent[0], str(sent))


# ---------------------------------------------------------------------------
print("\n9. dev_bot / Routine submission gates")


def _bound_bot(**over):
    bot = {"id": "level6bot", "owner": "owner1",
           "policy": {"glofox:read": 0}, "status": "running",
           "rift": "/tmp/level6-rift"}
    bot.update(over)
    return bot


def test_submission_gates():
    os.environ.setdefault("KYREX_DATA_DIR", _TMPDIR)
    # glofox_route_ready: running + exact grant → True
    check("running bot + exact grant route-ready",
          dev_bot.glofox_route_ready(_bound_bot()))
    # legacy policy (no glofox:read) → NOT route-ready
    check("legacy policy NOT route-ready",
          not dev_bot.glofox_route_ready(
              _bound_bot(policy={"browser:navigate": 0, "browser:read": 0})))
    # wildcard-only policy → NOT route-ready
    check("wildcard-only policy NOT route-ready",
          not dev_bot.glofox_route_ready(_bound_bot(policy={"*": 0})))
    check("stopped Bot NOT route-ready",
          not dev_bot.glofox_route_ready(_bound_bot(status="stopped")))
    check("write-capable Bot NOT route-ready",
          not dev_bot.glofox_route_ready(
              _bound_bot(policy={"fs:write": 1})))
    check("globally denied write op still NOT route-ready",
          not dev_bot.glofox_route_ready(
              _bound_bot(policy={"glofox:read": 0, "fs:write": 1})))

    # submit_glofox_task: exact text + eligibility; wrong text rejected.
    from task_store import CloudTaskStore
    fake_store = _FakeTaskStore()
    try:
        dev_bot.submit_glofox_task("owner1", _bound_bot(), "glofox: schedule",
                                   store=fake_store)
        check("pinned command accepted", True)
    except Exception as exc:
        check("pinned command accepted", False, repr(exc))
    check("durable task stored with pinned text", bool(fake_store.submitted) and
          fake_store.submitted[0][1]["task_text"] == serve.GLOFOX_TASK_TEXT
          and fake_store.submitted[0][1]["executor_prefix"] == "glofox"
          and fake_store.submitted[0][1]["session_key"] == "level6bot",
          str(fake_store.submitted[0][1] if fake_store.submitted else None))
    try:
        dev_bot.submit_glofox_task("owner1", _bound_bot(),
                                   "glofox: delete everything", store=fake_store)
        check("wrong task text rejected", False)
    except dev_bot.DevBotError:
        check("wrong task text rejected", True)
    try:
        dev_bot.submit_glofox_task(
            "owner1", _bound_bot(policy={"fs:write": 1}),
            serve.GLOFOX_TASK_TEXT, store=fake_store)
        check("write-capable rejection", False)
    except dev_bot.DevBotError:
        check("write-capable rejection", True)
    try:
        dev_bot.submit_glofox_task(
            "owner1", _bound_bot(status="stopped"),
            serve.GLOFOX_TASK_TEXT, store=fake_store)
        check("stopped rejection at submission", False)
    except dev_bot.DevBotError:
        check("stopped rejection at submission", True)

    # EXACT lockstep: the browser preset NOW includes glofox:read.
    preset = serve.browser_preset_policy()
    check("preset grants browser nav+read", preset.get(
        "browser:navigate") == 0 and preset.get("browser:read") == 0)
    check("preset grants glofox:read", preset.get("glofox:read") == 0)
    check("preset grants NOTHING else",
          set(preset) == {"browser:navigate", "browser:read", "glofox:read"},
          str(preset))
    check("presets classifier in lockstep (updated shape matches)",
          serve.is_browser_bot_policy(preset))
    check("legacy policy (no glofox:read) is NOT classified",
          not serve.is_browser_bot_policy(
              {"browser:navigate": 0, "browser:read": 0}))
    check("legacy Bots did not silently gain the grant",
          not serve.glofox_read_granted(
              {"browser:navigate": 0, "browser:read": 0}))


class _FakeTaskStore:
    """Minimal durable-store double: records exactly one submit."""

    def __init__(self):
        self._submitted = []

    def submit(self, *args, **kwargs):
        self._submitted = [(args, kwargs)]
        return "task-1"

    @property
    def submitted(self):
        return self._submitted


# ---------------------------------------------------------------------------
print("\n10. Routine glofox.schedule step shape (bounded, singleton)")


def test_routine_step_shape():
    v = routines_core
    # The exact routine: ONE glofox.schedule step. Valid.
    steps = v.validate_steps([{"action": "glofox.schedule"}])
    check("singleton glofox.schedule validates", len(steps) == 1 and
          steps[0]["action"] == "glofox.schedule")

    # Mixed (glofox + navigate) → rejected.
    try:
        v.validate_steps([
            {"action": "glofox.schedule"},
            {"action": "navigate", "url": "https://example.com"},
        ])
        check("glofox+navigate mixture rejected", False)
    except v.RoutineValidationError:
        check("glofox+navigate mixture rejected", True)

    # Extra field on the glofox step → rejected.
    try:
        v.validate_steps([
            {"action": "glofox.schedule", "url": "https://evil.example.com"}])
        check("extra field on glofox step rejected", False)
    except v.RoutineValidationError:
        check("extra field on glofox step rejected", True)

    # No arbitrary-date/HTTP surface: 2 glofox steps → rejected.
    try:
        v.validate_steps([
            {"action": "glofox.schedule"}, {"action": "glofox.schedule"}])
        check("2 glofox steps rejected", False)
    except v.RoutineValidationError:
        check("2 glofox steps rejected", True)

    # The compiled browser text for a glofox-only routine is EMPTY (no
    # browser steps exist; submission uses the glofox executor instead).
    check("browser task text empty for glofox-only",
          v.browser_task_text_for([{"action": "glofox.schedule"}]) == "")

    # is_glofox_schedule_routine: predicate true only for the singleton.
    check("is_glofox_schedule_routine predicate",
          v.is_glofox_schedule_routine([{"action": "glofox.schedule"}])
          and not v.is_glofox_schedule_routine(
              [{"action": "navigate", "url": "https://x.com"}])
          and not v.is_glofox_schedule_routine(
              [{"action": "glofox.schedule"},
               {"action": "glofox.schedule"}]))

    # build_routine_draft glofox path — rows become the redacted draft.
    exec_result = {
        "rows": [{"date": "2026-09-24", "class_name": "Group Fitness Class",
                  "trainer_name": "Austin Ordonez"}],
        "count": 1, "dates": ["2026-09-24"],
    }
    run_rec = {"_steps": [{"action": "glofox.schedule"}]}
    draft = v.build_routine_draft(executor_result=exec_result, run_rec=run_rec)
    check("glofox draft is the formatted schedule",
          "2026-09-24" in draft.get("draft_message", "")
          and "Austin Ordonez" in draft.get("draft_message"),
          str(draft))

    # resolve_browser_bot(schedule_only=True) — requires exact grant.
    bot = _bound_bot()
    with patch.object(routines_core, "_registry",
                      lambda: {"level6bot": bot}):
        resolved = routines_core.resolve_browser_bot(
            "owner1", "level6bot", schedule_only=True)
    check("schedule-only resolution matches exact-grant policy",
          resolved.get("id") == "level6bot")
    refuse_bot = _bound_bot(policy={"browser:navigate": 0, "browser:read": 0})
    try:
        with patch.object(routines_core, "_registry",
                          lambda: {"level6bot": refuse_bot}):
            routines_core.resolve_browser_bot(
                "owner1", "level6bot", schedule_only=True)
        check("schedule-only refuses non-granting policy", False)
    except routines_core.RoutineUnavailable:
        check("schedule-only refuses non-granting policy", True)

    # Legacy check — existing configured Bots cannot silently gain the
    # grant. (The classifier insists on the CURRENT preset shape.)
    check("legacy browser Bot policy does NOT match the updated classifier",
          not serve.is_browser_bot_policy(
              {"browser:navigate": 0, "browser:read": 0}))


if __name__ == "__main__":
    test_exact_request_set()
    test_token_never_leaks()
    test_bounds()
    test_week_selection()
    test_trainer_failures()
    test_failures_closed()
    test_no_write_surface()
    test_production_routing()
    test_submission_gates()
    test_routine_step_shape()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nAll mocked checks passed.")

    if os.environ.get("KYREX_GLOFOX_LIVE") == "1":
        print("\n--- LIVE MODE: real network probes (read-only) ---")
        try:
            rows = week_0830_classes()
            print(json.dumps(rows, indent=2))
            for row in rows:
                date = datetime.strptime(row["date"], "%Y-%m-%d")
                assert 0 <= date.weekday() <= 5, f"Sunday leaked: {row}"
                assert row["trainer_id"] and row["trainer_name"]
            print("Live probe OK (future-week, complete trainers).")
        except glofox_api.GlofoxError as exc:
            print("LIVE fail-closed:", type(exc).__name__, exc)
            print("(fail-closed live outcome accepted by this test)")
    else:
        print("(live test skipped — set KYREX_GLOFOX_LIVE=1)")
