"""Chief of Staff's conversational entry point for exact-sender rules."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import automation_rules
import email_automation_chat as email_chat


@pytest.mark.parametrize("text", [
    "Watch emails from Someone@Example.com",
    "Please monitor for email from someone@example.com",
    "Notify me when emails arrive from someone@example.com",
    "Add an email sender someone@example.com",
])
def test_explicit_email_rule_phrases_are_recognized(text):
    assert email_chat.is_rule_request(text)
    assert email_chat.parse_sender(text) == "someone@example.com"


@pytest.mark.parametrize("text", [
    "What is my latest email?", "Search Gmail for receipts", "watch emails",
    "Watch emails from one@example.com and two@example.com",
])
def test_searches_and_ambiguous_sender_requests_do_not_create_rules(text):
    if "watch emails" == text:
        assert email_chat.is_rule_request(text)
        with pytest.raises(email_chat.EmailRuleRequestError):
            email_chat.parse_sender(text)
    elif "Watch emails from" in text:
        with pytest.raises(email_chat.EmailRuleRequestError):
            email_chat.parse_sender(text)
    else:
        assert not email_chat.is_rule_request(text)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KYREX_AUTOMATION_ENABLED", "1")
    monkeypatch.setenv("KYREX_AUTOMATION_OWNER", "alice")
    monkeypatch.setenv("KYREX_AUTOMATION_TOKEN", "t" * 40)
    bot = {"id": "email", "name": "Email Bot", "owner": "alice",
           "status": "running"}
    monkeypatch.setattr(email_chat.bots, "load_bots", lambda: {"email": bot})
    monkeypatch.setattr(email_chat.connectors, "default_store", lambda:
                        SimpleNamespace(gmail_read_available=lambda owner: True))
    conversation = {"conversation_id": "a" * 32, "bot_id": "email",
                   "title": "Email Bot · Existing chat"}
    monkeypatch.setattr("chat_service.list_conversations", lambda owner: [conversation])
    monkeypatch.setattr("chat_service.get_conversation", lambda owner, cid:
                        {**conversation} if owner == "alice" and cid == "a" * 32 else None)
    return conversation


def test_add_rule_targets_existing_email_chat_and_is_idempotent(setup):
    first, title = email_chat.add_rule("alice", "Store@Example.com")
    second, second_title = email_chat.add_rule("alice", "store@example.com")
    assert title == second_title == "Email Bot · Existing chat"
    assert first["rule_id"] == second["rule_id"]
    assert len(automation_rules.list_rules("alice")) == 1
    assert first["sender"] == "store@example.com"
    assert first["conversation_id"] == "a" * 32


def test_gmail_read_scope_is_required(setup, monkeypatch):
    monkeypatch.setattr(email_chat.connectors, "default_store", lambda:
                        SimpleNamespace(gmail_read_available=lambda owner: False))
    with pytest.raises(email_chat.EmailRuleRequestError, match="Connect Gmail"):
        email_chat.add_rule("alice", "store@example.com")
    assert automation_rules.list_rules("alice") == []


def test_existing_email_bot_chat_is_required(setup, monkeypatch):
    monkeypatch.setattr("chat_service.list_conversations", lambda owner: [])
    with pytest.raises(email_chat.EmailRuleRequestError, match="existing Email Bot chat"):
        email_chat.add_rule("alice", "store@example.com")
    assert automation_rules.list_rules("alice") == []


def test_rule_is_scoped_to_authenticated_owner(setup):
    with pytest.raises(email_chat.EmailRuleRequestError, match="exactly one running Email Bot"):
        email_chat.add_rule("mallory", "store@example.com")
    assert automation_rules.list_rules("mallory") == []
