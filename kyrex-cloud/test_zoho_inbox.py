"""Dedicated Zoho reader checks. All provider traffic is an injected fake."""

import base64
import json
import sqlite3
import ssl
from types import SimpleNamespace

import pytest
import agent_inbox
import connectors as core
import zoho_inbox as zoho

EMAIL = "keith@kyrex.example"
PASSWORD = "zoho-test-app-credential-" + "x" * 24
PLAIN = b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 70 3)'
HEADER = b"From: School <school@example.com>\r\nSubject: Field trip\r\nDate: Tue, 13 Oct 2026 09:00:00 +0000\r\n\r\n"
BODY = b"Forwarded message\nFrom: School\nThe field trip is at 9am; bring lunch."


class Fake:
    def __init__(self):
        self.calls = []
        self.validity, self.next_uid = 88, 30
        self.ids = [2, 20]
        self.headers = {uid: HEADER for uid in self.ids}
        self.structure = PLAIN
        self.body = BODY
        self.callback = None
        self.failure = False

    def factory(self, host, **kwargs):
        self.calls.append(("open", host, kwargs["port"]))
        assert kwargs["ssl_context"].check_hostname
        assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["timeout"] == 20
        return self

    def login(self, address, password):
        self.calls.append(("login", address))
        assert address == EMAIL and password == PASSWORD
        if self.callback:
            self.callback()
        if self.failure:
            raise RuntimeError("Provider leaked " + PASSWORD)
        return "OK", []

    def select(self, folder, readonly=False):
        assert folder == "INBOX" and readonly is True
        self.calls.append(("examine", folder))
        return "OK", [b"2"]

    def response(self, key):
        return key, [
            str(self.validity if key == "UIDVALIDITY" else self.next_uid).encode()
        ]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [b" ".join(str(uid).encode() for uid in self.ids)]
        assert command == "FETCH"
        uid, fields = int(args[0]), args[1]
        if fields == "(UID BODYSTRUCTURE)":
            return "OK", [
                b"1 (UID "
                + str(uid).encode()
                + b" BODYSTRUCTURE "
                + self.structure
                + b")"
            ]
        assert "BODY.PEEK[" in fields, "Reading never changes Seen flags"
        if "HEADER.FIELDS" in fields:
            section, raw = (
                "HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)",
                self.headers.get(uid, HEADER),
            )
        else:
            section = fields.split("BODY.PEEK[")[1].split("]")[0]
            raw = self.body
        return "OK", [
            (f"1 (UID {uid} BODY[{section}]<0> {{{len(raw)}}}".encode(), raw),
            b")",
        ]

    def logout(self):
        self.calls.append(("logout",))
        return "BYE", []


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_SESSION_SECRET", "zoho-inbox-test-encryption")
    store = agent_inbox.AgentInboxStore(tmp_path / "inbox.sqlite3")
    fake = Fake()
    store.zoho_factory = fake.factory
    monkeypatch.setattr(agent_inbox, "default_store", lambda: store)
    return store, fake


def connect(setup):
    store, fake = setup
    view = store.connect_zoho("alice", EMAIL, PASSWORD, zoho.HOSTS[0])
    fake.calls.clear()
    return store, fake, view


def test_connect_checks_only_dedicated_inbox_and_seals_password(setup):
    store, fake = setup
    view = store.connect_zoho("alice", EMAIL, PASSWORD, zoho.HOSTS[0])
    assert view["source"] == view["provider"] == "zoho" and view["active"]
    assert not view["personal_gmail_enabled"]
    assert not any(c[0] in ("FETCH", "SEARCH") for c in fake.calls)
    assert (
        PASSWORD not in json.dumps(view)
        and PASSWORD.encode() not in store.path.read_bytes()
    )
    assert not store.view("bob")["connected"]
    fake.calls.clear()
    store.connect_zoho("alice", EMAIL, "", zoho.HOSTS[0])
    assert fake.calls, "Keeping a secret still checks its access"


