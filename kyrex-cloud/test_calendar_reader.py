#!/usr/bin/env python3
"""Focused tests for the Calendar Reader slice (Google Reader, first slice).

Covers, without any network or real credentials:

  * timezone/DST day boundaries in America/New_York;
  * the Calendar Reader preset's exactness and that NO existing preset is
    widened;
  * byte-exact Chat command routing + fail-closed unsupported calendar;
  * the cal_executor read-only contract for ``calendar: today|tomorrow|week``;
  * the owner-scoped encrypted connector: owner isolation, OAuth replay /
    expiry / owner-binding / redirect-binding, and token redaction at rest and
    in every returned view.

Run: python3 test_calendar_reader.py
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Connector environment BEFORE importing the modules that read it.
os.environ["WEB_SESSION_SECRET"] = "unit-test-connector-secret"
os.environ["KYREX_DATA_DIR"] = tempfile.mkdtemp(prefix="kx-cal-test-")
os.environ.setdefault("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "client-secret-value")
os.environ.setdefault("GOOGLE_REDIRECT_URI",
                      "https://kyrex.example/api/connections/google/callback")

import serve              # noqa: E402
import calendar_windows as cw  # noqa: E402
import connectors as conn      # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          ("" if cond else f"  [{detail}]"))
    if not cond:
        failures.append(name)


EXEC = HERE / "cal_executor.py"


def run_cal(task, verdict="ALLOW\n"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GOOGLE_")}
    proc = subprocess.run(
        [sys.executable, str(EXEC), "--task", task], input=verdict,
        capture_output=True, text=True, timeout=15, env=env)
    res = {}
    for line in proc.stdout.splitlines():
        if line.startswith("KYREX_RESULT_JSON:"):
            res = json.loads(line[len("KYREX_RESULT_JSON:"):])
    return res, proc.stdout.splitlines()


# ── 1. America/New_York boundaries incl. DST ───────────────────────────
print("\n1. timezone / DST day boundaries (America/New_York)")
_lbl, tmin, tmax = cw.window_bounds("today", now=datetime(2025, 3, 9, 12, 0))
check("today starts at LOCAL midnight (spring forward day)",
      tmin == "2025-03-09T00:00:00-05:00", tmin)
check("today ends at next LOCAL midnight (DST offset changed)",
      tmax == "2025-03-10T00:00:00-04:00", tmax)
check("NOT a UTC midnight", not tmin.startswith("2025-03-09T00:00:00+00:00"), tmin)
_lbl2, f_min, f_max = cw.window_bounds("tomorrow", now=datetime(2025, 11, 2, 12, 0))
check("fall-back local day is still one local day (offset changed)",
      f_min == "2025-11-03T00:00:00-05:00" and f_max == "2025-11-04T00:00:00-05:00",
      f"{f_min} -> {f_max}")
_lbl3, w_min, w_max = cw.window_bounds("week", now=datetime(2025, 3, 9, 12, 0))
check("week spans 7 local days",
      w_min == "2025-03-09T00:00:00-05:00" and w_max == "2025-03-16T00:00:00-04:00",
      f"{w_min} -> {w_max}")
try:
    cw.window_bounds("yesterday")
    check("unsupported window rejected", False)
except cw.CalendarWindowError:
    check("unsupported window rejected", True)


# ── 2. Malformed provider response fails closed; rendering is readable ──
print("\n2. provider response handling")
try:
    cw.render_events("Today", {"items": []})
    check("non-list items fail closed", False)
except cw.CalendarWindowError:
    check("non-list items fail closed", True)
text = cw.render_events("Today", [
    {"summary": "Standup",
     "start": {"dateTime": "2025-03-09T13:00:00+00:00"},
     "end": {"dateTime": "2025-03-09T13:15:00+00:00"}}])
check("renders local time + title", "Standup" in text and "9:00 AM" in text, text)
check("renders an event count", "1 event(s)" in text, text)


# ── 3. Calendar Reader preset exactness; no preset widened ─────────────
print("\n3. Calendar Reader preset + policy exactness")
check("preset is exactly {cal:list: 0}",
      serve.CALENDAR_READER_PRESET == {"cal:list": 0},
      str(serve.CALENDAR_READER_PRESET))
check("is_calendar_reader_policy(preset) is True",
      serve.is_calendar_reader_policy(serve.CALENDAR_READER_PRESET) is True)
check("raised tier rejected",
      serve.is_calendar_reader_policy({"cal:list": 1}) is False)
check("extra capability rejected",
      serve.is_calendar_reader_policy({"cal:list": 0, "fs:write": 1}) is False)
check("wildcard never counts",
      serve.is_calendar_reader_policy({"*": 0}) is False)
check("cal:list is host tier 0", serve.derive_host_tier("cal:list") == 0)
check("cal:create is NOT tier 0 (out of scope)", serve.derive_host_tier("cal:create") != 0)
# no existing preset widened / misclassified
for name, preset in [("developer", serve.DEVELOPER_PRESET),
                     ("browser", serve.BROWSER_PRESET),
                     ("glofox-reader", serve.GLOFOX_READER_PRESET)]:
    check(f"existing preset {name} is NOT a Calendar Reader",
          serve.is_calendar_reader_policy(preset) is False, str(preset))
check("browser preset unchanged (no cal:list)",
      "cal:list" not in serve.BROWSER_PRESET, str(serve.BROWSER_PRESET))
check("glofox preset unchanged",
      serve.GLOFOX_READER_PRESET == {"glofox:read": 0},
      str(serve.GLOFOX_READER_PRESET))


# ── 4. resolve_executor exact routing + fail-closed namespace ──────────
print("\n4. resolve_executor routing")
for cmd in ("calendar: today", "calendar: tomorrow", "calendar: week"):
    check(f"{cmd!r} routes to calendar",
          serve.resolve_executor(cmd) == ("calendar", cmd, None),
          str(serve.resolve_executor(cmd)))
for bad in ("calendar: yesterday", "calendar: 7 days", "calendar: next week"):
    prefix, _t, err = serve.resolve_executor(bad)
    check(f"{bad!r} fails closed (no default route)",
          prefix is None and err == "calendar", str(serve.resolve_executor(bad)))


# ── 5. cal_executor read-only contract ─────────────────────────────────
print("\n5. cal_executor contract")
res, _lines = run_cal("calendar: yesterday")
check("unsupported calendar: subcommand -> error",
      res.get("status") == "error" and
      any("unsupported" in (e or "").lower() for e in res.get("errors", [])),
      str(res))
res, lines = run_cal("calendar: today", verdict="DENY\n")
ops = [l for l in lines if l.startswith("KYREX_OPERATION:")]
op = json.loads(ops[0][len("KYREX_OPERATION:"):]) if ops else {}
check("operation is cal.list", op.get("op") == "cal.list", str(op))
check("operation target is the byte-exact command",
      op.get("target") == "calendar: today", str(op))
check("DENY -> error (fail closed)",
      res.get("status") == "error" and
      any("denied" in (e or "").lower() for e in res.get("errors", [])),
      str(res))
# no create path remains (event creation out of scope)
res, _lines = run_cal("create event")
check("create is unsupported (event creation removed)",
      res.get("status") == "error" and
      any("unsupported" in (e or "").lower() for e in res.get("errors", [])),
      str(res))


# ── 6. owner-scoped encrypted connector ────────────────────────────────
print("\n6. connector: owner isolation / OAuth state / redaction")
store = conn.ConnectorStore()

def fake_exchange(code, redirect, client):
    check("exchange uses the redirect bound to the state",
          redirect == os.environ["GOOGLE_REDIRECT_URI"], redirect)
    return {"access_token": "ya29.SECRET-ACCESS",
            "refresh_token": "1//SECRET-REFRESH", "expires_in": 3600,
            "scope": conn.GOOGLE_CALENDAR_READ_SCOPE}

start = store.begin_oauth("alice")
check("connect returns ONLY an authorization url + metadata (no secret)",
      set(start) == {"provider", "state", "authorization_url",
                     "expires_at", "scopes"}, str(sorted(start)))
check("scope is calendar.readonly only",
      start["scopes"] == [conn.GOOGLE_CALENDAR_READ_SCOPE], str(start["scopes"]))
view = store.complete_oauth("alice", start["state"], "authcode", exchange=fake_exchange)
check("connected view has NO token fields",
      view.get("connected") is True and "access_token" not in view
      and "sealed" not in view and "refresh_token" not in view, str(view))
# replay
try:
    store.complete_oauth("alice", start["state"], "authcode", exchange=fake_exchange)
    check("OAuth replay rejected", False)
except conn.OAuthStateError:
    check("OAuth replay rejected", True)
# foreign owner
s2 = store.begin_oauth("alice")
try:
    store.complete_oauth("bob", s2["state"], "code", exchange=fake_exchange)
    check("foreign-owner state rejected", False)
except conn.OAuthStateError:
    check("foreign-owner state rejected", True)
# expiry
s3 = store.begin_oauth("alice", ttl=60, now=1000.0)
try:
    store.complete_oauth("alice", s3["state"], "code", exchange=fake_exchange,
                         now=2000.0)
    check("expired state rejected", False)
except conn.OAuthStateError:
    check("expired state rejected", True)
# redirect binding
s4 = store.begin_oauth("alice")
try:
    store.complete_oauth("alice", s4["state"], "code", exchange=fake_exchange,
                         redirect_uri="https://evil.example/cb")
    check("redirect mismatch rejected", False)
except conn.OAuthStateError:
    check("redirect mismatch rejected", True)
# owner isolation
check("a different owner is NOT connected",
      store.status("bob")["connected"] is False)
# at rest
blob = open(store.path).read()
check("no plaintext access token on disk", "SECRET-ACCESS" not in blob)
check("no plaintext refresh token on disk", "SECRET-REFRESH" not in blob)
check("sealed blob present on disk", '"sealed"' in blob)
check("access token retrievable internally",
      store.access_token("alice").startswith("ya29."))
# disconnect removes the secret
check("disconnect is idempotent",
      store.disconnect("alice") is True and store.disconnect("alice") is False)
check("secret gone after disconnect", "SECRET-ACCESS" not in open(store.path).read())
# CalendarRead fail-closed after disconnect
try:
    store.calendar("alice").events()
    check("read fails closed when disconnected", False)
except conn.ConnectorUnavailable:
    check("read fails closed when disconnected", True)


# ── 7. CalendarRead request shape (bounded, primary, singleEvents) ──────
print("\n7. CalendarRead request shape")
s5 = store.begin_oauth("carol")
store.complete_oauth("carol", s5["state"], "code", exchange=fake_exchange)
seen = {}

def transport(method, url, token, params=None, body=None):
    seen.update(method=method, url=url, params=params, token=token)
    return {"items": []}

store.calendar("carol", transport=transport).events(
    time_min="2025-03-09T00:00:00-05:00", time_max="2025-03-10T00:00:00-04:00")
check("calendarId is primary", seen["url"].endswith("/calendars/primary/events"),
      seen["url"])
check("singleEvents true", seen["params"].get("singleEvents") == "true", str(seen))
check("orderBy startTime", seen["params"].get("orderBy") == "startTime", str(seen))
check("results bounded (maxResults set)",
      isinstance(seen["params"].get("maxResults"), int), str(seen))


print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
