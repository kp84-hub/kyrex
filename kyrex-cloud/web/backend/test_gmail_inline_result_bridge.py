"""Regressions for fast routed Gmail results returned inside one Chief turn."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gmail_inline_result_bridge as bridge  # noqa: E402


class Store:
    def __init__(self, task, delegation=None):
        self.task = dict(task)
        self.delegation = dict(delegation or {
            "delegation_id": "d1",
            "task_id": "t1",
            "status": "queued",
            "target_bot_id": "email-bot",
            "executor_prefix": "gmail",
        })
        self.relayed = []

    def get(self, task_id):
        assert task_id == "t1"
        return dict(self.task)

    def get_delegation(self, delegation_id):
        assert delegation_id == "d1"
        return dict(self.delegation)

    def mark_delegation_relayed(self, delegation_id):
        self.relayed.append(delegation_id)
        self.delegation["relayed_at"] = "now"
        return True


def _session():
    return SimpleNamespace(delegation_ctx={
        "owner": "alice",
        "conversation_id": "conversation-1",
    })


def _chat(store, remembered, reconciled):
    def reconcile(_store, rec):
        reconciled.append(dict(rec))
        rec = dict(rec)
        rec["status"] = str(store.task.get("status") or "")
        if rec["status"] == "done":
            rec["result_summary"] = (
                'I found 5 recent emails matching "4th grade field trip":\n'
                '5. Field Trip October 2nd- Please complete form')
        elif rec["status"] == "failed":
            rec["error"] = "gmail failed"
        store.delegation = dict(rec)
        return rec

    return SimpleNamespace(
        _task_store=lambda: store,
        _remember_gmail_page=lambda owner, cid, result: remembered.append(
            (owner, cid, dict(result))),
        _reconcile_delegation=reconcile,
        delegation=SimpleNamespace(public_view=lambda rec: dict(rec)),
    )


def test_completed_search_is_returned_inline_and_persists_page(monkeypatch):
    monkeypatch.setattr(bridge, "INLINE_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(bridge, "INLINE_POLL_SECONDS", 0.001)
    result = {
        "mode": "search",
        "query": "4th grade field trip",
        "message_ids": ["m1", "m2", "m3", "m4", "m5"],
        "next_page_token": "",
    }
    store = Store({"task_id": "t1", "status": "done", "result": result})
    remembered, reconciled = [], []
    chat = _chat(store, remembered, reconciled)

    jev = SimpleNamespace(
        _submit_routed_gmail=lambda *a, **k: (
            True, {"delegation_id": "d1", "task_id": "t1", "status": "queued"}))
    monkeypatch.setattr(bridge, "_installed", False)
    bridge.install(chat, jev)

    ok, payload = jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(), {"task": "gmail: read field trip"}, {})

    assert ok is True
    assert payload["status"] == "done"
    assert "Field Trip October 2nd" in payload["result_summary"]
    assert remembered == [("alice", "conversation-1", result)]
    assert reconciled and reconciled[0]["delegation_id"] == "d1"
    assert store.relayed == ["d1"]


def test_completed_message_read_persists_selected_email_state(monkeypatch):
    monkeypatch.setattr(bridge, "INLINE_WAIT_SECONDS", 0.05)
    selected = {
        "id": "m5",
        "headers": {"Subject": "Field Trip October 2nd- Please complete form"},
        "body": "Trip details",
    }
    result = {"mode": "read", "selected": selected}
    store = Store({"task_id": "t1", "status": "done", "result": result})
    remembered, reconciled = [], []
    chat = _chat(store, remembered, reconciled)

    jev = SimpleNamespace(
        _submit_routed_gmail=lambda *a, **k: (
            True, {"delegation_id": "d1", "task_id": "t1", "status": "queued"}))
    monkeypatch.setattr(bridge, "_installed", False)
    bridge.install(chat, jev)
    ok, payload = jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(), {"task": "read number 5"}, {})

    assert ok is True
    assert payload["status"] == "done"
    assert remembered == [("alice", "conversation-1", result)]
    assert store.relayed == ["d1"]


def test_timeout_preserves_original_queued_result(monkeypatch):
    monkeypatch.setattr(bridge, "INLINE_WAIT_SECONDS", 0.0)
    store = Store({"task_id": "t1", "status": "queued", "result": None})
    remembered, reconciled = [], []
    chat = _chat(store, remembered, reconciled)
    queued = {"delegation_id": "d1", "task_id": "t1", "status": "queued"}
    jev = SimpleNamespace(_submit_routed_gmail=lambda *a, **k: (True, dict(queued)))
    monkeypatch.setattr(bridge, "_installed", False)
    bridge.install(chat, jev)

    assert jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(), {"task": "x"}, {}) == (True, queued)
    assert remembered == []
    assert reconciled == []
    assert store.relayed == []


def test_failed_task_returns_terminal_safe_view_for_kyrex_recovery(monkeypatch):
    monkeypatch.setattr(bridge, "INLINE_WAIT_SECONDS", 0.05)
    store = Store({"task_id": "t1", "status": "failed", "result": {}})
    remembered, reconciled = [], []
    chat = _chat(store, remembered, reconciled)
    jev = SimpleNamespace(
        _submit_routed_gmail=lambda *a, **k: (
            True, {"delegation_id": "d1", "task_id": "t1", "status": "queued"}))
    monkeypatch.setattr(bridge, "_installed", False)
    bridge.install(chat, jev)

    ok, payload = jev._submit_routed_gmail(
        chat, SimpleNamespace(), _session(), {"task": "x"}, {})

    assert ok is True
    assert payload["status"] == "failed"
    assert payload["error"] == "gmail failed"
    assert remembered == []
    assert store.relayed == ["d1"]


def test_non_gmail_or_rejected_submitter_outcome_is_unchanged(monkeypatch):
    chat = SimpleNamespace()
    outcomes = [None, (False, {"error": "denied"})]
    for expected in outcomes:
        jev = SimpleNamespace(_submit_routed_gmail=lambda *a, _x=expected, **k: _x)
        monkeypatch.setattr(bridge, "_installed", False)
        bridge.install(chat, jev)
        assert jev._submit_routed_gmail(
            chat, SimpleNamespace(), _session(), {"task": "x"}, {}) == expected


def test_read_evidence_includes_facts_beyond_summary_without_extra_fields(monkeypatch):
    body = "Newsletter filler " * 400 + "Homecoming game: Friday October 9, 2026 at 7:00 PM."
    selected = {"headers": {"Subject": "Homecoming announcement", "From": "School", "Date": "Sep 9", "Secret": "hidden"}, "body": body, "truncated": False, "credential": "hidden"}
    store = Store({"task_id": "t1", "status": "done", "result": {"selected": selected}})
    payload = bridge._terminal_public_view(_chat(store, [], []), _session(), {"task_id": "t1", "delegation_id": "d1"})
    assert "7:00 PM" in payload["email_evidence"]["body"]
    assert payload["email_evidence"]["untrusted_data"] is True
    assert "hidden" not in str(payload["email_evidence"])


def test_duplicate_check_reuses_same_result_only_within_turn(monkeypatch):
    store = Store({"task_id": "t1", "status": "done", "result": {"mode": "search", "query": "homecoming", "message_ids": []}})
    calls = []
    def submit(*args):
        calls.append(args)
        return True, {"delegation_id": "d1", "task_id": "t1", "status": "queued"}
    jev = SimpleNamespace(_submit_routed_gmail=submit)
    monkeypatch.setattr(bridge, "_installed", False)
    chat = _chat(store, [], [])
    bridge.install(chat, jev)
    session = _session(); session._gmail_inline_cache = {}
    frame = {"target_bot_id": "email-bot", "task": "gmail: search homecoming"}
    one = jev._submit_routed_gmail(chat, None, session, frame, {})
    two = jev._submit_routed_gmail(chat, None, session, frame, {})
    assert len(calls) == 1
    assert two[1]["already_checked"] is True
    assert one[1]["task_id"] == two[1]["task_id"]
    session._gmail_inline_cache = {}
    jev._submit_routed_gmail(chat, None, session, frame, {})
    assert len(calls) == 2


def test_numbered_read_cache_is_scoped_to_the_current_search_page(monkeypatch):
    store = Store({"task_id": "t1", "status": "done", "result": {"mode": "read"}})
    calls = []
    def submit(*args):
        calls.append(args)
        return True, {"delegation_id": "d1", "task_id": "t1", "status": "queued"}
    jev = SimpleNamespace(_submit_routed_gmail=submit)
    chat = _chat(store, [], [])
    page = {"gmail_results": ["first-email"]}
    chat.get_conversation = lambda *args: page
    monkeypatch.setattr(bridge, "_installed", False)
    bridge.install(chat, jev)
    session = _session(); session._gmail_inline_cache = {}
    frame = {"target_bot_id": "email-bot", "task": "read number 1"}
    jev._submit_routed_gmail(chat, None, session, frame, {})
    jev._submit_routed_gmail(chat, None, session, frame, {})
    assert len(calls) == 1
    page["gmail_results"] = ["different-email"]
    jev._submit_routed_gmail(chat, None, session, frame, {})
    assert len(calls) == 2


def test_same_message_on_different_pages_reuses_body_without_changing_page(monkeypatch):
    store = Store({'task_id': 't1', 'status': 'done', 'result': {'mode': 'read', 'selected': {'body': 'Kickoff fact'}}})
    calls = []
    page = {'gmail_results': ['same-email', 'other-email']}
    chat = _chat(store, [], [])
    chat.get_conversation = lambda *a: page
    jev = SimpleNamespace(_submit_routed_gmail=lambda *a: (calls.append(a) or (True, {'task_id': 't1', 'delegation_id': 'd1'})),
                          _gmail_command_for_routed_turn=lambda *a, **k: 'gmail: read id same-email')
    import gmail_continuation_bridge
    monkeypatch.setattr(gmail_continuation_bridge, 'resolve_continuation', lambda *a: None)
    monkeypatch.setattr(bridge, '_installed', False)
    bridge.install(chat, jev)
    session = _session(); session._gmail_inline_cache = {}
    jev._submit_routed_gmail(chat, None, session, {'task': 'read number 1'}, {})
    page['gmail_results'] = ['new-email', 'same-email']
    result = jev._submit_routed_gmail(chat, None, session, {'task': 'read number 2'}, {})
    assert len(calls) == 1
    assert result[1]['already_checked']
    assert result[1]['email_evidence']['body'] == 'Kickoff fact'
    assert page['gmail_results'] == ['new-email', 'same-email']


def test_lookup_budget_blocks_new_search_and_resets_next_turn(monkeypatch):
    store = Store({'task_id': 't1', 'status': 'done', 'result': {'mode': 'search'}})
    calls = []
    chat = _chat(store, [], [])
    jev = SimpleNamespace(_submit_routed_gmail=lambda *a: (calls.append(a) or (True, {'task_id': 't1', 'delegation_id': 'd1'})),
                          _gmail_command_for_routed_turn=lambda chat, task, *a, **k: task)
    import gmail_continuation_bridge
    monkeypatch.setattr(gmail_continuation_bridge, 'resolve_continuation', lambda *a: None)
    monkeypatch.setattr(bridge, '_installed', False)
    monkeypatch.setattr(bridge, 'MAX_CHECKS_PER_TURN', 2)
    bridge.install(chat, jev)
    session = _session(); session._gmail_inline_cache = {}
    for topic in ('first', 'second'):
        jev._submit_routed_gmail(chat, None, session, {'task': 'gmail: search ' + topic}, {})
    ok, result = jev._submit_routed_gmail(chat, None, session, {'task': 'gmail: search third'}, {})
    assert not ok and result['lookup_limit_reached']
    assert len(calls) == 2
    session._gmail_inline_cache = {}
    assert jev._submit_routed_gmail(chat, None, session, {'task': 'gmail: search third'}, {})[0]
    assert len(calls) == 3


def test_inline_focus_cannot_leak_whole_body_through_summary():
    selected = {"headers": {"Subject": "Newsletter"},
                "body": "unrelated-private-medical-details. Field trip at 9 AM",
                "focus_section": "Field trip at 9 AM"}
    store = Store({"task_id": "t1", "status": "done", "result": {"selected": selected}})
    store.delegation["result_summary"] = "unrelated-private-medical-details"
    view = bridge._terminal_public_view(_chat(store, [], []), _session(),
                                        {"task_id": "t1", "delegation_id": "d1"})
    assert view["email_evidence"]["body"] == "Field trip at 9 AM"
    assert "unrelated-private-medical-details" not in str(view)
    assert store.task["result"]["selected"]["body"].startswith("unrelated")
