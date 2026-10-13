"""Owner-scoped forwarding inbox. No personal-Gmail fallback or mail writes."""

import base64
import hashlib
import json
import re
import shlex
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, getaddresses
from pathlib import Path
from urllib.parse import quote

import requests
import connectors as core
from paths import data_dir

API = "https://api.agentmail.to/v0/inboxes/"
MAX_RESPONSE = 1_048_576


def transport(path, key, params=None):
    # Caller-generated paths only; credentials cannot follow a redirect.
    try:
        with requests.get(
            API + path,
            headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
            params=params,
            timeout=(5, 20),
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise core.ConnectorUnavailable(
                    "AgentMail could not read this inbox. Check its address and read permissions."
                )
            raw = bytearray()
            for chunk in response.iter_content(16384):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE:
                    raise core.ConnectorUnavailable(
                        "AgentMail response was too large. Narrow the email search."
                    )
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
    except core.ConnectorError:
        raise
    except Exception:
        raise core.ConnectorUnavailable(
            "AgentMail is temporarily unavailable. Personal Gmail was not used."
        ) from None


class AgentInboxStore:
    def __init__(self, path=None, request=None):
        self.path = Path(path) if path else data_dir() / "agent_inbox.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.request = request or transport
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS inboxes (owner TEXT PRIMARY KEY, source TEXT NOT NULL, inbox TEXT, address TEXT, sealed TEXT, revision TEXT NOT NULL, checked REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, owner TEXT, revision TEXT, query TEXT, sealed TEXT, created REAL)"
            )
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, owner):
        owner = str(owner or "").strip()
        if not owner:
            raise core.ConnectorError("An inbox requires an owner.")
        with self.db() as db:
            row = db.execute(
                "SELECT source,inbox,address,sealed,revision,checked FROM inboxes WHERE owner=?",
                (owner,),
            ).fetchone()
        return (
            dict(
                zip(
                    ("source", "inbox", "address", "sealed", "revision", "checked"), row
                )
            )
            if row
            else {"source": "gmail", "revision": "legacy"}
        )

    def view(self, owner):
        row = self.record(owner)
        return {
            "provider": "agentmail",
            "source": row["source"],
            "revision": row["revision"],
            "inbox_id": row.get("inbox") or "",
            "address": row.get("address") or "",
            "connected": bool(row.get("sealed")),
            "active": row["source"] == "agentmail" and bool(row.get("sealed")),
            "personal_gmail_enabled": row["source"] == "gmail",
            "read_only": True,
            "checked_at": row.get("checked"),
        }

    def epoch(self, owner):
        row = self.record(owner)
        return row["source"], row["revision"]

    def connect(self, owner, inbox_id, key):
        old = self.record(owner)
        if (
            not isinstance(inbox_id, str)
            or inbox_id in (".", "..")
            or not re.fullmatch(r"[A-Za-z0-9@._+%-]{1,254}", inbox_id)
        ):
            raise core.ConnectorError("Enter the inbox ID from AgentMail Console.")
        if not key and old.get("inbox") == inbox_id and old.get("sealed"):
            key = core.unseal_tokens(old["sealed"]).get("api_key", "")
        if (
            not isinstance(key, str)
            or not 20 <= len(key) <= 512
            or any(not 33 <= ord(ch) <= 126 for ch in key)
        ):
            raise core.ConnectorError(
                "Enter an AgentMail API key with inbox and message read access."
            )
        value = self.request(quote(inbox_id, safe=""), key)
        inbox, actual = value.get("inbox_id"), value.get("email")
        if (
            inbox != inbox_id
            or not isinstance(actual, str)
            or len(actual) > 254
            or not re.fullmatch(
                r"[A-Za-z0-9.!#$%&*+_=?^`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", actual
            )
        ):
            raise core.ConnectorUnavailable(
                "AgentMail did not confirm the requested inbox."
            )
        if value.get("status") == "paused":
            raise core.ConnectorUnavailable(
                "This AgentMail inbox is paused. Resume it before connecting."
            )
        # Verify message_read as well as inbox_read, without surfacing a message.
        probe = self.request(quote(inbox, safe="") + "/messages", key, {"limit": 1})
        if not isinstance(probe.get("messages"), list):
            raise core.ConnectorUnavailable(
                "AgentMail message read access could not be verified."
            )
        sealed = core.seal_tokens({"api_key": key})
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT revision FROM inboxes WHERE owner=?", (owner,)
            ).fetchone()
            if (current[0] if current else "legacy") != old["revision"]:
                raise core.ConnectorUnavailable(
                    "Email settings changed while connecting. Try again."
                )
            db.execute("DELETE FROM pages WHERE owner=?", (owner,))
            db.execute(
                "INSERT OR REPLACE INTO inboxes VALUES (?,?,?,?,?,?,?)",
                (
                    owner,
                    "agentmail",
                    inbox,
                    actual,
                    sealed,
                    uuid.uuid4().hex,
                    time.time(),
                ),
            )
        return self.view(owner)

    def select(self, owner, source):
        self.record(owner)
        if source not in ("none", "gmail"):
            raise core.ConnectorError("Choose personal Gmail or disable email reading.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO inboxes (owner,source,revision) VALUES (?,'none',?)",
                (owner, uuid.uuid4().hex),
            )
            db.execute(
                "UPDATE inboxes SET source=?,revision=? WHERE owner=?",
                (source, uuid.uuid4().hex, owner),
            )
            db.execute("DELETE FROM pages WHERE owner=?", (owner,))
        return self.view(owner)

    def disconnect(self, owner):
        self.record(owner)
        with self.db() as db:
            db.execute(
                "UPDATE inboxes SET source='none',sealed=NULL,revision=? WHERE owner=?",
                (uuid.uuid4().hex, owner),
            )
            db.execute("DELETE FROM pages WHERE owner=?", (owner,))
        # A never-connected owner explicitly disconnecting also blocks Gmail.
        if self.record(owner)["source"] == "gmail":
            self.select(owner, "none")
        return self.view(owner)

    def reader(self, owner):
        return AgentMailRead(self, owner)


