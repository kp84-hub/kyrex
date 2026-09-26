"""Regressions for Chief -> mail-specialist connected-tool handoff."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import mail_routing_bridge as bridge  # noqa: E402
import serve  # noqa: E402


def _chat():
    return SimpleNamespace(serve=serve)


def _email_hint(request):
    return {
        "coordinator_bot_id": "chief",
        "selected_bot_id": "email-bot",
        "selected_bot_name": "Email Bot",
        "selected_bot_role": "email",
        "selected_bot_description": "Handles email information lookups.",
        "request_text": request,
    }


def test_nounless_lookup_uses_gmail_only_after_mail_specialist_route():
    request = "Find the details for the 4th grade field trip"
    command = bridge.bounded_gmail_command(
        _chat(), request, request, hint=_email_hint(request))
    assert command is not None
    assert command.startswith("gmail: read ")
    assert "4th grade field trip" in command.lower()


def test_original_request_beats_model_authored_canonical_gmail_task():
    request = "Find the details for the 4th grade field trip"
    task = (
        'gmail: search the "4th grade field trip". Look for any recent '
        "email(s) mentioning the 4th grade (or fourth grade) field trip — "
        "including date, time, location, cost, permission slip, chaperone "
        "info, and any deadline"
    )
    command = bridge.bounded_gmail_command(
        _chat(), task, request, hint=_email_hint(request))
    assert command is not None
    assert command.startswith("gmail: read ")
    assert "4th grade field trip" in command.lower()
    assert "look for" not in command.lower()
    assert "permission" not in command.lower()
    assert "chaperone" not in command.lower()


def test_compact_grounded_search_can_broaden_a_no_match_query():
    request = (
        "Find the details for the 4th grade field trip, including its "
        "location and form deadline."
    )
    first = bridge.bounded_gmail_command(
        _chat(), "gmail: search 4th grade field trip",
        request, hint=_email_hint(request))
    retry = bridge.bounded_gmail_command(
        _chat(), "gmail: search field trip",
        request, hint=_email_hint(request))
    assert first == "gmail: search 4th grade field trip"
    assert retry == "gmail: search field trip"
    assert first != retry


def test_unrelated_or_verbose_model_search_uses_original_request():
    request = "Find the details for the 4th grade field trip"
    original = bridge.bounded_gmail_command(
        _chat(), "", request, hint=_email_hint(request))
    for task in (
        "gmail: search payroll",
        'gmail: search "4th grade field trip". Look for location and deadline',
    ):
        assert bridge.bounded_gmail_command(
            _chat(), task, request, hint=_email_hint(request)) == original


def test_exact_user_message_selection_is_not_replaced_by_model_search():
    request = "Read my email id m12345678"
    assert bridge.bounded_gmail_command(
        _chat(), "gmail: search email",
        request, hint=_email_hint(request)) == "gmail: read id m12345678"


def test_model_task_cannot_invent_gmail_for_non_mail_original_request():
    request = "Review the parser tests"
    task = "gmail: search parser tests"
    assert bridge.bounded_gmail_command(
        _chat(), task, request, hint=_email_hint(request)) is None


def test_nounless_lookup_is_not_coerced_for_non_mail_target():
    request = "Find the details for the 4th grade field trip"
    hint = dict(_email_hint(request))
    hint.update({
        "selected_bot_id": "calendar",
        "selected_bot_name": "Calendar Bot",
        "selected_bot_role": "calendar",
        "selected_bot_description": "Scheduling specialist.",
    })
    assert bridge.bounded_gmail_command(
        _chat(), request, request, hint=hint) is None


def test_mail_specialist_does_not_turn_arbitrary_work_into_gmail():
    request = "Review the parser tests"
    assert bridge.bounded_gmail_command(
        _chat(), request, request, hint=_email_hint(request)) is None


def test_mail_mutation_never_becomes_read():
    request = "Delete the 4th grade field trip email"
    assert bridge.bounded_gmail_command(
        _chat(), request, request, hint=_email_hint(request)) is None


def test_query_keeps_context_before_grammatical_for_clause():
    request = (
        "check any wake christian emails with a mention of a field trip "
        "for 4th grade"
    )
    assert bridge.full_gmail_query(serve, request) == (
        "wake christian field trip 4th grade")


def test_normal_search_for_form_still_reduces_to_topic():
    assert bridge.full_gmail_query(
        serve, "search my email for Tesla") == "Tesla"


def test_chief_kept_turn_reuses_original_request_for_reasoned_email_subtask():
    request = "Find the details for the 4th grade field trip"
    session = SimpleNamespace(
        _jev_bot_target_hint={
            "coordinator_bot_id": "chief",
            "selected_bot_id": "chief",
            "request_text": request,
        })
    frame = {
        "target_bot_id": "email-bot",
        # Deliberately model-rephrased: this text alone would not prove Gmail.
        "task": "Investigate the field trip details using available sources",
    }
    seen = {}

    def fallback_hint(chat_service, sess, frm, hint):
        out = dict(hint)
        out.update({
            "selected_bot_id": frm["target_bot_id"],
            "selected_bot_name": "Email Bot",
            "selected_bot_role": "email",
            "selected_bot_description": "Handles email information lookups.",
        })
        return out

    def routed_calendar(*args, **kwargs):
        return None

    def routed_gmail(chat_service, dev_bot, sess, frm, hint):
        seen["hint"] = dict(hint)
        seen["frame"] = dict(frm)
        return True, {"executor_prefix": "gmail"}

    jev = SimpleNamespace(
        _fallback_hint_for_frame=fallback_hint,
        _submit_routed_email_calendar=routed_calendar,
        _submit_routed_gmail=routed_gmail,
    )

    result = bridge._reasoned_connected_delegate(
        SimpleNamespace(), SimpleNamespace(), jev, session, frame)

    assert result == (True, {"executor_prefix": "gmail"})
    assert seen["hint"]["request_text"] == request
    assert seen["hint"]["selected_bot_id"] == "email-bot"


def test_initial_peer_route_is_left_to_existing_jev_wrapper():
    session = SimpleNamespace(
        _jev_bot_target_hint={
            "coordinator_bot_id": "chief",
            "selected_bot_id": "email-bot",
            "request_text": "Find the field trip",
        })
    jev = SimpleNamespace(
        _fallback_hint_for_frame=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not build fallback hint for initial Jev peer")),
        _submit_routed_email_calendar=lambda *a, **k: None,
        _submit_routed_gmail=lambda *a, **k: None,
    )
    assert bridge._reasoned_connected_delegate(
        SimpleNamespace(), SimpleNamespace(), jev, session,
        {"target_bot_id": "email-bot", "task": "x"}) is None


def test_date_only_email_event_defaults_to_all_day_proposal():
    facts = {
        "title": "4th Grade Field Trip - NCSU Agroecology Farm & Howling Cow",
        "date": "2026-10-02",
        "start": None,
        "end": None,
        "all_day": False,
        "date_ambiguous": False,
        "time_ambiguous": False,
        "missing": ["time"],
        "needs": ["time"],
    }
    out = bridge.calendar_handoff_facts(facts)
    assert out["all_day"] is True
    assert "time" not in out["missing"]
    assert "time" not in out["needs"]
    # Never mutate the persisted Gmail facts just to propose a Calendar event.
    assert facts["all_day"] is False


def test_partial_or_ambiguous_time_never_defaults_to_all_day():
    partial = {
        "title": "Field Trip", "date": "2026-10-02",
        "start": "09:00", "end": None, "all_day": False,
        "date_ambiguous": False, "time_ambiguous": False,
    }
    conflict = {
        "title": "Field Trip", "date": "2026-10-02",
        "start": None, "end": None, "all_day": False,
        "date_ambiguous": False, "time_ambiguous": True,
    }
    assert bridge.calendar_handoff_facts(partial)["all_day"] is False
    assert bridge.calendar_handoff_facts(conflict)["all_day"] is False


def test_only_newest_completed_gmail_delegation_updates_selection():
    sync = {
        "delegations": [
            {"delegation_id": "new-calendar", "status": "done",
             "executor_prefix": "calendar"},
            {"delegation_id": "new-gmail", "status": "done",
             "executor_prefix": "gmail"},
            {"delegation_id": "old-gmail", "status": "done",
             "executor_prefix": "gmail"},
        ],
        "relayed": [],
    }
    narrowed = bridge.latest_completed_gmail_sync(sync)
    assert [row["delegation_id"] for row in narrowed["delegations"]] == [
        "new-gmail"]
    # The original owner-facing sync response is never mutated.
    assert len(sync["delegations"]) == 3


def test_new_turn_syncs_completed_delegated_gmail_before_routing(monkeypatch):
    """Immediate follow-up cannot race the UI's Delegated Work poll."""
    events = []

    class Engine:
        def _handle_delegation(self, frame):
            return True, frame

    async def original_stream(user, conversation_id, user_content, **kwargs):
        events.append(("stream", user, conversation_id, user_content))
        yield {"type": "status", "status": "complete", "content": "ok"}

    def sync(user, conversation_id):
        # In production Jev's sync wrapper persists a completed delegated Gmail
        # result into gmail_selected here. Ordering is the invariant under test.
        events.append(("sync", user, conversation_id))
        return {"delegations": [], "relayed": []}

    chat = SimpleNamespace(
        EngineSession=Engine,
        stream_chat=original_stream,
        sync_delegated_work=sync,
    )
    jev = SimpleNamespace(
        _gmail_command_for_routed_turn=lambda *a, **k: None,
        _fallback_hint_for_frame=lambda *a, **k: {},
        _submit_routed_email_calendar=lambda *a, **k: None,
        _submit_routed_gmail=lambda *a, **k: None,
    )
    fake_serve = SimpleNamespace(_gmail_query_from=lambda text: text)

    monkeypatch.setattr(bridge, "_installed", False)
    bridge.install(chat, SimpleNamespace(), jev, fake_serve)

    async def consume():
        return [frame async for frame in chat.stream_chat(
            "alice", "conversation-1", "add that to my calendar")]

    frames = asyncio.run(consume())
    assert frames[-1]["status"] == "complete"
    assert events == [
        ("sync", "alice", "conversation-1"),
        ("stream", "alice", "conversation-1", "add that to my calendar"),
    ]
