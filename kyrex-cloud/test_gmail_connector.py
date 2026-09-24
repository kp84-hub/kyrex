#!/usr/bin/env python3
"""Focused tests for the read-only Gmail connector slice.

Covers, without any network or real credentials:

  * a Calendar-only owner CANNOT read Gmail (scope-missing fails closed);
  * the Gmail read upgrade UNIONs gmail.readonly with the existing granted
    scopes, so Calendar access is preserved (and no arbitrary scope is added);
  * ONLY the gmail.readonly scope is added to the allowed set -- the default
    Calendar connect never requests Gmail;
  * there is NO Gmail mutation surface (no send/delete/archive/label op or
    method), and every such capability is declared unsupported;
  * tokens / codes never reach a returned view (search + message projections);
  * disconnected / expired fail closed;
  * the existing Calendar behaviour is unchanged.

Run: python3 test_gmail_connector.py
"""
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Connector environment BEFORE importing the module that reads it.
os.environ["WEB_SESSION_SECRET"] = "unit-test-gmail-secret"
os.environ["KYREX_DATA_DIR"] = tempfile.mkdtemp(prefix="kx-gmail-test-")
os.environ.setdefault("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "client-secret-value")
os.environ.setdefault("GOOGLE_REDIRECT_URI",
                      "https://kyrex.example/api/connections/google/callback")

import connectors as conn  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          ("" if cond else f"  [{detail}]"))
    if not cond:
        failures.append(name)


CAL = conn.GOOGLE_CALENDAR_READ_SCOPE
GMAIL = conn.GOOGLE_GMAIL_READ_SCOPE
WRITE = conn.GOOGLE_CALENDAR_WRITE_SCOPE


def exchange_for(scopes):
    def _ex(code, redirect, client):
        return {"access_token": "ya29.SECRET-ACCESS",
                "refresh_token": "1//SECRET-REFRESH", "expires_in": 3600,
                "scope": " ".join(scopes)}
    return _ex


# ── 1. scope allow-list: only gmail.readonly added, default unchanged ───
print("\n1. scope allow-list")
check("gmail.readonly is in the allowed set", GMAIL in conn.GOOGLE_ALLOWED_SCOPES)
check("calendar.readonly still allowed", CAL in conn.GOOGLE_ALLOWED_SCOPES)
check("calendar.events still allowed", WRITE in conn.GOOGLE_ALLOWED_SCOPES)
check("allowed set is exactly the three scopes",
      conn.GOOGLE_ALLOWED_SCOPES == frozenset({CAL, WRITE, GMAIL}),
      sorted(conn.GOOGLE_ALLOWED_SCOPES))
check("default Calendar connect does NOT request gmail",
      conn.GOOGLE_READ_SCOPES == (CAL,), conn.GOOGLE_READ_SCOPES)
check("gmail.read routes to the gmail reader bot",
      conn.CAPABILITY_ROUTING.get("gmail.read") == "gmail_bot")


# ── 2. an arbitrary scope is still refused ─────────────────────────────
print("\n2. arbitrary scope refused")
store = conn.ConnectorStore()
try:
    store.begin_oauth("mallory", scopes=["https://www.googleapis.com/auth/drive"])
    check("unknown scope refused", False)
except conn.ConnectorError:
    check("unknown scope refused", True)


# ── 3. Calendar-only owner CANNOT read Gmail ───────────────────────────
print("\n3. Calendar-only owner cannot read Gmail")
start = store.begin_oauth("alice")
check("plain connect requests calendar.readonly only",
      start["scopes"] == [CAL], start["scopes"])
view = store.complete_oauth("alice", start["state"], "code",
                           exchange=exchange_for([CAL]))
check("alice connected", view["connected"] is True, view)
try:
    store.gmail("alice").search()
    check("gmail search blocked for calendar-only owner", False)
except conn.ConnectorUnavailable:
    check("gmail search blocked for calendar-only owner", True)
try:
    store.gmail("alice").message("m1")
    check("gmail message blocked for calendar-only owner", False)
except conn.ConnectorUnavailable:
    check("gmail message blocked for calendar-only owner", True)
