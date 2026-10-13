"""Read one dedicated Zoho Inbox over TLS. No SMTP or IMAP mutations."""

import base64
import hashlib
import imaplib
import quopri
import re
import ssl
from contextlib import contextmanager
from datetime import datetime
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses

import connectors as core

HOSTS = ("imappro.zoho.com", "imap.zoho.com")
WINDOW = 5000
MAX_RESPONSE = 1_048_576
MAX_HEADERS = 16384
MAX_TEXT = 131072
MAX_STRUCTURE = 131072


class BoundedIMAP(imaplib.IMAP4_SSL):
    def read(self, size):
        # Reject oversized provider literals before allocating their content.
        if size > MAX_RESPONSE:
            raise core.ConnectorUnavailable(
                "Zoho response exceeded the email read limit."
            )
        return super().read(size)


def validate(address, host):
    if (
        not isinstance(address, str)
        or len(address) > 254
        or not re.fullmatch(r"[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", address)
    ):
        raise core.ConnectorError("Enter the dedicated Zoho mailbox email address.")
    if host not in HOSTS:
        raise core.ConnectorError("Choose a supported Zoho US IMAP server.")


def validate_password(password):
    if (
        not isinstance(password, str)
        or not 8 <= len(password) <= 512
        or any(not 33 <= ord(c) <= 126 for c in password)
    ):
        raise core.ConnectorError(
            "Enter the dedicated mailbox account’s Zoho app password."
        )


def _number(client, name):
    _, values = client.response(name)
    if (
        not isinstance(values, list)
        or len(values) != 1
        or not isinstance(values[0], bytes)
        or not re.fullmatch(rb"[0-9]{1,10}", values[0])
    ):
        raise core.ConnectorUnavailable("Zoho did not confirm a stable Inbox identity.")
    number = int(values[0])
    if not 1 <= number <= 4294967295:
        raise core.ConnectorUnavailable("Zoho returned an invalid Inbox identity.")
    return number


@contextmanager
def session(address, password, host, factory=None):
    validate(address, host)
    validate_password(password)
    client = None
    try:
        client = (factory or BoundedIMAP)(
            host, port=993, ssl_context=ssl.create_default_context(), timeout=20
        )
        if client.login(address, password)[0] != "OK":
            raise core.ConnectorUnavailable("Zoho mailbox sign-in was rejected.")
        if client.select("INBOX", readonly=True)[0] != "OK":
            raise core.ConnectorUnavailable("Zoho Inbox read access is unavailable.")
        yield client, _number(client, "UIDVALIDITY"), _number(client, "UIDNEXT")
    except core.ConnectorError:
        raise
    except Exception:
        raise core.ConnectorUnavailable(
            "Zoho mailbox is unavailable. Check its app password, IMAP setting and plan. Personal Gmail was not used."
        ) from None
    finally:
        if client:
            try:
                client.logout()  # Never CLOSE/EXPUNGE a mailbox.
            except Exception:
                pass


def _quoted(value):
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise core.ConnectorError(
            "Email search terms cannot contain control characters."
        )
    return b'"' + value.replace("\\", "\\\\").replace('"', '\\"').encode("utf-8") + b'"'


def _criteria(query, lower, upper):
    from agent_inbox import AgentMailRead

    words, dates, sender, subject, _ = AgentMailRead._query(query)
    args = ["UID", f"{lower}:{upper}"]
    for field, value in [("FROM", sender), ("SUBJECT", subject), ("TEXT", words)]:
        if value:
            args.extend([field, _quoted(value)])
    months = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    for name, value in dates.items():
        date = datetime.fromisoformat(value)
        args.extend(
            [
                "SINCE" if name == "after" else "BEFORE",
                f"{date.day:02}-{months[date.month-1]}-{date.year}",
            ]
        )
    return args, sender, subject


def _data(client, operation, uid, fields):
    status, values = client.uid(operation, uid, fields)
    if status != "OK" or not isinstance(values, list):
        raise core.ConnectorUnavailable("Zoho could not complete this email lookup.")
    total = 0
    for item in values:
        chunks = item if isinstance(item, tuple) else (item,)
        if any(not isinstance(chunk, bytes) for chunk in chunks):
            raise core.ConnectorUnavailable("Zoho returned an invalid email response.")
        total += sum(len(chunk) for chunk in chunks)
    if total > MAX_RESPONSE:
        raise core.ConnectorUnavailable("Zoho response exceeded the email read limit.")
    return values


