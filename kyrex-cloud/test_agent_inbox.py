"""Privacy and routing boundaries, with deterministic provider responses."""

import json
import time
from types import SimpleNamespace

import pytest
import agent_inbox as inbox
import connectors as core

KEY = "inbox-test-credential-" + "x" * 32
OWNER = "alice"
INBOX = "inb_school"


def message(mid="school-message", **values):
    return dict(
        inbox_id=INBOX,
        message_id=mid,
        thread_id="school-thread",
        subject="Field trip",
        timestamp="2026-10-12T09:00:00Z",
        **{"from": "School <school@example.com>"},
        preview="Trip at 9am",
        labels=["received"],
        **values
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "agent-inbox-test-encryption")
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    calls = []

    def request(path, key, params=None):
        calls.append((path, key, params))
        assert key == KEY
        if path == INBOX:
            return {"inbox_id": INBOX, "email": "school-inbox@agentmail.to"}
        if "/messages/" in path and not path.endswith("/search"):
            return message(
                text="Forwarded message\nFrom: School\nTrip at 9am; bring lunch.",
                extracted_text="Forwarded message",
            )
        return {"messages": [message()]}

    store = inbox.AgentInboxStore(tmp_path / "inbox.sqlite3", request)
    monkeypatch.setattr(inbox, "default_store", lambda: store)
    return store, calls


def connected(setup):
    store, calls = setup
    view = store.connect(OWNER, INBOX, KEY)
    calls.clear()
    return store, calls, view


def test_sealed_owner_scoped_and_read_permissions_verified(setup):
    store, calls = setup
    view = store.connect(OWNER, INBOX, KEY)
    assert len(calls) == 2 and calls[1][2] == {"limit": 1}
    assert view["active"] and not view["personal_gmail_enabled"]
    assert KEY not in json.dumps(view) and KEY.encode() not in store.path.read_bytes()
    assert not store.view("bob")["connected"]
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert inbox.AgentInboxStore(store.path).view(OWNER) == view


def test_failed_validation_does_not_switch_or_leak_provider_errors(setup):
    store, _ = setup
    store.select(OWNER, "none")
    store.request = lambda *args: (_ for _ in ()).throw(
        core.ConnectorUnavailable("Unavailable")
    )
    with pytest.raises(core.ConnectorError):
        store.connect(OWNER, INBOX, KEY)
    assert store.view(OWNER)["source"] == "none"


def test_route_uses_only_dedicated_inbox_and_preserves_full_forward(setup):
    store, calls, _ = connected(setup)
    google = core.ConnectorStore(store.path.parent / "google.json")
    reader = google.gmail(
        OWNER, transport=lambda *a: pytest.fail("Personal Gmail requested")
    )
    assert google.gmail_read_available(OWNER)
    page = reader.search(query="field trip")
    assert set(page["messages"][0]) == {"owner", "id", "thread_id"}
    mid = page["messages"][0]["id"]
    body = reader.read_message(mid)
    assert "bring lunch" in body["body"]
    assert all(path.startswith(INBOX + "/messages") for path, _, _ in calls)
    assert "attachments" not in body and KEY not in json.dumps(body)


@pytest.mark.parametrize("source", ["none", "gmail"])
def test_switch_invalidates_cached_messages_and_inflight_results(setup, source):
    store, calls, _ = connected(setup)
    reader = store.reader(OWNER)
    mid = reader.search()["messages"][0]["id"]
    request = store.request

    def switch(*args):
        result = request(*args)
        store.select(OWNER, source)
        return result

    store.request = switch
    with pytest.raises(core.ConnectorUnavailable):
        reader.read_message(mid)
    count = len(calls)
    with pytest.raises(core.ConnectorUnavailable):
        reader.message(mid)
    assert len(calls) == count