# The calendar still works for the same owner (unchanged behaviour).
check("calendar read still authorized for alice", CAL in view["scopes"])


# ── 4. Gmail upgrade PRESERVES existing Calendar scopes ────────────────
print("\n4. Gmail read upgrade unions existing scopes")
up = store.begin_gmail_read_upgrade("alice")
check("upgrade asks for calendar.readonly + gmail.readonly",
      set(up["scopes"]) == {CAL, GMAIL}, sorted(up["scopes"]))
store.complete_oauth("alice", up["state"], "code",
                     exchange=exchange_for(up["scopes"]))
scopes = set(store.status("alice")["scopes"])
check("calendar.readonly preserved after gmail upgrade", CAL in scopes, scopes)
check("gmail.readonly now granted", GMAIL in scopes, scopes)
# An owner who already had the calendar WRITE scope keeps it too.
store2 = conn.ConnectorStore()
w = store2.begin_oauth("carol", scopes=[CAL, WRITE])
store2.complete_oauth("carol", w["state"], "code",
                      exchange=exchange_for([CAL, WRITE]))
g = store2.begin_gmail_read_upgrade("carol")
check("gmail upgrade unions write + read + gmail",
      set(g["scopes"]) == {CAL, WRITE, GMAIL}, sorted(g["scopes"]))
check("gmail upgrade never adds a mutation scope",
      not (set(g["scopes"]) & {
          "https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/gmail.modify"}),
      sorted(g["scopes"]))


# ── 5. no Gmail mutation surface exists ────────────────────────────────
print("\n5. no Gmail mutation surface")
gmail_iface = store.gmail("alice")
for method in ("send", "delete", "archive", "label", "modify", "trash"):
    check(f"GmailRead has no {method}() method",
          not hasattr(gmail_iface, method))
decl = conn.CAPABILITY_DECLARATIONS["gmail_bot"]
check("gmail_bot declares exactly gmail.read",
      tuple(decl["capabilities"]) == ("gmail.read",), decl["capabilities"])
check("gmail_bot is read-only", decl["read_only"] is True)
for cap in ("gmail.send", "gmail.delete", "gmail.archive", "gmail.modify",
            "gmail.label"):
    check(f"{cap} declared unsupported", cap in decl["unsupported"])
    check(f"{cap} is not routable", cap not in conn.CAPABILITY_ROUTING)
for cap in ("gmail.write", "gmail.compose", "gmail.send"):
    try:
        store.route_capability("alice", cap)
        check(f"route_capability({cap!r}) refused", False)
    except conn.ConnectorUnavailable:
        check(f"route_capability({cap!r}) refused", True)


# ── 6. read projections never carry a token / code ─────────────────────
print("\n6. read projections are secret-safe")
seen = {}


