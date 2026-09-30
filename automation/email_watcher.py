"""Outbound-only email watcher. Credentials and execution stay on Railway.

The first complete snapshot establishes a baseline; only subsequent new IDs
are submitted. Local durable receipts plus server task IDs tolerate restarts
and a lost HTTP response without duplicating work.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward the service credential to another origin


class CloudClient:
    def __init__(self, base_url, token):
        url = urllib.parse.urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("An HTTPS Cloud origin is required")
        if len(token) < 32 or any(c.isspace() for c in token):
            raise ValueError("An automation credential is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base_url + "/api/automations/email" + path,
                                     data=data, headers={
                                         "Authorization": "Bearer " + self.token,
                                         "Content-Type": "application/json"})
        with self.opener.open(req, timeout=30) as response:
            if response.headers.get_content_type() != "application/json":
                raise ValueError("Expected a JSON response")
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("Cloud response exceeds the limit")
            return json.loads(raw)

    def rules(self):
        return self.request("/rules")["rules"]

    def snapshot(self, rule):
        ids, page, tokens = set(), "", set()
        for _ in range(20):  # at most 1,000 matches; never baseline a partial list
            query = urllib.parse.urlencode({"version": rule["version"], "page_token": page})
            out = self.request("/" + urllib.parse.quote(rule["id"], safe="") + "/candidates?" + query)
            batch = out["message_ids"]
            if not isinstance(batch, list) or any(not isinstance(mid, str) for mid in batch):
                raise ValueError("Invalid candidate list")
            ids.update(batch)
            page = out["next_page_token"]
            if not page:
                return ids
            if not isinstance(page, str) or page in tokens:
                raise ValueError("Invalid candidate pagination")
            tokens.add(page)
        raise ValueError("Candidate limit reached; narrow the sender rule")

    def submit(self, rule, message_id):
        receipt = self.request("/" + urllib.parse.quote(rule["id"], safe="") + "/events",
                               {"version": rule["version"], "message_id": message_id})
        if receipt.get("accepted") is not True or not receipt.get("task_id"):
            raise ValueError("Event was not accepted")
        return receipt["task_id"]


class Watcher:
    def __init__(self, client, state_path):
        self.client = client
        path = Path(state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS baselines (version TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS receipts (
                version TEXT NOT NULL, message_id TEXT NOT NULL, task_id TEXT,
                PRIMARY KEY (version, message_id));
        """)

    def poll_rule(self, rule):
        ids = self.client.snapshot(rule)
        version = rule["version"]
        if not self.db.execute("SELECT 1 FROM baselines WHERE version=?", (version,)).fetchone():
            with self.db:
                self.db.executemany("INSERT OR IGNORE INTO receipts VALUES (?, ?, NULL)",
                                    [(version, mid) for mid in ids])
                self.db.execute("INSERT INTO baselines VALUES (?)", (version,))
            return {"baseline": True, "queued": 0}
        queued = 0
        first_error = None
        for mid in sorted(ids):
            if self.db.execute("SELECT 1 FROM receipts WHERE version=? AND message_id=?",
                               (version, mid)).fetchone():
                continue
            try:
                task_id = self.client.submit(rule, mid)
            except Exception as exc:
                # One unavailable/deleted/mismatching message must not block
                # other new emails. Keep it unseen so a transient error retries.
                first_error = first_error or exc
                continue
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?, ?)",
                                (version, mid, task_id))
            queued += 1
        if first_error is not None:
            raise first_error
        return {"baseline": False, "queued": queued}

    def close(self):
        self.db.close()


def main():
    os.umask(0o077)
    shutdown = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: shutdown.set())
    client = CloudClient(os.environ["KYREX_AUTOMATION_CLOUD_URL"],
                         os.environ["KYREX_AUTOMATION_TOKEN"])
    interval = int(os.environ.get("KYREX_AUTOMATION_POLL_SECONDS", "300"))
    if not 60 <= interval <= 3600:
        raise ValueError("Polling interval must be 60–3600 seconds")
    watcher = Watcher(client, os.environ.get("KYREX_AUTOMATION_STATE", "/data/state.sqlite3"))
    try:
        while not shutdown.is_set():
            try:
                rules = client.rules()
                for rule in rules:
                    if shutdown.is_set():
                        break
                    try:
                        result = watcher.poll_rule(rule)
                        print(f"[email-watcher] baseline={result['baseline']} queued={result['queued']}", flush=True)
                    except Exception as exc:
                        # No email content, tokens, URLs, or provider errors in logs.
                        print(f"[email-watcher] rule unavailable: {type(exc).__name__}", flush=True)
            except Exception as exc:
                print(f"[email-watcher] Cloud unavailable: {type(exc).__name__}", flush=True)
            shutdown.wait(interval)
    finally:
        watcher.close()


if __name__ == "__main__":
    main()