def test_legacy_agentmail_database_migrates_without_losing_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WEB_SESSION_SECRET", "migration-test-encryption")
    path = tmp_path / "legacy.sqlite3"
    sealed = core.seal_tokens({"api_key": "agentmail-test-key"})
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE inboxes (owner TEXT PRIMARY KEY, source TEXT NOT NULL, inbox TEXT, address TEXT, sealed TEXT, revision TEXT NOT NULL, checked REAL)"
        )
        db.execute(
            "INSERT INTO inboxes VALUES (?,?,?,?,?,?,?)",
            (
                "alice",
                "agentmail",
                "inb_old",
                "old@agentmail.to",
                sealed,
                "old-revision",
                1,
            ),
        )
    store = agent_inbox.AgentInboxStore(path)
    assert (
        store.view("alice")["provider"] == "agentmail" and store.view("alice")["active"]
    )
    assert (
        store.record("alice")["sealed"] == sealed
        and store.epoch("alice")[1] == "old-revision"
    )


@pytest.mark.parametrize(
    "host", ["localhost", "imap.zoho.com.evil", "127.0.0.1", "smtp.zoho.com"]
)
def test_arbitrary_servers_rejected_before_credentials_leave_process(setup, host):
    store, fake = setup
    with pytest.raises(core.ConnectorError):
        store.connect_zoho("alice", EMAIL, PASSWORD, host)
    assert not fake.calls


def test_owner_scoped_email_route_and_no_personal_gmail_fallback(setup):
    store, fake, _ = connect(setup)
    google = core.ConnectorStore(store.path.parent / "google.json")
    assert google.gmail_read_available("alice") and not google.gmail_read_available(
        "bob"
    )
    reader = google.gmail(
        "alice", transport=lambda *a: pytest.fail("Google transport used")
    )
    page = reader.search(query="field trip")
    assert len(page["messages"]) == 2 and set(page["messages"][0]) == {
        "owner",
        "id",
        "thread_id",
    }
    assert page["messages"][0]["id"].endswith("_20")
    result = reader.read_message(page["messages"][0]["id"])
    assert "bring lunch" in result["body"] and result["body_type"] == "text"
    assert all(
        call[0] in ("open", "login", "examine", "FETCH", "SEARCH", "logout")
        for call in fake.calls
    )
    fake.failure = True
    with pytest.raises(core.ConnectorUnavailable) as exc:
        store.reader("alice").search()
    assert PASSWORD not in str(exc.value) and store.view("alice")["source"] == "zoho"


def test_disconnect_blocks_gmail_and_pending_first_connect(setup):
    store, fake = setup
    fake.callback = lambda: store.disconnect("alice")
    with pytest.raises(core.ConnectorUnavailable):
        store.connect_zoho("alice", EMAIL, PASSWORD, zoho.HOSTS[0])
    assert store.view("alice")["source"] == "none"
    fake.callback = None
    store.connect_zoho("alice", EMAIL, PASSWORD, zoho.HOSTS[0])
    store.disconnect("alice")
    assert (
        store.view("alice")["source"] == "none" and not store.view("alice")["connected"]
    )
    assert not core.ConnectorStore(
        store.path.parent / "google.json"
    ).gmail_read_available("alice")


def test_inflight_result_discarded_after_source_switch(setup):
    store, fake, _ = connect(setup)
    reader = store.reader("alice")
    fake.callback = lambda: store.select("alice", "none")
    with pytest.raises(core.ConnectorUnavailable):
        reader.search()
    assert not any(c[0] == "FETCH" for c in fake.calls)


def test_message_reference_requires_owner_revision_and_mailbox_generation(setup):
    store, fake, _ = connect(setup)
    mid = store.reader("alice").search()["messages"][0]["id"]
    fake.validity += 1
    with pytest.raises(core.ConnectorUnavailable):
        store.reader("alice").read_message(mid)
    assert not any(c[0] == "FETCH" and "BODY.PEEK[1]" in c[-1] for c in fake.calls)
    store.connect_zoho("alice", EMAIL, PASSWORD, zoho.HOSTS[0])
    fake.calls.clear()
    with pytest.raises(core.ConnectorError):
        store.reader("alice").read_message(mid)
    with pytest.raises(core.ConnectorError):
        store.reader("alice").read_message("old-gmail-id")
    assert not fake.calls