def test_disconnect_never_reenables_gmail_and_old_google_readers_block(setup):
    store, _ = setup
    google = core.ConnectorStore(store.path.parent / "google.json")
    old = core.GmailRead(google, OWNER, transport=lambda *a: pytest.fail("Google read"))
    store.connect(OWNER, INBOX, KEY)
    with pytest.raises(core.ConnectorUnavailable):
        old.search()
    store.disconnect(OWNER)
    assert not google.gmail_read_available(OWNER)
    with pytest.raises(core.ConnectorUnavailable):
        google.gmail(OWNER)
    assert store.view(OWNER)["source"] == "none"
    assert not store.view(OWNER)["connected"]
    with pytest.raises(core.ConnectorUnavailable):
        google.begin_gmail_read_upgrade(OWNER)


def test_calendar_still_uses_google_when_mail_is_blocked(setup, monkeypatch):
    store, _ = setup
    store.select(OWNER, "none")
    google = core.ConnectorStore(store.path.parent / "google.json")
    monkeypatch.setattr(
        google, "route_capability", lambda *a: {"bot_role": "calendar_bot"}
    )
    monkeypatch.setattr(google, "access_token", lambda *a: "calendar-test-token")
    monkeypatch.setattr(
        google, "status", lambda *a: {"scopes": [core.GOOGLE_CALENDAR_READ_SCOPE]}
    )
    result = google.calendar(OWNER, transport=lambda *a: {"items": []}).events()
    assert result == []


def test_ids_bound_to_owner_and_revision_before_network(setup):
    store, calls, _ = connected(setup)
    first = store.reader(OWNER).search()["messages"][0]["id"]
    store.connect("bob", INBOX, KEY)
    store.connect(OWNER, INBOX, "")
    calls.clear()
    for owner in [OWNER, "bob"]:
        with pytest.raises(core.ConnectorError):
            store.reader(owner).read_message(first)
        with pytest.raises(core.ConnectorError):
            store.reader(owner).read_message("old-gmail-id")
    assert not calls


def test_provider_failure_never_falls_back_to_google(setup):
    store, calls, _ = connected(setup)
    store.request = lambda *a: (_ for _ in ()).throw(
        core.ConnectorUnavailable("Unavailable")
    )
    google = core.ConnectorStore(store.path.parent / "google.json")
    with pytest.raises(core.ConnectorUnavailable):
        google.gmail(
            OWNER, transport=lambda *a: pytest.fail("Google fallback")
        ).search()
    assert store.view(OWNER)["source"] == "agentmail"


def test_exact_outer_sender_and_received_filter(setup):
    store, _, _ = connected(setup)
    values = [
        message("correct"),
        {**message("manual"), "from": "Keith <keith@example.com>"},
        {**message("sent"), "labels": ["sent"]},
    ]
    store.request = lambda *a: {"messages": values}
    page = store.reader(OWNER).search(
        query="in:inbox from:school@example.com newer_than:7d"
    )
    assert len(page["messages"]) == 1 and page["messages"][0]["id"].endswith(
        inbox._encoded("correct")
    )


def test_pagination_bound_to_query_owner_epoch_and_server_params(setup):
    store, _, _ = connected(setup)
    request = store.request
    store.request = lambda *a: {
        "messages": [message()],
        "next_page_token": "provider-page",
    }
    token = store.reader(OWNER).search(query="trip")["next_page_token"]
    assert len(token) < core._GMAIL_PAGE_TOKEN_MAX
    calls = []
    store.request = lambda *a: calls.append(a) or {"messages": []}
    store.reader(OWNER).search(query="trip", page_token=token)
    assert calls[-1][2]["page_token"] == "provider-page"
    for query, page in [
        ("different", token),
        ("trip", token + "x"),
        ("trip", "x" * 5000),
    ]:
        with pytest.raises(core.ConnectorError):
            store.reader(OWNER).search(query=query, page_token=page)
    store.select(OWNER, "none")
    store.request = request
    store.connect(OWNER, INBOX, KEY)
    with pytest.raises(core.ConnectorError):
        store.reader(OWNER).search(query="trip", page_token=token)


