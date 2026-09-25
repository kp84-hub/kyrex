"""Regression coverage for owner-scoped Calendar tools shared across Bots.

The stored Bot policy may still describe an old routing specialization. It is
NOT connector authority. Calendar read/create/delete availability comes from
the owner's Google connection; each task receives only its required operation
policy in a task-local context, while the provider connector and approval gates
remain authoritative.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bots  # noqa: E402
import chat_service  # noqa: E402
import connectors  # noqa: E402
import delegation  # noqa: E402
import dev_bot  # noqa: E402
import serve  # noqa: E402
import shared_connected_tools as shared  # noqa: E402
from task_store import CloudTaskStore  # noqa: E402

OWNER = "alice"


class _ConnectorStore:
    def __init__(self, read=True, write=True):
        self.read = read
        self.write = write

    def calendar_read_available(self, owner):
        assert owner == OWNER
        return self.read

    def scope_granted(self, owner, scope, provider="google"):
        assert owner == OWNER
        return self.read and scope == connectors.GOOGLE_CALENDAR_READ_SCOPE

    def calendar_write_available(self, owner):
        assert owner == OWNER
        return self.write


class _RecordingStore:
    def __init__(self, tmp_path):
        self.raw = CloudTaskStore(db_path=tmp_path / "shared-calendar.db")
        self.submissions = []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        return self.raw.submit(**kwargs)

    def __getattr__(self, name):
        return getattr(self.raw, name)


ROLE_POLICIES = {
    "email": lambda: {},
    "developer": serve.developer_preset_policy,
    "calendar": serve.calendar_preset_policy,
    "browser": serve.browser_preset_policy,
    "chief": serve.coordinator_preset_policy,
}


def _bot(role):
    return {
        "id": f"bot-{role}",
        "name": f"{role.title()} Bot",
        "owner": OWNER,
        "status": "running",
        "rift": "",
        "policy": ROLE_POLICIES[role](),
    }


@pytest.fixture(autouse=True)
def _connected(monkeypatch):
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _ConnectorStore(read=True, write=True))
    # The production bootstrap calls this before Jev. Calling it here is
    # idempotent and makes this file independently runnable.
    shared.install(chat_service, dev_bot)


@pytest.mark.parametrize("role", list(ROLE_POLICIES))
def test_every_running_bot_can_route_owner_calendar_read(role):
    assert dev_bot.calendar_route_ready(_bot(role)) is True


@pytest.mark.parametrize("role", list(ROLE_POLICIES))
def test_every_running_bot_submits_same_calendar_read(tmp_path, role):
    store = _RecordingStore(tmp_path)
    bot = _bot(role)
    dev_bot.submit_calendar_task(
        OWNER, bot, "calendar: week", store=store, conversation_id="conv")
    task = store.submissions[-1]
    assert task["executor_prefix"] == "calendar"
    assert task["repo_url"] is None
    assert task["bot_id"] == bot["id"]


@pytest.mark.parametrize("role", list(ROLE_POLICIES))
def test_every_running_bot_can_submit_owner_calendar_create(tmp_path, role):
    store = _RecordingStore(tmp_path)
    bot = _bot(role)
    dev_bot.submit_calendar_writer_task(
        OWNER, bot,
        '{"title":"Dentist","start":"2026-10-02T09:00:00",'
        '"end":"2026-10-02T09:30:00","all_day":false}',
        store=store, conversation_id="conv")
    task = store.submissions[-1]
    assert task["executor_prefix"] == "cal_write"
    assert task["repo_url"] is None


@pytest.mark.parametrize("role", list(ROLE_POLICIES))
def test_every_running_bot_can_submit_owner_calendar_delete(tmp_path, role):
    store = _RecordingStore(tmp_path)
    bot = _bot(role)
    dev_bot.submit_calendar_editor_task(
        OWNER, bot, '{"id":"event-123"}', store=store,
        conversation_id="conv")
    task = store.submissions[-1]
    assert task["executor_prefix"] == "cal_edit"
    assert task["repo_url"] is None


def test_chief_delegates_calendar_read_to_email_bot_without_rift(
        tmp_path, monkeypatch):
    """Connected Calendar classification happens before repo eligibility."""
    coordinator = _bot("chief")
    target = _bot("email")
    # Deliberately no Rift and no provider profile on the Email Bot. Those are
    # repo requirements, not owner-connected Calendar requirements.
    monkeypatch.setattr(
        bots, "load_bots",
        lambda: {coordinator["id"]: coordinator, target["id"]: target})
    store = _RecordingStore(tmp_path)

    delegation.submit_delegation(
        OWNER, coordinator, target["id"],
        "what is on my calendar tomorrow?",
        store=store, parent_conversation_id="conv")

    task = store.submissions[-1]
    assert task["executor_prefix"] == "calendar"
    assert task["task_text"] == "calendar: tomorrow"
    assert task["repo_url"] is None
    assert task["rift"] == ""


def test_chief_delegates_calendar_create_to_email_bot_without_rift(
        tmp_path, monkeypatch):
    """A shared Calendar write stays on cal_write and keeps its approval path."""
    coordinator = _bot("chief")
    target = _bot("email")
    monkeypatch.setattr(
        bots, "load_bots",
        lambda: {coordinator["id"]: coordinator, target["id"]: target})
    store = _RecordingStore(tmp_path)

    delegation.submit_delegation(
        OWNER, coordinator, target["id"],
        'create a calendar event titled "Dentist" on 2026-10-02 '
        'at 09:00 for 30 minutes',
        store=store, parent_conversation_id="conv")

    task = store.submissions[-1]
    assert task["executor_prefix"] == "cal_write"
    assert task["repo_url"] is None
    intent = json.loads(task["task_text"])
    assert intent == {
        "all_day": False,
        "end": "2026-10-02T09:30:00",
        "start": "2026-10-02T09:00:00",
        "title": "Dentist",
    }


def test_connected_context_overlays_only_required_calendar_operation(
        monkeypatch):
    # Resolve a real registry Bot so build_context carries owner identity.
    bot = _bot("developer")
    monkeypatch.setattr(bots, "load_bots", lambda: {bot["id"]: bot})

    read_ctx = serve.build_context(bot["id"], "calendar")
    create_ctx = serve.build_context(bot["id"], "cal_write")
    delete_ctx = serve.build_context(bot["id"], "cal_edit")
    repo_ctx = serve.build_context(bot["id"], "repo")

    assert serve.cal_list_granted(read_ctx.policy) is True
    assert serve.calendar_writer_granted(create_ctx.policy) is True
    assert serve.calendar_editor_granted(delete_ctx.policy) is True
    # The stored/repo policy is not widened by connection availability.
    assert serve.cal_list_granted(repo_ctx.policy) is False
    assert serve.calendar_writer_granted(repo_ctx.policy) is False
    assert serve.calendar_editor_granted(repo_ctx.policy) is False


def test_shared_delete_classifier_does_not_steal_file_delete():
    assert shared._calendar_delete_request("delete this file") is False
    assert shared._calendar_delete_request("delete calendar event Dentist") is True
    assert shared._calendar_delete_request("cancel my dentist appointment") is True


def test_shared_create_classifier_does_not_steal_developer_create():
    assert shared._calendar_create_request("create a parser file") is False
    assert shared._calendar_create_request("create a calendar event for Friday") is True
    assert shared._calendar_create_request("schedule dentist for Friday") is True


def test_calendar_routes_fail_closed_without_owner_scope(monkeypatch):
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _ConnectorStore(read=False, write=False))
    bot = _bot("email")
    assert dev_bot.calendar_route_ready(bot) is False
    assert dev_bot.calendar_editor_route_ready(bot) is False
    with pytest.raises(dev_bot.DevBotError, match="not connected"):
        dev_bot.submit_calendar_task(OWNER, bot, "calendar: today",
                                     store=SimpleNamespace())
