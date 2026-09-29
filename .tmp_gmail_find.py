"""Read-only Gmail search: FROM Randy -> Subject / From / Date.

Only what the connector is designed to expose is printed (ids, thread ids,
Subject/From/Date, snippet). No token, no body, no attachment.
"""
import sys
import time
import urllib.parse

sys.path.insert(0, "/app/kyrex-cloud")

import connectors  # noqa: E402

OWNER = "kp84-hub"
QUERY = sys.argv[1] if len(sys.argv) > 1 else "from:Randy"
MAX = int(sys.argv[2]) if len(sys.argv) > 2 else 12

store = connectors.ConnectorStore()
status = store.status(OWNER, "google")
print("=== connector ===")
print(f"  {status.get('owner')} / {status.get('provider')} / "
      f"{status.get('status')} / gmail.readonly="
      f"{connectors.GOOGLE_GMAIL_READ_SCOPE in (status.get('scopes') or [])}")
print(f"  route: {store.route_capability(OWNER, 'gmail.read', 'google')}")

print("\n=== BUG: urlencode of the metadataHeaders list ===")
p = {"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]}
print(f"  as-shipped: {urllib.parse.urlencode(p)}")
print(f"  corrected : {urllib.parse.urlencode(p, doseq=True)}")

reader = store.gmail(OWNER)
stubs = reader.search(query=QUERY, max_results=MAX)
print(f"\n=== {QUERY!r} -> {len(stubs)} message(s) ===")

# Proof the shipped path loses headers (one call).
first = reader.message(stubs[0]["id"])
print(f"  as-shipped headers for {stubs[0]['id']}: "
      f"{first.get('headers')!r}  (snippet present: {bool(first.get('snippet'))})")

token = store.access_token(OWNER, "google")
api = connectors.PROVIDERS["google"]["gmail_api"]
query = urllib.parse.urlencode(
    {"format": "metadata",
     "metadataHeaders": ["Subject", "From", "Date"]}, doseq=True)

for stub in stubs:
    quoted = urllib.parse.quote(stub["id"], safe="")
    raw = connectors.default_transport(
        "GET", f"{api}/users/me/messages/{quoted}?{query}", token, None)
    headers = (connectors._gmail_message(raw, OWNER).get("headers") or {})
    print("-" * 70)
    print(f"  date   : {headers.get('Date', '(missing)')}")
    print(f"  from   : {headers.get('From', '(missing)')}")
    print(f"  subject: {headers.get('Subject', '(missing)')}")
    print(f"  id     : {stub['id']}")