def test_snapshot_pagination_is_query_revision_and_uidvalidity_bound(setup):
    store, fake, _ = connect(setup)
    page = store.reader("alice").search(query="trip", max_results=1)
    token = page["next_page_token"]
    assert token and len(token) < 1024
    later = store.reader("alice").search(query="trip", max_results=1, page_token=token)
    assert later["messages"][0]["id"].endswith("_2") and not later["next_page_token"]
    assert len([c for c in fake.calls if c[0] == "SEARCH"]) == 1
    with pytest.raises(core.ConnectorError):
        store.reader("alice").search(query="other", page_token=token)
    fake.validity += 1
    with pytest.raises(core.ConnectorError):
        store.reader("alice").search(query="trip", page_token=token)


def test_exact_outer_sender_is_checked_and_search_is_bounded(setup):
    store, fake, _ = connect(setup)
    fake.headers[20] = HEADER.replace(
        b"school@example.com", b"school@example.com.attacker.test"
    )
    page = store.reader("alice").search(
        query="in:inbox from:school@example.com newer_than:7d"
    )
    assert len(page["messages"]) == 1 and page["messages"][0]["id"].endswith("_2")
    search = next(c for c in fake.calls if c[0] == "SEARCH")
    assert "UID" in search and "1:29" in search and "SINCE" in search
    fake.next_uid = 10000
    fake.ids = [9999]
    store.reader("alice").search()
    assert any(c[0] == "SEARCH" and "5000:9999" in c for c in fake.calls)
    fake.ids = []
    with pytest.raises(core.ConnectorUnavailable, match="recent Inbox"):
        store.reader("alice").search()


@pytest.mark.parametrize(
    "query", ["trip OR password", "has:attachment", 'subject:"a\r\nb"', "after:wrong"]
)
def test_invalid_search_fails_before_socket(setup, query):
    store, fake, _ = connect(setup)
    with pytest.raises(core.ConnectorError):
        store.reader("alice").search(query=query)
    assert not fake.calls


def test_mime_alternative_selects_plain_and_never_reads_named_attachment(setup):
    store, fake, _ = connect(setup)
    html = b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 90 3)'
    attachment = b'("TEXT" "PLAIN" ("NAME" "private.txt") NIL NIL "7BIT" 100 3 NIL ("ATTACHMENT" ("FILENAME" "private.txt")))'
    fake.structure = (
        b"(" + attachment + b" (" + html + b" " + PLAIN + b' "ALTERNATIVE") "MIXED")'
    )
    reader = store.reader("alice")
    mid = reader.search()["messages"][0]["id"]
    result = reader.read_message(mid)
    assert result["body"] == BODY.decode()
    body_fetch = [
        c[-1]
        for c in fake.calls
        if c[0] == "FETCH" and "HEADER.FIELDS" not in c[-1] and "BODY.PEEK" in c[-1]
    ]
    assert body_fetch == ["(UID BODY.PEEK[2.2]<0.131072>)"]


def test_html_base64_unicode_headers_and_body_bounds(setup):
    store, fake, _ = connect(setup)
    fake.structure = b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "BASE64" 200000 3)'
    fake.body = base64.b64encode(
        b"<script>secret_script</script><p>Bring lunch</p>" + b"x" * 22000
    )
    fake.headers[20] = HEADER.replace(b"Field trip", b"=?utf-8?b?Q2Fmw6k=?=")
    reader = store.reader("alice")
    mid = reader.search()["messages"][0]["id"]
    msg = reader.read_message(mid)
    assert msg["headers"]["Subject"] == "Café"
    assert "Bring lunch" in msg["body"] and "secret_script" not in msg["body"]
    assert len(msg["body"]) <= 20000 and msg["truncated"]