def transport(method, url, token, params=None, body=None):
    seen.update(method=method, url=url, params=params, token=token)
    if url.endswith("/users/me/messages"):
        return {"messages": [{"id": "m1", "threadId": "t1",
                              "snippet": "SECRET should not surface here"}]}
    return {
        "id": "m1", "threadId": "t1", "snippet": "hi",
        "payload": {"headers": [
            {"name": "Subject", "value": "Hello"},
            {"name": "From", "value": "someone@example.com"},
            {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            {"name": "Authorization", "value": "Bearer ya29.LEAK"},
        ]},
        "access_token": "ya29.LEAK-TOKEN", "raw": "SECRET-BODY",
    }


results = store.gmail("alice", transport=transport).search(query="from:me")
check("search returns stubs only (no snippet/headers)",
      results["messages"] == [{"owner": "alice", "id": "m1",
                               "thread_id": "t1"}], results)
check("search reports no next page when the provider omits one",
      results["next_page_token"] == "", results)
check("search never surfaces a snippet from the list payload",
      "SECRET should not surface" not in str(results), results)
check("search URL is the gmail messages endpoint",
      seen["url"].endswith("/users/me/messages"), seen["url"])
check("search passes the query", seen["params"].get("q") == "from:me", seen)
msg = store.gmail("alice", transport=transport).message("m1")
blob = str(msg)
check("message id/headers surfaced",
      msg["id"] == "m1" and msg["headers"].get("Subject") == "Hello", msg)
check("no access token in projection", "ya29" not in blob, blob)
check("no raw body in projection", "SECRET-BODY" not in blob, blob)
check("Authorization header never surfaced",
      "Authorization" not in msg["headers"], msg["headers"])
check("message fetch uses metadata format",
      seen["params"].get("format") == "metadata", seen["params"])
check("message fetch quotes a bad id closed",
      all(m not in str(msg) for m in ("raw", "access_token")), msg)

# public_view never carries a token either
pub = store.status("alice")
check("public status has no token fields",
      not (set(pub) & {"access_token", "refresh_token", "sealed"}), sorted(pub))
check("on-disk blob has no plaintext token",
      "SECRET-ACCESS" not in open(store.path).read()
      and "SECRET-REFRESH" not in open(store.path).read())


# ── 7. disconnected / expired fail closed ──────────────────────────────
print("\n7. disconnected / expired fail closed")
store.disconnect("alice")
try:
    store.gmail("alice").search()
    check("disconnected gmail read fails closed", False)
except conn.ConnectorUnavailable:
    check("disconnected gmail read fails closed", True)

store3 = conn.ConnectorStore()
e = store3.begin_oauth("erin", scopes=[CAL, GMAIL])
store3.complete_oauth("erin", e["state"], "code",
                      exchange=exchange_for([CAL, GMAIL]))
# Force expiry and remove the refresh token so refresh cannot succeed.
data = store3._read()
data["owners"][store3._owner_key("erin")]["providers"]["google"]["expires_at"] = 1.0
rec = data["owners"][store3._owner_key("erin")]["providers"]["google"]
rec["sealed"] = conn.seal_tokens({
    "access_token": "ya29.x", "refresh_token": "",
    "scope": f"{CAL} {GMAIL}", "token_type": "Bearer"})
store3._write(data)
try:
    store3.gmail("erin", transport=transport).search()
    check("expired-without-refresh gmail read fails closed", False)
except conn.ConnectorUnavailable:
    check("expired-without-refresh gmail read fails closed", True)


# ── 8. existing Calendar behaviour is unchanged ────────────────────────
print("\n8. existing Calendar behaviour unchanged")
storeC = conn.ConnectorStore()
cstart = storeC.begin_oauth("frank")
check("calendar connect mints exactly its default scopes",
      cstart["scopes"] == [CAL], cstart["scopes"])
storeC.complete_oauth("frank", cstart["state"], "code",
                      exchange=exchange_for([CAL]))
cseen = {}


def cal_transport(method, url, token, params=None, body=None):
    cseen.update(url=url, params=params)
    return {"items": []}


storeC.calendar("frank", transport=cal_transport).events(
    time_min="2025-03-09T00:00:00-05:00", time_max="2025-03-10T00:00:00-04:00")
check("calendar read still targets primary",
      cseen["url"].endswith("/calendars/primary/events"), cseen["url"])
check("calendar read still bounded + ordered",
      cseen["params"].get("singleEvents") == "true"
      and cseen["params"].get("orderBy") == "startTime", cseen["params"])
check("calendar.write upgrade still unions the write scope",
      WRITE in storeC.begin_calendar_write_upgrade("frank")["scopes"])


# ── 9. transport encodes REPEATED metadataHeaders (encoded-query regression) ─
# Production bug: `gmail: search from:Randy` found real message ids, yet every
# enriched result rendered "(no subject) - from (unknown sender) - (no date)".
# Trace: GmailRead.message sends metadataHeaders=["Subject","From","Date"], but
# default_transport built the query with urlencode(params) -- no doseq -- so
# Gmail's REPEATED metadataHeaders param went out as ONE url-encoded list
# literal and was silently ignored, yielding a header-less payload.
#
# These checks drive the REAL default_transport (no injected transport) and
# inspect the ACTUAL encoded request query captured off the Request object,
# then render a realistic Gmail metadata response end-to-end.
print("\n9. transport encodes repeated metadataHeaders "
      "(encoded-query regression)")

storeX = conn.ConnectorStore()
xstart = storeX.begin_oauth("gwen", scopes=[CAL, GMAIL])
storeX.complete_oauth("gwen", xstart["state"], "code",
                      exchange=exchange_for([CAL, GMAIL]))

captured = []


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# A realistic Gmail `format=metadata` message: real header ordering and a
# non-Subject/From/Date header mixed in, to prove only the safe three surface.
_METADATA_MESSAGE = {
    "id": "18c0f1a2b3c4d5e6", "threadId": "18c0f1a2b3c4d5e6",
    "labelIds": ["INBOX", "UNREAD"], "snippet": "Roadmap for Q1",
    "historyId": "987654", "sizeEstimate": 4821,
    "payload": {"mimeType": "multipart/alternative", "headers": [
        {"name": "Delivered-To", "value": "owner@example.com"},
        {"name": "Date", "value": "Mon, 3 Feb 2025 09:12:44 -0800"},
        {"name": "From", "value": "Randy Marsh <randy@example.com>"},
        {"name": "Subject", "value": "Q1 roadmap draft"},
        {"name": "To", "value": "owner@example.com"},
        {"name": "Authentication-Results", "value": "spf=pass"},
    ]},
}

_real_urlopen = conn.urllib.request.urlopen


def _fake_urlopen(req, *a, **kw):
    url = getattr(req, "full_url", str(req))
    captured.append(url)
    base, _, qs = url.partition("?")
    if base.endswith("/users/me/messages"):
        stub = {"id": _METADATA_MESSAGE["id"],
                "threadId": _METADATA_MESSAGE["threadId"]}
        return _FakeResp(json.dumps({"messages": [stub]}).encode())
    if "/calendars/" in base:
        return _FakeResp(json.dumps({"items": []}).encode())
    # Mimic Gmail's documented `format=metadata`: it returns ONLY the headers
    # named by the REPEATED `metadataHeaders` params. The old single
    # url-encoded list literal names none, so Gmail returns a header-less
    # payload -- the production "(no subject) - from (unknown sender) -
    # (no date)" regression. This makes the render checks fail closed too.
    pairs = urllib.parse.parse_qsl(qs, keep_blank_values=True)
    requested = [v for k, v in pairs if k == "metadataHeaders"]
    given = dict(pairs)
    headers = ([h for h in _METADATA_MESSAGE["payload"]["headers"]
                if h["name"] in requested]
               if given.get("format") == "metadata" else [])
    payload = dict(_METADATA_MESSAGE)
    payload["payload"] = dict(_METADATA_MESSAGE["payload"], headers=headers)
    return _FakeResp(json.dumps(payload).encode())


def _encoded(url):
    """The parsed (key, value) pairs of a request's REAL query string."""
    return urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query,
                                  keep_blank_values=True)


