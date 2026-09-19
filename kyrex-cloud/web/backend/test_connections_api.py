"""HTTP-level tests for connections_api.py (Google Calendar, read-only).

Drives the real FastAPI routes with TestClient, monkeypatching ONLY the
owner auth (so no full Cloud app/DB is needed) and the token exchange (so no
network). Verifies:

  * the owner-scoped view NEVER contains a token / sealed blob;
  * connect returns ONLY an authorization URL + metadata;
  * the callback validates the single-use owner-bound state and seals tokens
    server-side; a replay fails closed with a generic page;
  * disconnect is idempotent;
  * owner isolation (a different owner sees "not connected");
  * a missing connector core fails closed with 503.

Run: python3 test_connections_api.py
"""
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent                # web/backend/
CLOUD = HERE.parent.parent                            # kyrex-cloud/
for path in (str(CLOUD), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ["WEB_SESSION_SECRET"] = "unit-test-connector-secret"
os.environ["KYREX_DATA_DIR"] = tempfile.mkdtemp(prefix="kx-conn-api-")
os.environ.setdefault("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "client-secret-value")
os.environ.setdefault("GOOGLE_REDIRECT_URI",
                      "https://kyrex.example/api/connections/google/callback")

from fastapi import FastAPI                     # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

import connections_api                          # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          ("" if cond else f"  [{detail}]"))
    if not cond:
        failures.append(name)


# Fixed owner, swappable for the isolation case.
OWNER = {"id": "alice"}
connections_api._require_user = lambda request: OWNER["id"]
connections_api._DEFAULT_STORE = None  # let it build the temp-dir store

EXPECTED_REDIRECT = os.environ["GOOGLE_REDIRECT_URI"]


def fake_exchange(code, redirect, client):
    assert redirect == EXPECTED_REDIRECT, redirect
    return {"access_token": "ya29.SECRET-ACCESS",
            "refresh_token": "1//SECRET-REFRESH", "expires_in": 3600,
            "scope": connections_api.connectors_core.GOOGLE_CALENDAR_READ_SCOPE}


connections_api.exchange_override = fake_exchange

app = FastAPI()
app.include_router(connections_api.router)
client = TestClient(app)

# ── 1. status (never connected) ────────────────────────────────────────
print("\n1. GET /api/connections (never connected)")
r = client.get("/api/connections")
check("200", r.status_code == 200, r.status_code)
body = r.json()
view = body["connectors"][0]
check("read_only true", body.get("read_only") is True)
check("status disconnected", view.get("status") == "disconnected", view)
check("no token/sealed fields",
      not (set(view) & {"sealed", "access_token", "refresh_token",
                        "client_secret"}), sorted(view))
check("scopes empty", view.get("scopes") == [], view)


# ── 2. connect ─────────────────────────────────────────────────────────
print("\n2. POST /google/connect")
r = client.post("/api/connections/google/connect")
check("200", r.status_code == 200, r.status_code)
started = r.json()
check("returns authorization_url", bool(started.get("authorization_url")))
check("NO secret/token fields",
      not (set(started) & {"access_token", "refresh_token", "client_secret",
                           "sealed", "code"}), sorted(started))
check("calendar.readonly scope only",
      started.get("scopes") ==
      [connections_api.connectors_core.GOOGLE_CALENDAR_READ_SCOPE],
      started.get("scopes"))
qs = urllib.parse.parse_qs(
    urllib.parse.urlparse(started["authorization_url"]).query)
state = qs["state"][0]
check("state present in the url", bool(state))


# ── 3. callback completes the flow server-side ─────────────────────────
print("\n3. GET /google/callback (success)")
r = client.get("/api/connections/google/callback",
               params={"state": state, "code": "authcode"})
check("200 html page", r.status_code == 200 and "text/html" in
      r.headers.get("content-type", ""), r.status_code)
check("success page (no echo of code/state)",
      "connected" in r.text.lower() and "authcode" not in r.text
      and state not in r.text)

r = client.get("/api/connections")
view = r.json()["connectors"][0]
check("now connected", view.get("connected") is True, view)
check("view still has no token/sealed",
      not (set(view) & {"sealed", "access_token", "refresh_token"}))
check("usable true", view.get("usable") is True, view)


# ── 4. replay fails closed ─────────────────────────────────────────────
print("\n4. callback replay")
r = client.get("/api/connections/google/callback",
               params={"state": state, "code": "authcode"})
check("replay -> not-completed page",
      "not be completed" in r.text.lower() or "no longer valid" in r.text.lower(),
      r.text[:120])


# ── 5. owner isolation ─────────────────────────────────────────────────
print("\n5. owner isolation")
OWNER["id"] = "bob"
view_bob = client.get("/api/connections").json()["connectors"][0]
check("bob is NOT connected", view_bob.get("connected") is False, view_bob)
OWNER["id"] = "alice"


# ── 6. disconnect (idempotent) ─────────────────────────────────────────
print("\n6. POST /google/disconnect")
r = client.post("/api/connections/google/disconnect")
check("first disconnect true", r.json().get("disconnected") is True, r.json())
check("view disconnected", r.json()["connection"].get("connected") is False)
r = client.post("/api/connections/google/disconnect")
check("second disconnect false (idempotent)",
      r.json().get("disconnected") is False, r.json())


# ── 7. missing connector core fails closed ─────────────────────────────
print("\n7. connector core unavailable -> 503")
_saved = connections_api.connectors_core
connections_api.connectors_core = None
try:
    r = client.get("/api/connections")
    check("503 when core absent", r.status_code == 503, r.status_code)
finally:
    connections_api.connectors_core = _saved


print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