def _literal(values, uid, section, maximum):
    found = []
    for item in values:
        if not isinstance(item, tuple):
            continue
        if len(item) != 2:
            raise core.ConnectorUnavailable("Zoho returned an invalid message part.")
        prefix, raw = item
        identity = re.search(rb"\bUID ([0-9]+)(?=[ )])", prefix, re.I)
        if (
            not identity
            or int(identity[1]) != uid
            or section.upper() not in prefix.upper()
            or len(raw) > maximum
        ):
            raise core.ConnectorUnavailable(
                "Zoho returned a different or oversized message part."
            )
        found.append(raw)
    if len(found) != 1:
        raise core.ConnectorUnavailable(
            "The requested email is unavailable. Search again."
        )
    return found[0]


def _structure(values, uid):
    # Normalize IMAP literals to quoted strings, then parse the FETCH attribute
    # list. UID is checked at the top level, never inside a MIME filename.
    chunks = []
    for item in values:
        if isinstance(item, tuple):
            prefix, literal = item
            prefix = re.sub(rb"\{[0-9]+\}$", b"", prefix)
            chunks.append(
                prefix
                + b'"'
                + literal.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
                + b'"'
            )
        else:
            chunks.append(item)
    raw = b" ".join(chunks)
    if len(raw) > MAX_STRUCTURE:
        raise core.ConnectorUnavailable(
            "This email has too many MIME parts to read safely."
        )
    pos, tokens = 0, 0

    def parse(depth=0):
        nonlocal pos, tokens
        tokens += 1
        if depth > 20 or tokens > 4096:
            raise ValueError()
        while pos < len(raw) and raw[pos : pos + 1].isspace():
            pos += 1
        if pos >= len(raw):
            raise ValueError()
        ch = raw[pos]
        pos += 1
        if ch == 40:
            out = []
            while True:
                while pos < len(raw) and raw[pos : pos + 1].isspace():
                    pos += 1
                if pos >= len(raw):
                    raise ValueError()
                if raw[pos] == 41:
                    pos += 1
                    return out
                out.append(parse(depth + 1))
        if ch == 34:
            out = bytearray()
            while pos < len(raw):
                ch = raw[pos]
                pos += 1
                if ch == 34:
                    return out.decode("utf-8", errors="replace")
                if ch == 92:
                    if pos >= len(raw):
                        raise ValueError()
                    ch = raw[pos]
                    pos += 1
                out.append(ch)
            raise ValueError()
        start = pos - 1
        while pos < len(raw) and raw[pos] not in b" ()\r\n\t":
            pos += 1
        atom = raw[start:pos].decode("ascii")
        if not atom or atom == ")":
            raise ValueError()
        return None if atom.upper() == "NIL" else int(atom) if atom.isdigit() else atom

    try:
        if not isinstance(parse(), int):
            raise ValueError()
        attrs = parse()
        if not isinstance(attrs, list) or len(attrs) % 2 or raw[pos:].strip():
            raise ValueError()
        if not all(isinstance(key, str) for key in attrs[::2]):
            raise ValueError()
        pairs = dict(zip((key.upper() for key in attrs[::2]), attrs[1::2]))
        if len(pairs) * 2 != len(attrs):
            raise ValueError()
        if pairs.get("UID") != uid or not isinstance(pairs.get("BODYSTRUCTURE"), list):
            raise ValueError()
        return pairs["BODYSTRUCTURE"]
    except Exception:
        raise core.ConnectorUnavailable(
            "Zoho returned an unsupported email structure. No attachment was downloaded."
        ) from None


def _parts(node, path=""):
    if not isinstance(node, list) or not node:
        return []

    def attached(disposition):
        return (
            isinstance(disposition, list)
            and bool(disposition)
            and str(disposition[0]).upper() == "ATTACHMENT"
        )

    if isinstance(node[0], list):
        count = next(
            (i for i, part in enumerate(node) if not isinstance(part, list)), len(node)
        )
        if len(node) > count + 2 and attached(node[count + 2]):
            return []
        params = node[count + 1] if len(node) > count + 1 else None
        if isinstance(params, list) and any(
            str(key).upper() in ("NAME", "FILENAME") for key in params[::2]
        ):
            return []
        return [
            part
            for i, child in enumerate(node[:count], 1)
            for part in _parts(child, f"{path}.{i}" if path else str(i))
        ]
    if (
        len(node) < 8
        or str(node[0]).upper() != "TEXT"
        or str(node[1]).upper() not in ("PLAIN", "HTML")
    ):
        return []
    params = node[2] or []
    if not isinstance(params, list) or len(params) % 2:
        return []
    params = {str(k).upper(): v for k, v in zip(params[::2], params[1::2])}
    if (
        "NAME" in params
        or "FILENAME" in params
        or (len(node) > 9 and attached(node[9]))
    ):
        return []
    if not isinstance(node[6], int) or node[6] < 0:
        return []
    return [
        (
            path or "1",
            str(node[1]).lower(),
            str(params.get("CHARSET") or "utf-8"),
            str(node[5]).upper(),
            node[6],
        )
    ]


