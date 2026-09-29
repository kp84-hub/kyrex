"""Fetch Subject/From/Date for every message in the 'from:Randy' set.

Writes one TSV line per message to /data/rifts/dev/.tmp_randy_out.txt as it
goes (flushed), so partial progress survives an interrupt.
Printed/derived data is limited to Subject/From/Date + ids. No token, no body.
"""
import sys
import urllib.parse

sys.path.insert(0, "/app/kyrex-cloud")

import connectors  # noqa: E402

OWNER = "kp84-hub"
OUT = "/data/rifts/dev/.tmp_randy_out.txt"

store = connectors.ConnectorStore()
reader = store.gmail(OWNER)
stubs = reader.search(query="from:Randy", max_results=50)
token = store.access_token(OWNER, "google")
api = connectors.PROVIDERS["google"]["gmail_api"]
query = urllib.parse.urlencode(
    {"format": "metadata",
     "metadataHeaders": ["Subject", "From", "Date"]}, doseq=True)

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(f"count={len(stubs)}\n")
    fh.flush()
    for i, stub in enumerate(stubs, 1):
        quoted = urllib.parse.quote(stub["id"], safe="")
        try:
            raw = connectors.default_transport(
                "GET", f"{api}/users/me/messages/{quoted}?{query}", token, None)
            h = connectors._gmail_message(raw, OWNER).get("headers") or {}
        except Exception as exc:  # keep going; record the failure
            h = {"Date": f"ERROR {type(exc).__name__}",
                 "From": "", "Subject": ""}
        fh.write(f"{i}\t{h.get('Date','')}\t{h.get('From','')}\t"
                 f"{h.get('Subject','')}\t{stub['id']}\n")
        fh.flush()
    fh.write("DONE\n")
