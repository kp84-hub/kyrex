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
