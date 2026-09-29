"""Count how many messages match the Gmail query (estimate + page size)."""
import sys
import urllib.parse

sys.path.insert(0, "/app/kyrex-cloud")

import connectors  # noqa: E402

store = connectors.ConnectorStore()
owner = "kp84-hub"
token = store.access_token(owner, "google")
api = connectors.PROVIDERS["google"]["gmail_api"]

for q in ("from:Randy", "from:randy.see@principal.com", "Randy"):
    params = urllib.parse.urlencode({"maxResults": 500, "q": q})
    out = connectors.default_transport(
        "GET", f"{api}/users/me/messages?{params}", token, None)
    msgs = out.get("messages") or []
    print(f"query={q!r:34} returned={len(msgs):3d} "
          f"resultSizeEstimate={out.get('resultSizeEstimate')}")
