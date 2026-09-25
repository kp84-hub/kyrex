"""Regression coverage for owner-scoped Gmail delegation.

The live Chief -> email-bot smoke test exposed a layer-order bug: generic
``submit_delegation`` required the target's repo Rift/provider before deciding
that the task was actually an owner-scoped Gmail read. A workspace-free Email
Bot therefore fell into ``repo`` and later died with
``empty --rift requires --repo-url to clone``.
"""

from __future__ import annotations

import os
import sys

import pytest

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import bots  # noqa: E402
import delegation  # noqa: E402
import serve  # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402


COORD_POLICY = {"fs:read": 0, "bot:delegate": 0}


def _chief():
    return {
        "id": "chief",
        "name": "Chief of Staff",
        "owner": "alice",
        "status": "running",
        "policy": COORD_POLICY,
    }


def _email_bot(*, owner="alice", status="running"):
    # Deliberately NO Rift and NO provider profile: neither is authority for an
    # owner-scoped connected-tool read.
    return {
        "id": "email-bot",
        "name": "Email Bot",
        "owner": owner,
        "status": status,
        "rift": "",
        "policy": {},
        "provider_profile_id": "",
    }


def _store(tmp_path):
    return CloudTaskStore(db_path=tmp_path / "delegated-gmail.db")


def _registry(monkeypatch, target):
    monkeypatch.setattr(
        bots, "load_bots", lambda: {"chief": _chief(), target["id"]: target})


def test_explicit_delegated_email_lookup_routes_gmail_without_rift_or_provider(
        tmp_path, monkeypatch):
    target = _email_bot()
    _registry(monkeypatch, target)
    monkeypatch.setattr(
        serve, "_bot_llm_config",
        lambda bot: (_ for _ in ()).throw(
            AssertionError("Gmail delegation must not resolve a Bot provider")),
    )
    store = _store(tmp_path)

    view = delegation.submit_delegation(
        "alice",
        _chief(),
        "email-bot",
        "Search emails for details about the 4th grade field trip in October.",
        store=store,
        parent_conversation_id="conv-1",
    )

    task = store.get(view["task_id"])
    assert view["executor_prefix"] == "gmail"
    assert task["executor_prefix"] == "gmail"
    assert task["task_text"].startswith("gmail: ")
    assert task["repo_url"] in (None, "")
    assert task["rift"] in (None, "")
    assert task["bot_id"] == "email-bot"
    assert task["chat_id"] == "alice"


def test_nounless_field_trip_lookup_uses_mail_specialist_route(
        tmp_path, monkeypatch):
    target = _email_bot()
    _registry(monkeypatch, target)
    store = _store(tmp_path)

    view = delegation.submit_delegation(
        "alice",
        _chief(),
        "email-bot",
        "Find the details for the 4th grade field trip in October",
        store=store,
    )

    task = store.get(view["task_id"])
    assert task["executor_prefix"] == "gmail"
    assert task["task_text"].startswith("gmail: ")
    # The deterministic Gmail parser should carry the user's topical terms.
    assert "4th grade field trip" in task["task_text"].lower()


def test_delegated_gmail_worker_never_enters_repo_executor(
        tmp_path, monkeypatch):
    """Chief -> TaskWorker reaches the in-process Gmail branch with no Rift."""
    target = _email_bot()
    _registry(monkeypatch, target)
    store = _store(tmp_path)

    view = delegation.submit_delegation(
        "alice",
        _chief(),
        "email-bot",
        "Find the details for the 4th grade field trip in October",
        store=store,
        parent_conversation_id="conv-1",
    )
    task = store.get(view["task_id"])
    assert task["executor_prefix"] == "gmail"
    assert not task.get("rift")

    seen = {}

    def fake_gmail(ctx, chat_id, task_text, task_id, send,
                   on_progress=None, on_result=None):
        seen["bot_id"] = getattr(ctx, "bot_id", "")
        seen["owner"] = getattr(ctx, "bot_owner", "")
        seen["task_text"] = task_text
        if on_result is not None:
            on_result({
                "status": "no_changes",
                "mode": "read_query",
                "count": 1,
                "final_response": "4th Grade Field Trip — October 2",
            })

    monkeypatch.setattr(serve, "_run_gmail_read_task", fake_gmail)

    worker = TaskWorker(
        store, worker_id="gmail-w", executor=serve.run_task, max_workers=1)
    worker.execute_task(store.get(view["task_id"]))

    assert seen["bot_id"] == "email-bot"
    assert seen["owner"] == "alice"
    assert seen["task_text"].startswith("gmail: ")
    assert store.status(view["task_id"]) == "done"
    result_events = [
        event for event in store.get_events(view["task_id"])
        if event.get("type") == "result"
    ]
    assert len(result_events) == 1
    assert "October 2" in (
        result_events[0]["payload"] or {}).get("final_response", "")


def test_repo_work_still_requires_repo_eligible_target(tmp_path, monkeypatch):
    target = _email_bot()
    _registry(monkeypatch, target)
    store = _store(tmp_path)

    with pytest.raises(delegation.DelegationError, match="Rift is unavailable"):
        delegation.submit_delegation(
            "alice", _chief(), "email-bot", "Review the parser tests",
            store=store)

    assert store.list_delegations(owner="alice") == []


def test_connected_tool_target_is_still_owner_and_lifecycle_scoped(
        tmp_path, monkeypatch):
    store = _store(tmp_path)

    foreign = _email_bot(owner="bob")
    _registry(monkeypatch, foreign)
    with pytest.raises(delegation.DelegationError, match="not owned by you"):
        delegation.submit_delegation(
            "alice", _chief(), "email-bot", "Find the email about the trip",
            store=store)

    stopped = _email_bot(status="stopped")
    _registry(monkeypatch, stopped)
    with pytest.raises(delegation.DelegationError, match="start it before delegating"):
        delegation.submit_delegation(
            "alice", _chief(), "email-bot", "Find the email about the trip",
            store=store)

    assert store.list_delegations(owner="alice") == []