conn.urllib.request.urlopen = _fake_urlopen
try:
    stubs = storeX.gmail("gwen").search(query="from:Randy", max_results=10)
    enriched = storeX.gmail("gwen").message(stubs["messages"][0]["id"])
finally:
    conn.urllib.request.urlopen = _real_urlopen

search_pairs = _encoded(captured[0])
msg_pairs = _encoded(captured[1])
msg_multi = {}
for _k, _v in msg_pairs:
    msg_multi.setdefault(_k, []).append(_v)

# The bug: metadataHeaders must repeat, one pair per header, in order.
check("encoded request repeats metadataHeaders once per header",
      msg_multi.get("metadataHeaders") == ["Subject", "From", "Date"],
      msg_multi.get("metadataHeaders"))
# ...and it must NOT be the old url-encoded list literal ("['Subject', ...").
check("encoded request carries no url-encoded list literal",
      "%5B%27" not in captured[1] and "metadataHeaders=%5B" not in captured[1],
      captured[1])
check("encoded request still carries format=metadata exactly once",
      msg_multi.get("format") == ["metadata"], msg_multi.get("format"))

# Scalar params are byte-for-byte unchanged by doseq=True.
_scalars = {"maxResults": 10, "q": "from:Randy", "singleEvents": "true",
            "orderBy": "startTime", "format": "metadata"}
