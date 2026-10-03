"""Replay actual owner requests through the production mail compiler.

These offline regressions cover host routing/evidence, not live model quality
or successful OAuth. Run alongside the provider/stream and Browser tests.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import chat_service
import mail_routing_bridge as mail
import serve

CASES = json.loads(Path(__file__).with_name("fixtures").joinpath("chat_quality_requests.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_owner_requests_compile_to_bounded_mail_steps(case, monkeypatch):
    monkeypatch.setattr(serve, "_gmail_query_from", lambda text: mail.full_gmail_query(serve, text))
    conv = {"gmail_results": ["fixture-oct-1", "fixture-may-1"], "messages": [{
        "role": "assistant", "content":
        "1. Field Trip reminders for tomorrow — Thu, 01 Oct 2026 18:58:59 +0000\n"
        "2. Zoo Field Trip Information — Thu, 01 May 2025 12:02:07 +0000"}]}
    chat = SimpleNamespace(serve=serve,
        get_conversation=lambda owner, cid: conv if case.get("history") else {},
        _gmail_results_state=chat_service._gmail_results_state,
        _gmail_select_command=chat_service._gmail_select_command)
    hint = {"owner": "alice", "conversation_id": "c1", "selected_bot_name": "Email Bot"}
    command = mail.bounded_gmail_command(chat, case["task"], case["request"], hint=hint)
    assert command == case["expected"]
    if command:
        assert serve.canonical_gmail_task(command) == command


@pytest.mark.parametrize("owner_request,expected", [
    ("What is on my calendar today", "calendar: today"),
    ("What is on my calendar for Friday", "calendar: friday"),
])
def test_owner_calendar_requests_stay_reads(owner_request, expected):
    assert serve.natural_calendar_command(owner_request) == expected


def test_later_instructions_do_not_replace_the_email_topic():
    request = "Use the email about today’s field trip. Open its Google Form link and tell me the exact location and arrival time."
    query = mail.full_gmail_query(serve, request)
    assert "field trip" in query
    assert "Open" not in query and "location" not in query and "arrival" not in query
    assert mail.mail_topic_request('Read the email titled "Trip. Reminders". Open the link.') == 'Read the email titled "Trip. Reminders"'