def default_store():
    return AgentInboxStore()


def _encoded(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decoded(value):
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    ).decode()


class AgentMailRead:
    """Compatibility with the existing bounded Email Bot reader interface."""

    def __init__(self, store, owner):
        self.store, self.owner = store, owner
        self.revision = store.record(owner)["revision"]
        self.namespace = hashlib.sha256(
            (owner + ":" + self.revision).encode()
        ).hexdigest()[:16]
        self.summaries = {}

    def _auth(self):
        row = self.store.record(self.owner)
        if (
            row["source"] != "agentmail"
            or not row.get("sealed")
            or row["revision"] != self.revision
        ):
            raise core.ConnectorUnavailable(
                "The email source changed or the dedicated inbox is disconnected. Search again after connecting."
            )
        key = core.unseal_tokens(row["sealed"]).get("api_key", "")
        if not key:
            raise core.ConnectorUnavailable(
                "The dedicated inbox key could not be opened. Reconnect in Connections."
            )
        return row, key

    def _get(self, suffix="", params=None):
        row, key = self._auth()
        result = self.store.request(quote(row["inbox"], safe="") + suffix, key, params)
        self._auth()  # Never release an in-flight result after a source switch.
        return result

    def _id(self, raw):
        if not isinstance(raw, str) or not raw or len(raw) > 256:
            raise core.ConnectorUnavailable(
                "AgentMail returned an invalid message identity."
            )
        return "am1_" + self.namespace + "_" + _encoded(raw)

    def _raw_id(self, value):
        prefix = "am1_" + self.namespace + "_"
        if (
            not isinstance(value, str)
            or not value.startswith(prefix)
            or len(value) > 512
        ):
            raise core.ConnectorUnavailable(
                "This email reference belongs to a different inbox. Search again."
            )
        try:
            raw = _decoded(value[len(prefix) :])
        except Exception:
            raise core.ConnectorError("Invalid inbox message reference.") from None
        if not raw or raw in (".", "..") or len(raw) > 256:
            raise core.ConnectorError("Invalid inbox message reference.")
        return raw

    def _safe(self, value):
        _, key = self._auth()
        return (
            core.redact_text(value).replace(key, "[redacted]")
            if key
            else core.redact_text(value)
        )

    def _project(self, value, body=False):
        row, _ = self._auth()
        if value.get("inbox_id") != row["inbox"]:
            raise core.ConnectorUnavailable(
                "AgentMail returned a message from a different inbox."
            )
        try:
            date = format_datetime(
                datetime.fromisoformat(
                    str(value.get("timestamp")).replace("Z", "+00:00")
                )
            )
        except (TypeError, ValueError):
            date = ""
        result = {
            "owner": self.owner,
            "id": self._id(value.get("message_id")),
            "thread_id": self._id(value.get("thread_id") or value.get("message_id")),
            "snippet": self._safe(value.get("preview"))[:500],
            "headers": {
                "Subject": self._safe(value.get("subject"))[:500],
                "From": self._safe(value.get("from"))[:500],
                "Date": date,
            },
        }
        if body:
            # Full text preserves forwarded/quoted school content. Extracted
            # new-text-only fields can remove precisely what the user forwarded.
            text, kind = value.get("text"), "text"
            if not isinstance(text, str) or not text.strip():
                text = core._gmail_html_to_text(value.get("html"))
                kind = "html"
            if not text:
                text = value.get("extracted_text") or ""
                kind = "text"
            result.update(
                body=self._safe(text)[: core._GMAIL_BODY_MAX],
                body_type=kind,
                truncated=len(text) > core._GMAIL_BODY_MAX,
            )
        return result

    def _query(self, query):
        query = str(query or "").strip()
        if len(query) > 1024:
            raise core.ConnectorError("Narrow the email search to 1,024 characters.")
        try:
            terms = shlex.split(query)
        except ValueError:
            raise core.ConnectorError(
                "Use a plain-text email search or quoted subject."
            ) from None
        words, params, sender, subject, received = [], {}, "", "", False
        for term in terms:
            name, sep, value = term.partition(":")
            if sep and name == "in" and value == "inbox":
                received = True
            elif sep and name in ("after", "before"):
                try:
                    params[name] = (
                        datetime.strptime(value.replace("/", "-"), "%Y-%m-%d")
                        .replace(tzinfo=timezone.utc)
                        .isoformat()
                    )
                except ValueError:
                    raise core.ConnectorError(
                        "Use YYYY-MM-DD for email date filters."
                    ) from None
            elif sep and name == "newer_than" and re.fullmatch(r"\d{1,3}[dmy]", value):
                days = int(value[:-1]) * {"d": 1, "m": 30, "y": 365}[value[-1]]
                params["after"] = (
                    datetime.now(timezone.utc) - timedelta(days=days)
                ).isoformat()
            elif sep and name in ("from", "subject"):
                if name == "from":
                    sender = value.strip("()")
                else:
                    subject = value
            elif sep or term.upper() in ("OR", "AND") or any(ch in term for ch in "{}"):
                raise core.ConnectorError(
                    "Dedicated inbox searches support plain text, from:, subject:, after:, before:, and newer_than:."
                )
            else:
                words.append(term)
        return " ".join(words), params, sender, subject, received

    def search(self, *, query=None, max_results=10, page_token=None):
        self._auth()
        q, params, sender, subject, received = self._query(query)
        params["limit"] = max(1, min(int(max_results or 10), 50))
        if page_token:
            if not isinstance(page_token, str) or not re.fullmatch(
                r"amp1_[a-f0-9]{32}", page_token
            ):
                raise core.ConnectorError("Invalid email page. Search again.")
            with self.store.db() as db:
                saved = db.execute(
                    "SELECT sealed FROM pages WHERE id=? AND owner=? AND revision=? AND query=? AND created>?",
                    (
                        page_token,
                        self.owner,
                        self.revision,
                        hashlib.sha256(str(query or "").encode()).hexdigest(),
                        time.time() - 3600,
                    ),
                ).fetchone()
            if not saved:
                raise core.ConnectorError(
                    "This email page expired or belongs to a different inbox or search. Search again."
                )
            params = core.unseal_tokens(saved[0])
            if (
                not isinstance(params.get("page_token"), str)
                or not params["page_token"]
            ):
                raise core.ConnectorError(
                    "This email page cannot be opened. Search again."
                )
            params["limit"] = max(1, min(int(max_results or 10), 50))
        if q:
            params["q"] = q
        else:
            if sender:
                params["from"] = [sender]
            if subject:
                params["subject"] = [subject]
            if received:
                params["labels"] = ["received"]
        out = self._get("/messages/search" if q else "/messages", params)
        items = out.get("messages")
        if not isinstance(items, list) or len(items) > params["limit"]:
            raise core.ConnectorUnavailable("AgentMail returned an invalid email page.")
        results = []
        for item in items:
            if not isinstance(item, dict):
                raise core.ConnectorUnavailable(
                    "AgentMail returned an invalid message."
                )
            if received and "received" not in (item.get("labels") or []):
                continue
            message = self._project(item)
            # from: remains an exact outer-sender constraint for notification
            # rules. Quoted forward headers are not authenticated sender facts.
            actual = getaddresses([message["headers"]["From"]])
            if (
                sender
                and "@" in sender
                and (len(actual) != 1 or actual[0][1].lower() != sender.lower())
            ):
                continue
            if (
                sender
                and "@" not in sender
                and sender.lower() not in message["headers"]["From"].lower()
            ):
                continue
            if subject and subject.lower() not in message["headers"]["Subject"].lower():
                continue
            self.summaries[message["id"]] = message
            results.append(
                {
                    "owner": self.owner,
                    "id": message["id"],
                    "thread_id": message["thread_id"],
                }
            )
        token = out.get("next_page_token") or ""
        if not isinstance(token, str) or len(token) > 1024:
            raise core.ConnectorUnavailable("AgentMail returned an invalid page token.")
        next_page = ""
        if token:
            next_page = "amp1_" + uuid.uuid4().hex
            saved = dict(params, page_token=token)
            with self.store.db() as db:
                db.execute("DELETE FROM pages WHERE created<?", (time.time() - 3600,))
                db.execute(
                    "INSERT INTO pages VALUES (?,?,?,?,?,?)",
                    (
                        next_page,
                        self.owner,
                        self.revision,
                        hashlib.sha256(str(query or "").encode()).hexdigest(),
                        core.seal_tokens(saved),
                        time.time(),
                    ),
                )
                db.execute(
                    "DELETE FROM pages WHERE owner=? AND id NOT IN (SELECT id FROM pages WHERE owner=? ORDER BY created DESC LIMIT 64)",
                    (self.owner, self.owner),
                )
        return {"owner": self.owner, "messages": results, "next_page_token": next_page}

    def latest(self, *, query=None, max_results=10):
        # AgentMail search ranks relevance, not date. Only claim "latest" once
        # a bounded complete search has been examined, otherwise fail closed.
        q = self._query(query)[0]
        hits, token = [], None
        for _ in range(10):
            page = self.search(query=query, max_results=50, page_token=token)
            hits.extend(page["messages"])
            token = page["next_page_token"]
            if not token or (not q and hits):
                break
        if token and (q or not hits):
            raise core.ConnectorUnavailable(
                "Too many matches to identify the latest email. Narrow the sender or date."
            )
        if any(not self.summaries[hit["id"]]["headers"]["Date"] for hit in hits):
            raise core.ConnectorUnavailable(
                "A matching email has no valid date. Cannot identify the latest message."
            )
        # RFC dates are display strings, so compare parsed timestamps instead.
        from email.utils import parsedate_to_datetime

        hits.sort(
            key=lambda hit: (
                parsedate_to_datetime(
                    self.summaries[hit["id"]]["headers"]["Date"]
                ).timestamp()
                if self.summaries[hit["id"]]["headers"]["Date"]
                else 0
            ),
            reverse=True,
        )
        return {"owner": self.owner, "messages": hits[:1], "next_page_token": ""}

    def message(self, message_id):
        self._auth()
        raw = self._raw_id(message_id)
        if message_id in self.summaries:
            return self.summaries[message_id]
        value = self._get("/messages/" + quote(raw, safe=""))
        if value.get("message_id") != raw:
            raise core.ConnectorUnavailable("AgentMail returned a different message.")
        return self._project(value)

    def read_message(self, message_id):
        self._auth()
        raw = self._raw_id(message_id)
        value = self._get("/messages/" + quote(raw, safe=""))
        if value.get("message_id") != raw:
            raise core.ConnectorUnavailable("AgentMail returned a different message.")
        return self._project(value, body=True)