check("doseq=True leaves scalar params byte-identical",
      urllib.parse.urlencode(_scalars) ==
      urllib.parse.urlencode(_scalars, doseq=True),
      urllib.parse.urlencode(_scalars, doseq=True))
check("encoded search query keeps scalar q + maxResults verbatim",
      dict(search_pairs).get("q") == "from:Randy"
      and dict(search_pairs).get("maxResults") == "10", search_pairs)

# And the realistic metadata response now renders subject / sender / date.
check("metadata response renders the subject",
      enriched["headers"].get("Subject") == "Q1 roadmap draft",
      enriched["headers"])
check("metadata response renders the sender",
      "randy@example.com" in enriched["headers"].get("From", ""),
      enriched["headers"])
check("metadata response renders the date",
      "3 Feb 2025" in enriched["headers"].get("Date", ""),
      enriched["headers"])
check("all three safe headers are present and non-empty "
      "(no '(no subject)'/'unknown sender'/'no date' fallbacks)",
      all(enriched["headers"].get(h) for h in ("Subject", "From", "Date")),
      enriched["headers"])
check("only the safe headers surface (no Authentication-Results)",
      "Authentication-Results" not in enriched["headers"], enriched["headers"])
check("metadata payload leaks no body / label / history fields",
      all(f not in str(enriched)
          for f in ("labelIds", "historyId", "sizeEstimate")), enriched)

# The Calendar query, through the same real transport, is unchanged too.
captured.clear()
conn.urllib.request.urlopen = _fake_urlopen
try:
    storeX.calendar("gwen").events(time_min="2025-03-09T00:00:00-05:00",
                                   time_max="2025-03-10T00:00:00-04:00")
finally:
    conn.urllib.request.urlopen = _real_urlopen
cal_q = dict(_encoded(captured[0]))
check("calendar encoded query unchanged by the transport fix",
      cal_q.get("singleEvents") == "true"
      and cal_q.get("orderBy") == "startTime"
      and cal_q.get("maxResults") == "25", cal_q)


# ── 10. bounded pagination: pageToken out, nextPageToken back ──────────
# The reader relays Gmail's opaque nextPageToken so a bounded search can be
# continued page-by-page with the SAME query, without ever widening the read
# (still gmail.readonly, metadata stubs only) or fetching a body.
print("\n10. bounded pagination via nextPageToken")
page_seen = []


def page_transport(method, url, token, params=None, body=None):
    page_seen.append(dict(params or {}))
    if (params or {}).get("pageToken") == "TOK2":
        return {"messages": [{"id": "m3", "threadId": "t3"}]}
    return {"messages": [{"id": "m1", "threadId": "t1"},
                         {"id": "m2", "threadId": "t2"}],
            "nextPageToken": "TOK2"}


first = storeX.gmail("gwen", transport=page_transport).search(
    query="from:Randy", max_results=5)
check("first page carries the provider nextPageToken",
      first["next_page_token"] == "TOK2", first)
check("first page returns the bounded stubs",
      [m["id"] for m in first["messages"]] == ["m1", "m2"], first)
check("first page asks for exactly the bounded size, no pageToken",
      page_seen[0].get("maxResults") == 5 and "pageToken" not in page_seen[0],
      page_seen[0])

second = storeX.gmail("gwen", transport=page_transport).search(
    query="from:Randy", max_results=5, page_token="TOK2")
check("continuation re-sends the SAME query + pageToken",
      page_seen[1].get("q") == "from:Randy"
      and page_seen[1].get("pageToken") == "TOK2", page_seen[1])
check("continuation asks for exactly the bounded size",
      page_seen[1].get("maxResults") == 5, page_seen[1])
check("last page reports no further token",
      second["next_page_token"] == "", second)

# A malformed / oversized page token fails closed BEFORE any provider call.
for bad in ("a b", "x" * 5000):
    try:
        storeX.gmail("gwen", transport=page_transport).search(page_token=bad)
        check(f"page token {bad[:8]!r} rejected", False)
    except conn.ConnectorError:
        check(f"page token {bad[:8]!r} rejected", True)


print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)