"""Read-only: latest Gmail messages from Randy, with snippets.

Prints only what the connector exposes to the owner: From / Subject / Date /
short snippet. No token, no body, no attachments.
"""
import sys

sys.path.insert(0, "/app/kyrex-cloud")

import connectors  # noqa: E402

OWNER = "kp84-hub"
store = connectors.ConnectorStore()
reader = store.gmail(OWNER)

stubs = reader.search(query="from:Randy", max_results=15)
print(f"total returned: {len(stubs)}\n")

for i, stub in enumerate(stubs, 1):
    detail = reader.message(stub["id"])
    hdrs = detail.get("headers") or {}
    snippet = (detail.get("snippet") or "").replace("\n", " ").strip()
    print(f"[{i}] {hdrs.get('Date', '(no date)')}")
    print(f"    from   : {hdrs.get('From', stub.get('from', '(unknown)'))}")
    print(f"    subject: {hdrs.get('Subject', '(no subject)')}")
    print(f"    snippet: {snippet[:160]}")
    print()
