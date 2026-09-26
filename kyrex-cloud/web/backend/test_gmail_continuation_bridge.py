"""Regressions for bounded Gmail continuations inside one delegated Chat turn."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gmail_continuation_bridge as bridge  # noqa: E402
import serve  # noqa: E402


def _session():
    return SimpleNamespace(delegation_ctx={
        "owner": "alice",
        "conversation_id": "conversation-1",
    })


def test_read_number_syncs_same_turn_search_before_resolving_selection():
    events = []
    state = {"conv": {}}

    def sync(owner, conversation_id):
        events.append(("sync", owner, conversation_id))
        # Simulate the just-completed delegated Gmail search being persisted by
        # Jev's sync wrapper before this same Kyrex turn asks to read #5.
        state["conv"] = {
            "gmail_page": {"hits": [
                {"id": "m1"}, {"id": "m2"}, {"id": "m3"},
                {"id": "m4"}, {"id": "field-trip-message"},
            ]}
        }
        return {"delegations": [], "relayed": []}

    def select_command(conv, index):
        events.append(("select", index, len(conv["gmail_page"]["hits"])))
        hit = conv["gmail_page"]["hits"][index - 1]
        return f'gmail: read id {hit["id"]} focus "4th grade field trip"'

    chat = SimpleNamespace(
        serve=serve,
        sync_delegated_work=sync,
        get_conversation=lambda owner, cid: state["conv"],
        _gmail_select_command=select_command,
    )

    resolved = bridge.resolve_continuation(
        chat, _session(), "read number 5")

    assert resolved == (
        "command",
        'gmail: read id field-trip-message focus "4th grade field trip"',
    )
    assert events == [
        ("sync", "alice", "conversation-1"),
        ("select", 5, 5),
    ]


def test_namespaced_read_number_is_still_a_bounded_selection():
    chat = SimpleNamespace(
        serve=serve,
        sync_delegated_work=lambda *a: None,
        get_conversation=lambda *a: {"gmail_page": {"hits": [{"id": "m1"}]}},
        _gmail_select_command=lambda conv, index: "gmail: read id m1",
    )
    assert bridge.resolve_continuation(
        chat, _session(), "gmail: read number 1") == (
            "command", "gmail: read id m1")


def test_show_more_uses_stored_page_token_not_model_authored_query():
    seen = []
    chat = SimpleNamespace(
        serve=serve,
        sync_delegated_work=lambda *a: seen.append("sync"),
        get_conversation=lambda *a: {
            "gmail_page": {"next_page_token": "opaque", "query": "field trip"}},
        _gmail_continuation_command=lambda conv: (
            "gmail: more opaque field trip"),
    )
    assert bridge.resolve_continuation(
        chat, _session(), "show 5 more") == (
            "command", "gmail: more opaque field trip")
    assert seen == ["sync"]


def test_selection_without_stored_page_fails_instead_of_repeating_search():
    called = []
    chat = SimpleNamespace(
        serve=serve,
        sync_delegated_work=lambda *a: None,
        get_conversation=lambda *a: {},
        _gmail_select_command=lambda conv, index: None,
    )
    jev = SimpleNamespace(
        _submit_routed_gmail=lambda *a, **k: called.append(True))

    bridge._installed = False
    bridge.install(chat, jev)
    ok, payload = jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(),
        {"task": "read number 5"},
        {"request_text": "Find the details for the 4th grade field trip"},
    )

    assert ok is False
    assert "result page" in payload["error"]
    assert called == []


def test_resolved_selection_bypasses_original_search_only_for_continuation():
    calls = []
    chat = SimpleNamespace(
        serve=serve,
        sync_delegated_work=lambda *a: None,
        get_conversation=lambda *a: {"gmail_page": {"hits": [{"id": "m1"}]}},
        _gmail_select_command=lambda conv, index: "gmail: read id m1",
    )

    def original_submit(chat_arg, dev_bot, session, frame, hint):
        calls.append((dict(frame), dict(hint)))
        return True, {"task": frame["task"]}

    jev = SimpleNamespace(_submit_routed_gmail=original_submit)
    bridge._installed = False
    bridge.install(chat, jev)

    original_request = "Find the details for the 4th grade field trip"
    result = jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(),
        {"task": "read number 1", "target_bot_id": "email-bot"},
        {"request_text": original_request, "selected_bot_id": "email-bot"},
    )

    assert result == (True, {"task": "gmail: read id m1"})
    assert calls[0][0]["task"] == "gmail: read id m1"
    assert calls[0][1]["request_text"] == ""
    assert calls[0][1]["selected_bot_id"] == "email-bot"


def test_non_continuation_keeps_original_request_authority_unchanged():
    calls = []
    chat = SimpleNamespace(serve=serve)

    def original_submit(chat_arg, dev_bot, session, frame, hint):
        calls.append((dict(frame), dict(hint)))
        return True, {}

    jev = SimpleNamespace(_submit_routed_gmail=original_submit)
    bridge._installed = False
    bridge.install(chat, jev)

    request = "Find the details for the 4th grade field trip"
    jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(),
        {"task": "gmail: read 4th grade field trip"},
        {"request_text": request},
    )

    assert calls == [(
        {"task": "gmail: read 4th grade field trip"},
        {"request_text": request},
    )]


def test_routed_gmail_search_is_followed_to_terminal_and_remembered(monkeypatch):
    """A quick read-only Gmail task returns its safe result in the same turn."""
    monkeypatch.setattr(bridge, "_GMAIL_FOLLOW_MAX_SECONDS", 1.0)
    remembered = []

    search_result = {
        "status": "ok",
        "mode": "search",
        "query": "4th grade field trip",
        "message_ids": ["m1", "m2", "m3", "m4", "m5"],
        "final_response": "I found 5 recent emails matching 4th grade field trip",
    }

    class Store:
        def get(self, task_id):
            assert task_id == "task-search"
            return {"task_id": task_id, "status": "done", "result": search_result}

        def get_delegation(self, delegation_id):
            assert delegation_id == "dlg-search"
            return {
                "delegation_id": delegation_id,
                "task_id": "task-search",
                "target_bot_id": "email-bot",
                "executor_prefix": "gmail",
                "status": "queued",
            }

    store = Store()

    def reconcile(_store, rec):
        out = dict(rec)
        out["status"] = "done"
        out["result_summary"] = search_result["final_response"]
        return out

    chat = SimpleNamespace(
        serve=serve,
        _task_store=lambda: store,
        _reconcile_delegation=reconcile,
        _remember_gmail_page=lambda owner, cid, result: remembered.append(
            (owner, cid, dict(result))),
        delegation=SimpleNamespace(public_view=lambda rec: dict(rec)),
    )

    jev = SimpleNamespace(_submit_routed_gmail=lambda *a, **k: (
        True,
        {"delegation_id": "dlg-search", "task_id": "task-search",
         "target_bot_id": "email-bot", "status": "queued"},
    ))
    bridge._installed = False
    bridge.install(chat, jev)

    ok, payload = jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(),
        {"task": "gmail: read 4th grade field trip"},
        {"request_text": "Find the details for the 4th grade field trip"},
    )

    assert ok is True
    assert payload["status"] == "done"
    assert "5 recent emails" in payload["result_summary"]
    assert remembered == [("alice", "conversation-1", search_result)]


def test_same_turn_search_then_read_number_uses_persisted_hit(monkeypatch):
    """Live regression: search result #5 can be read without another user turn."""
    monkeypatch.setattr(bridge, "_GMAIL_FOLLOW_MAX_SECONDS", 1.0)
    state = {"conv": {}}
    calls = []

    search_result = {
        "mode": "search",
        "query": "4th grade field trip",
        "message_ids": ["m1", "m2", "m3", "m4", "field-trip-message"],
        "final_response": "I found 5 recent emails",
    }
    read_result = {
        "mode": "message",
        "selected": {"id": "field-trip-message"},
        "final_response": "Field Trip October 2nd- Please complete form",
    }
    task_results = {
        "task-search": search_result,
        "task-read": read_result,
    }

    class Store:
        def get(self, task_id):
            return {"task_id": task_id, "status": "done", "result": task_results[task_id]}

        def get_delegation(self, delegation_id):
            task_id = "task-search" if delegation_id == "dlg-search" else "task-read"
            return {
                "delegation_id": delegation_id,
                "task_id": task_id,
                "target_bot_id": "email-bot",
                "executor_prefix": "gmail",
                "status": "queued",
            }

    store = Store()

    def remember(owner, cid, result):
        if result.get("mode") == "search":
            state["conv"]["gmail_results"] = list(result["message_ids"])
            state["conv"]["gmail_focus"] = result["query"]
        elif result.get("selected"):
            state["conv"]["gmail_selected"] = dict(result["selected"])

    def select_command(conv, index):
        ids = conv.get("gmail_results") or []
        if index < 1 or index > len(ids):
            return None
        return f'gmail: read id {ids[index - 1]} focus "{conv["gmail_focus"]}"'

    def reconcile(_store, rec):
        out = dict(rec)
        out["status"] = "done"
        result = task_results[out["task_id"]]
        out["result_summary"] = result["final_response"]
        return out

    chat = SimpleNamespace(
        serve=serve,
        _task_store=lambda: store,
        _reconcile_delegation=reconcile,
        _remember_gmail_page=remember,
        get_conversation=lambda owner, cid: state["conv"],
        sync_delegated_work=lambda *a: {"delegations": [], "relayed": []},
        _gmail_select_command=select_command,
        delegation=SimpleNamespace(public_view=lambda rec: dict(rec)),
    )

    def original_submit(chat_arg, dev_bot, session, frame, hint):
        calls.append((dict(frame), dict(hint)))
        if len(calls) == 1:
            return True, {
                "delegation_id": "dlg-search", "task_id": "task-search",
                "target_bot_id": "email-bot", "status": "queued",
            }
        return True, {
            "delegation_id": "dlg-read", "task_id": "task-read",
            "target_bot_id": "email-bot", "status": "queued",
        }

    jev = SimpleNamespace(_submit_routed_gmail=original_submit)
    bridge._installed = False
    bridge.install(chat, jev)

    request = "Find the details for the 4th grade field trip"
    first = jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(),
        {"task": "gmail: read 4th grade field trip"},
        {"request_text": request, "selected_bot_id": "email-bot"},
    )
    assert first[1]["status"] == "done"
    assert state["conv"]["gmail_results"][-1] == "field-trip-message"

    second = jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(),
        {"task": "read number 5", "target_bot_id": "email-bot"},
        {"request_text": request, "selected_bot_id": "email-bot"},
    )

    assert second[1]["status"] == "done"
    assert calls[1][0]["task"] == (
        'gmail: read id field-trip-message focus "4th grade field trip"')
    assert calls[1][1]["request_text"] == ""
    assert state["conv"]["gmail_selected"]["id"] == "field-trip-message"


def test_follow_timeout_keeps_existing_async_view(monkeypatch):
    monkeypatch.setattr(bridge, "_GMAIL_FOLLOW_MAX_SECONDS", 0.0)

    class Store:
        def get(self, task_id):
            return {"task_id": task_id, "status": "queued"}

    submitted = {
        "delegation_id": "dlg-1", "task_id": "task-1",
        "target_bot_id": "email-bot", "status": "queued",
    }
    chat = SimpleNamespace(_task_store=lambda: Store())
    assert bridge.follow_routed_gmail(chat, _session(), (True, submitted)) == (
        True, submitted)


def test_follow_never_waits_on_approval(monkeypatch):
    monkeypatch.setattr(bridge, "_GMAIL_FOLLOW_MAX_SECONDS", 30.0)

    class Store:
        def get(self, task_id):
            return {"task_id": task_id, "status": "awaiting_approval"}

    submitted = {
        "delegation_id": "dlg-1", "task_id": "task-1",
        "target_bot_id": "email-bot", "status": "queued",
    }
    chat = SimpleNamespace(_task_store=lambda: Store())
    assert bridge.follow_routed_gmail(chat, _session(), (True, submitted)) == (
        True, submitted)
