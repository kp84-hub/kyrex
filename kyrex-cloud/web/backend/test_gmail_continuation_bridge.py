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