def test_named_multipart_is_skipped_and_blank_plain_uses_html(setup):
    store, fake, _ = connect(setup)
    named = b"(" + PLAIN + b' "MIXED" ("NAME" "private.txt"))'
    html = b'("TEXT" "HTML" NIL NIL NIL "7BIT" 30 1)'
    fake.structure = (
        b"(" + named + b" (" + PLAIN + b" " + html + b' "ALTERNATIVE") "MIXED")'
    )
    original = fake.uid

    def response(command, *args):
        if command == "FETCH" and "BODY.PEEK[2.1]" in args[-1]:
            fake.body = b"\r\n"
        elif command == "FETCH" and "BODY.PEEK[2.2]" in args[-1]:
            fake.body = b"<p>Trip at 9am</p>"
        return original(command, *args)

    fake.uid = response
    reader = store.reader("alice")
    mid = reader.search()["messages"][0]["id"]
    result = reader.read_message(mid)
    assert "Trip at 9am" in result["body"] and result["body_type"] == "html"
    assert not any(c[0] == "FETCH" and "BODY.PEEK[1.1]" in c[-1] for c in fake.calls)


def test_attachments_only_and_foreign_uid_are_rejected(setup):
    store, fake, _ = connect(setup)
    fake.structure = b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 100)'
    reader = store.reader("alice")
    mid = reader.search()["messages"][0]["id"]
    with pytest.raises(core.ConnectorUnavailable, match="inline text"):
        reader.read_message(mid)
    assert not any(c[0] == "FETCH" and "BODY.PEEK[1]" in c[-1] for c in fake.calls)
    with pytest.raises(core.ConnectorUnavailable):
        zoho._literal([(b"1 (UID 999 BODY[1] {3}", b"bad")], 20, b"BODY[1]", 100)
    with pytest.raises(core.ConnectorUnavailable):
        zoho.BoundedIMAP.read(None, zoho.MAX_RESPONSE + 1)


@pytest.mark.parametrize(
    "raw",
    [
        b"bad",
        b'1 (UID 20 BODYSTRUCTURE ("TEXT"',
        b'1 (UID 99 BODYSTRUCTURE ("TEXT" "PLAIN"))',
        b"1 (UID 20 BODYSTRUCTURE " + b"(" * 30 + b"x" + b")" * 30 + b")",
    ],
)
def test_malformed_structures_fail_closed(raw):
    with pytest.raises(core.ConnectorUnavailable):
        zoho._structure([raw], 20)


def test_unsolicited_flag_updates_and_literal_structures_are_framed_separately():
    prefix = b'1 (UID 20 BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" {5}'
    suffix = b') NIL NIL "7BIT" 70 3))'
    values = [
        b"99 (UID 99 FLAGS (\\Seen))",
        (prefix, b"UTF-8"),
        suffix,
        b"2 (FLAGS (\\Answered))",
    ]
    tree = zoho._structure(values, 20)
    assert zoho._parts(tree)[0][:3] == ("1", "plain", "UTF-8")
    assert zoho._parts([tree]) == [], "Missing multipart subtype fails closed"


def test_email_bot_task_uses_zoho_without_a_google_grant(setup, monkeypatch):
    import serve
    import task_store

    store, fake, _ = connect(setup)
    tasks = task_store.CloudTaskStore(db_path=store.path.parent / "tasks.sqlite3")
    command = "gmail: latest field trip"
    tid = tasks.submit(
        "email-bot",
        command,
        bot_id="email-bot",
        executor_prefix="gmail",
        resolve_bot=False,
    )
    assert tasks.claim_next("fake-worker")["task_id"] == tid
    monkeypatch.setattr(task_store, "CloudTaskStore", lambda: tasks)
    monkeypatch.setattr(
        core,
        "default_store",
        lambda: core.ConnectorStore(store.path.parent / "google.json"),
    )
    results = []
    serve._run_gmail_read_task(
        SimpleNamespace(bot_id="email-bot", bot_owner="alice"),
        "chat",
        command,
        tid,
        lambda *a: None,
        on_result=results.append,
    )
    assert results[0]["count"] == 1 and "bring lunch" in results[0]["selected"]["body"]
