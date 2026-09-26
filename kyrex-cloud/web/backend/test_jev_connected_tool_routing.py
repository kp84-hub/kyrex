"""Focused regressions for Jev-routed owner-scoped connected tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_CLOUD = _HERE.parents[1]
_REPO = _HERE.parents[2]
_ENGINE = _REPO / "kyrex_engine"
for _path in (str(_HERE), str(_CLOUD), str(_ENGINE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import cal_writer  # noqa: E402
import email_event  # noqa: E402
import jev_stream_router  # noqa: E402
import serve  # noqa: E402


class FakeStore:
    def __init__(self):
        self.created = []
        self.records = {}
        self.statuses = []

    def create_delegation(self, **kwargs):
        did = f"d{len(self.created) + 1}"
        self.created.append(dict(kwargs))
        self.records[did] = {"delegation_id": did, **kwargs, "task_id": None}
        return did

    def set_delegation_status(self, delegation_id, status, **kwargs):
        self.statuses.append((delegation_id, status, dict(kwargs)))
        rec = self.records[delegation_id]
        rec["status"] = status
        rec.update(kwargs)

    def get_delegation(self, delegation_id):
        rec = self.records.get(delegation_id)
        return dict(rec) if rec else None


class FakeBots:
    def __init__(self, entries):
        self.entries = entries

    def load_bots(self):
        return dict(self.entries)

    @staticmethod
    def is_running(bot):
        return str((bot or {}).get("status") or "") == "running"


def _target(bot_id="calendar", *, name="Calendar Bot"):
    # Deliberately NO usable Rift/provider: owner-scoped connected tools must
    # not inherit generic repo delegation's workspace requirements.
    return {
        "id": bot_id,
        "name": name,
        "owner": "alice",
        "status": "running",
        "rift": "",
        "policy": {},
    }


def test_shared_tools_claim_calendar_write_only_when_owner_scope_is_live():
    bot = _target("chief", name="Chief of Staff")
    unavailable = SimpleNamespace(
        gmail_route_ready=lambda _bot: True,
        email_calendar_route_ready=lambda _bot: True,
        _calendar_write_available=lambda owner: False,
    )
    assert jev_stream_router._shared_tools(unavailable, bot) == ["gmail_read"]

    available = SimpleNamespace(
        gmail_route_ready=lambda _bot: True,
        email_calendar_route_ready=lambda _bot: True,
        _calendar_write_available=lambda owner: owner == "alice",
    )
    assert jev_stream_router._shared_tools(available, bot) == [
        "gmail_read", "calendar_write"]


def test_non_mail_peer_selection_does_not_coerce_arbitrary_request_into_gmail():
    chat_service = SimpleNamespace(serve=serve)
    request = "Find tomorrow's weather"
    hint = {
        "selected_bot_id": "calendar",
        "selected_bot_name": "Calendar Bot",
        "selected_bot_role": "calendar",
        "selected_bot_description": "Handles calendar work.",
    }
    assert jev_stream_router._gmail_command_for_routed_turn(
        chat_service, request, request, hint=hint) is None


def test_mail_specialist_can_receive_natural_lookup_without_email_noun():
    chat_service = SimpleNamespace(serve=serve)
    request = "Find the details for the 4th grade field trip in October"
    hint = {
        "selected_bot_id": "email-bot",
        "selected_bot_name": "Email Bot",
        "selected_bot_role": "custom",
        "selected_bot_description": "Handles email information lookups.",
    }
    command = jev_stream_router._gmail_command_for_routed_turn(
        chat_service, request, request, hint=hint)
    assert command is not None
    assert command.startswith("gmail: read ")
    assert "4th" in command.lower()
    assert "field" in command.lower()
    assert "trip" in command.lower()


def test_routed_selected_email_calendar_handoff_uses_shared_executor_without_rift():
    target = _target()
    store = FakeStore()
    submitted = []
    facts = {
        "title": "4th Grade Field Trip",
        "date": "2026-10-02",
        "start": "08:30",
        "end": "14:00",
        "all_day": False,
        "location": "Science Museum",
        "missing": [],
        "ambiguous": [],
    }
    conv = {"gmail_selected": {"id": "m1", "facts": facts}}
    delegation = SimpleNamespace(public_view=lambda rec: dict(rec))
    chat_service = SimpleNamespace(
        bots=FakeBots({"calendar": target}),
        delegation=delegation,
        email_event=email_event,
        cal_writer=cal_writer,
        get_conversation=lambda owner, cid: conv,
        _task_store=lambda: store,
    )

    def submit_email_calendar_task(user, bot, task_text, store=None,
                                   conversation_id=None):
        submitted.append({
            "user": user,
            "bot": bot["id"],
            "intent": json.loads(task_text),
            "conversation_id": conversation_id,
        })
        return "t1"

    dev_bot = SimpleNamespace(
        email_calendar_route_ready=lambda bot: True,
        _calendar_write_available=lambda owner: owner == "alice",
        submit_email_calendar_task=submit_email_calendar_task,
    )
    session = SimpleNamespace(delegation_ctx={
        "owner": "alice",
        "bot": {"id": "chief"},
        "conversation_id": "c1",
    })
    frame = {
        "target_bot_id": "calendar",
        "task": "Add the verified field trip to the owner's calendar.",
    }
    hint = {
        "selected_bot_id": "calendar",
        "request_text": "Add that to my calendar",
    }

    ok, result = jev_stream_router._submit_routed_email_calendar(
        chat_service, dev_bot, session, frame, hint)

    assert ok is True
    assert result["delegation_id"] == "d1"
    assert store.created[0]["executor_prefix"] == "email_calendar"
    assert submitted[0]["bot"] == "calendar"
    assert submitted[0]["intent"] == {
        "title": "4th Grade Field Trip",
        "start": "2026-10-02T08:30:00",
        "end": "2026-10-02T14:00:00",
        "all_day": False,
    }
    # The specialist's missing Rift is irrelevant to this owner-scoped path.
    assert target["rift"] == ""


def test_routed_calendar_handoff_fails_closed_when_selected_email_is_incomplete():
    target = _target()
    store = FakeStore()
    facts = {
        "title": "4th Grade Field Trip",
        "date": "2026-10-02",
        "start": None,
        "end": None,
        "all_day": False,
        "missing": ["time"],
        "ambiguous": [],
    }
    chat_service = SimpleNamespace(
        bots=FakeBots({"calendar": target}),
        delegation=SimpleNamespace(public_view=lambda rec: dict(rec)),
        email_event=email_event,
        cal_writer=cal_writer,
        get_conversation=lambda owner, cid: {
            "gmail_selected": {"id": "m1", "facts": facts}},
        _task_store=lambda: store,
    )
    dev_bot = SimpleNamespace(
        email_calendar_route_ready=lambda bot: True,
        _calendar_write_available=lambda owner: True,
        submit_email_calendar_task=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not submit incomplete event")),
    )
    session = SimpleNamespace(delegation_ctx={
        "owner": "alice", "bot": {"id": "chief"}, "conversation_id": "c1"})

    ok, result = jev_stream_router._submit_routed_email_calendar(
        chat_service, dev_bot, session,
        {"target_bot_id": "calendar", "task": "Add it."},
        {"selected_bot_id": "calendar",
         "request_text": "Add that to my calendar"},
    )

    assert ok is False
    assert "start and end time" in result["error"]
    assert store.created == []


def test_peer_route_suppresses_coordinator_direct_shared_tool_shortcuts(monkeypatch):
    """If Jev chose a peer, Chief must not intercept the shared tool itself."""
    monkeypatch.setattr(jev_stream_router, "_installed", False)

    class FakeEngine:
        def _handle_delegation(self, frame):
            return True, dict(frame)

    chief = {"id": "chief", "owner": "alice", "policy": {}}
    peer = _target("email-bot", name="Email Bot")
    bots = FakeBots({"chief": chief, "email-bot": peer})

    async def original_stream(*args, **kwargs):
        yield {"type": "status", "status": "complete", "content": "ok"}

    chat_service = SimpleNamespace(
        stream_chat=original_stream,
        get_conversation=lambda user, cid: {"bot_id": "chief"},
        resolve_bot_for_user=lambda user, bot_id: chief,
        serve=SimpleNamespace(
            DEVELOPER_PRESET={"fs:write": 1},
            is_browser_bot_policy=lambda policy: False,
            coordinator_granted=lambda bot: True,
        ),
        bots=bots,
        delegation=SimpleNamespace(
            visible_targets=lambda owner, exclude_bot_id=None: [],
        ),
        EngineSession=FakeEngine,
    )
    dev_bot = SimpleNamespace(
        is_writable_bot_policy=lambda policy: False,
        browser_route_ready=lambda bot: False,
        gmail_route_ready=lambda bot: True,
        email_calendar_route_ready=lambda bot: True,
        _calendar_write_available=lambda owner: True,
    )

    jev_stream_router.install(chat_service, dev_bot)
    token = jev_stream_router._bot_target_hint.set({
        "coordinator_bot_id": "chief",
        "selected_bot_id": "email-bot",
    })
    try:
        assert dev_bot.gmail_route_ready(chief) is False
        assert dev_bot.email_calendar_route_ready(chief) is False
        # The selected peer still sees the actual owner-scoped readiness.
        assert dev_bot.gmail_route_ready(peer) is True
        assert dev_bot.email_calendar_route_ready(peer) is True
    finally:
        jev_stream_router._bot_target_hint.reset(token)


def test_gmail_detail_guidance_requires_gmail_and_leaves_hit_selection_to_kyrex():
    guidance = jev_stream_router._gmail_detail_followup_guidance({
        "shared_tools": ["gmail_read"],
        "selected_bot_id": "email-bot",
    })
    assert "choose the most relevant message" in guidance
    assert "`gmail: search <topic>`" in guidance
    assert "different shorter relevant query" in guidance
    assert "`read number N`" in guidance
    assert "Do not assume a fixed result position" in guidance
    assert "same result page" in guidance
    assert "remaining plausible hits" in guidance
    assert "until the requested fact is found" in guidance
    assert "5" not in guidance

    assert jev_stream_router._gmail_detail_followup_guidance({
        "shared_tools": [],
    }) == ""


def test_calendar_read_guidance_rejects_unproven_write_only_lookup():
    guidance = jev_stream_router._calendar_read_guidance({
        "shared_tools": ["gmail_read", "calendar_write"],
    })
    assert "has not proven a calendar_read capability" in guidance
    assert "Do not delegate Calendar lookup/search/read" in guidance
    assert "event creation only" in guidance

    assert jev_stream_router._calendar_read_guidance({
        "shared_tools": ["calendar_read"],
    }) == ""