def test_latest_uses_dates_after_complete_relevance_ranked_search(setup):
    store, _, _ = connected(setup)
    older = message("older")
    newer = {**message("newer"), "timestamp": "2026-10-13T09:00:00Z"}
    store.request = lambda *a: {"messages": [older, newer]}
    latest = store.reader(OWNER).latest(query="trip")["messages"][0]["id"]
    assert latest.endswith(inbox._encoded("newer"))
    store.request = lambda *a: {"messages": [older], "next_page_token": "more"}
    with pytest.raises(core.ConnectorUnavailable):
        store.reader(OWNER).latest(query="trip")


@pytest.mark.parametrize(
    "query", ["has:attachment", "trip OR party", "{school party}", "after:not-a-date"]
)
def test_unsupported_queries_do_not_broaden_search(setup, query):
    store, calls, _ = connected(setup)
    with pytest.raises(core.ConnectorError):
        store.reader(OWNER).search(query=query)
    assert not calls


def test_transport_redacts_failure_and_does_not_follow_redirects(monkeypatch):
    calls = []

    class Response:
        status_code = 302

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(
        inbox.requests, "get", lambda *a, **kw: calls.append((a, kw)) or Response()
    )
    with pytest.raises(core.ConnectorUnavailable) as exc:
        inbox.transport(INBOX, KEY)
    assert KEY not in str(exc.value)
    assert calls[0][0][0] == inbox.API + INBOX
    assert calls[0][1]["allow_redirects"] is False


def test_wrong_provider_identity_rejected(setup):
    store, _, _ = connected(setup)
    store.request = lambda *a: {
        "messages": [{**message(), "inbox_id": "another-inbox"}]
    }
    with pytest.raises(core.ConnectorUnavailable):
        store.reader(OWNER).search()


def test_connect_cannot_override_a_concurrent_disconnect(setup):
    store, _ = setup
    request = store.request

    def changed(*args):
        result = request(*args)
        store.disconnect(OWNER)
        return result

    store.request = changed
    with pytest.raises(core.ConnectorUnavailable):
        store.connect(OWNER, INBOX, KEY)
    assert store.view(OWNER)["source"] == "none"


def test_google_inflight_result_not_released_after_source_switch(setup, monkeypatch):
    store, _ = setup
    google = core.ConnectorStore(store.path.parent / "google.json")
    monkeypatch.setattr(
        google, "route_capability", lambda *a: {"bot_role": "gmail_bot"}
    )
    monkeypatch.setattr(
        google, "status", lambda *a: {"scopes": [core.GOOGLE_GMAIL_READ_SCOPE]}
    )
    monkeypatch.setattr(google, "access_token", lambda *a: "google-test-token")

    def response(*args):
        store.select(OWNER, "none")
        return {"messages": [{"id": "private-message"}]}

    with pytest.raises(core.ConnectorUnavailable):
        google.gmail(OWNER, transport=response).search()


@pytest.mark.parametrize(
    "command", ["gmail: search field trip", "gmail: latest field trip"]
)
def test_email_bot_executor_reads_dedicated_inbox_without_google_grant(
    setup, monkeypatch, command
):
    import serve
    import task_store

    store, _, _ = connected(setup)
    tasks = task_store.CloudTaskStore(db_path=store.path.parent / "tasks.sqlite3")
    task_id = tasks.submit(
        "email-bot",
        command,
        bot_id="email-bot",
        executor_prefix="gmail",
        resolve_bot=False,
    )
    assert tasks.claim_next("test-worker")["task_id"] == task_id
    monkeypatch.setattr(task_store, "CloudTaskStore", lambda: tasks)
    monkeypatch.setattr(
        core,
        "default_store",
        lambda: core.ConnectorStore(store.path.parent / "google.json"),
    )
    results, sent = [], []
    serve._run_gmail_read_task(
        SimpleNamespace(bot_id="email-bot", bot_owner=OWNER),
        "chat",
        command,
        task_id,
        lambda *args: sent.append(args),
        on_result=results.append,
    )
    assert results[0]["count"] == 1
    assert "Field trip" in results[0]["final_response"]
    if "latest" in command:
        assert "bring lunch" in results[0]["selected"]["body"]
    assert sent and not core.default_store().scope_granted(
        OWNER, core.GOOGLE_GMAIL_READ_SCOPE
    )