class ZohoRead:
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
            row["source"] != "zoho"
            or row.get("provider") != "zoho"
            or row["revision"] != self.revision
            or not row.get("sealed")
        ):
            raise core.ConnectorUnavailable(
                "The Zoho mailbox is disconnected or the email source changed. Search again."
            )
        secret = core.unseal_tokens(row["sealed"]).get("password", "")
        if not secret:
            raise core.ConnectorUnavailable(
                "The Zoho app password cannot be opened. Reconnect in Connections."
            )
        return row, secret

    @contextmanager
    def _session(self):
        row, password = self._auth()
        with session(
            row["address"], password, row["host"], self.store.zoho_factory
        ) as value:
            self._auth()
            yield value
            self._auth()  # Discard in-flight results after any source change.

    def _id(self, validity, uid):
        return f"zm1_{self.namespace}_{validity}_{uid}"

    def _decode(self, message_id):
        match = re.fullmatch(
            r"zm1_" + self.namespace + r"_([0-9]{1,10})_([0-9]{1,10})",
            str(message_id or ""),
        )
        if not match or any(not 1 <= int(v) <= 4294967295 for v in match.groups()):
            raise core.ConnectorError(
                "This email reference belongs to a different mailbox. Search again."
            )
        return tuple(map(int, match.groups()))

    def _safe(self, value):
        _, password = self._auth()
        return core.redact_text(value).replace(password, "[redacted]")

    def _metadata(self, client, validity, uid):
        fields = f"(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)]<0.{MAX_HEADERS}>)"
        raw = _literal(
            _data(client, "FETCH", str(uid), fields),
            uid,
            b"BODY[HEADER.FIELDS",
            MAX_HEADERS,
        )
        msg = BytesHeaderParser(policy=policy.default).parsebytes(raw)
        headers = {
            name: (
                self._safe(str(msg.get(name) or ""))[:500]
                if len(msg.get_all(name, [])) <= 1
                else ""
            )
            for name in ("Subject", "From", "Date")
        }
        result = {
            "owner": self.owner,
            "id": self._id(validity, uid),
            "thread_id": self._id(validity, uid),
            "headers": headers,
            "snippet": "",
        }
        self.summaries[result["id"]] = result
        return result

    def search(self, *, query=None, max_results=10, page_token=None):
        self._auth()
        args, sender, subject = _criteria(
            query, 1, 1
        )  # Validate before opening a socket.
        limit = max(1, min(int(max_results or 10), 50))
        saved = (
            self.store.load_page(self.owner, self.revision, query, page_token)
            if page_token
            else None
        )
        with self._session() as (client, validity, next_uid):
            if saved:
                ids = saved.get("uids")
                if (
                    saved.get("validity") != validity
                    or not isinstance(ids, list)
                    or len(ids) > WINDOW
                    or any(
                        not isinstance(uid, int) or not 1 <= uid < next_uid
                        for uid in ids
                    )
                ):
                    raise core.ConnectorError("The Zoho Inbox changed. Search again.")
            elif next_uid == 1:
                ids = []
            else:
                lower, upper = max(1, next_uid - WINDOW), next_uid - 1
                args, sender, subject = _criteria(query, lower, upper)
                utf8 = any(isinstance(arg, bytes) and not arg.isascii() for arg in args)
                status, values = client.uid(
                    "SEARCH", *(["CHARSET", "UTF-8"] if utf8 else []), *args
                )
                if (
                    status != "OK"
                    or not isinstance(values, list)
                    or len(values) != 1
                    or not isinstance(values[0], bytes)
                    or len(values[0]) > 60000
                ):
                    raise core.ConnectorUnavailable(
                        "Zoho could not complete this bounded email search."
                    )
                raw = values[0].split()
                if len(raw) > WINDOW or any(
                    not re.fullmatch(rb"[0-9]{1,10}", uid)
                    or not lower <= int(uid) <= upper
                    for uid in raw
                ):
                    raise core.ConnectorUnavailable(
                        "Zoho returned invalid message identities."
                    )
                ids = sorted(set(map(int, raw)), reverse=True)
                if not ids and lower > 1:
                    raise core.ConnectorUnavailable(
                        "No matches in the recent Inbox search window. Older messages can be checked in Zoho Mail."
                    )
            hits, consumed = [], 0
            for uid in ids[:50]:
                msg = self._metadata(client, validity, uid)
                consumed += 1
                addresses = getaddresses([msg["headers"]["From"]])
                if (
                    sender
                    and "@" in sender
                    and (
                        len(addresses) != 1 or addresses[0][1].lower() != sender.lower()
                    )
                ):
                    continue
                if (
                    sender
                    and "@" not in sender
                    and sender.lower() not in msg["headers"]["From"].lower()
                ):
                    continue
                if subject and subject.lower() not in msg["headers"]["Subject"].lower():
                    continue
                hits.append({k: msg[k] for k in ("owner", "id", "thread_id")})
                if len(hits) >= limit:
                    break
            remaining = ids[consumed:]
            token = (
                self.store.save_page(
                    self.owner,
                    self.revision,
                    query,
                    {"validity": validity, "uids": remaining},
                )
                if remaining
                else ""
            )
            result = {"owner": self.owner, "messages": hits, "next_page_token": token}
        return result

    def latest(self, *, query=None, max_results=10):
        # UIDs increase for newly delivered Inbox messages. Search is already
        # ordered by received identity, not full-text relevance or quoted Date.
        page = self.search(query=query, max_results=1)
        if not page["messages"] and page["next_page_token"]:
            raise core.ConnectorUnavailable(
                "Narrow the email search to identify the latest exact match."
            )
        return {**page, "next_page_token": ""}

    def message(self, message_id):
        self._auth()
        wanted, uid = self._decode(message_id)
        if message_id in self.summaries:
            return self.summaries[message_id]
        with self._session() as (client, validity, _):
            if wanted != validity:
                raise core.ConnectorUnavailable(
                    "The Zoho Inbox was recreated. Search again."
                )
            result = self._metadata(client, validity, uid)
        return result

    def read_message(self, message_id):
        self._auth()
        wanted, uid = self._decode(message_id)
        with self._session() as (client, validity, _):
            if wanted != validity:
                raise core.ConnectorUnavailable(
                    "The Zoho Inbox was recreated. Search again."
                )
            result = self._metadata(client, validity, uid)
            tree = _structure(
                _data(client, "FETCH", str(uid), "(UID BODYSTRUCTURE)"), uid
            )
            parts = sorted(_parts(tree), key=lambda part: part[1] != "plain")
            if not parts:
                raise core.ConnectorUnavailable(
                    "This email has no readable inline text. Attachments are not downloaded."
                )
            # At most one plain and one HTML alternative. Blank plain text
            # must not hide a real HTML message; never concatenate attachments.
            alternatives = [
                next((part for part in parts if part[1] == kind), None)
                for kind in ("plain", "html")
            ]
            text = ""
            for candidate in alternatives:
                if not candidate:
                    continue
                part, kind, charset, encoding, size = candidate
                raw = _literal(
                    _data(
                        client,
                        "FETCH",
                        str(uid),
                        f"(UID BODY.PEEK[{part}]<0.{MAX_TEXT}>)",
                    ),
                    uid,
                    f"BODY[{part}]".encode(),
                    MAX_TEXT,
                )
                try:
                    if encoding == "BASE64":
                        raw = base64.b64decode(raw + b"=" * (-len(raw) % 4))
                    elif encoding == "QUOTED-PRINTABLE":
                        raw = quopri.decodestring(raw)
                    elif encoding not in ("7BIT", "8BIT", "BINARY"):
                        raise ValueError()
                    try:
                        text = raw.decode(charset, errors="replace")
                    except LookupError:
                        text = raw.decode("utf-8", errors="replace")
                except Exception:
                    raise core.ConnectorUnavailable(
                        "This email text could not be decoded. No attachment was downloaded."
                    ) from None
                if kind == "html":
                    text = core._gmail_html_to_text(text)
                if text.strip():
                    break
            if not text.strip():
                raise core.ConnectorUnavailable(
                    "This email contains no readable inline text. Attachments are not downloaded."
                )
            result = {
                **result,
                "body": self._safe(text)[: core._GMAIL_BODY_MAX],
                "body_type": "text" if kind == "plain" else "html",
                "truncated": size > MAX_TEXT or len(text) > core._GMAIL_BODY_MAX,
            }
        return result
