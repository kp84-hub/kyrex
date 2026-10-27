"""The Overwatcher's conversational entry point for exact-sender rules."""
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
    "Notify me whenever I get an email from someone@example.com",
    "Can you alert me when I receive mail from someone@example.com?",
    "Set up an email alert from someone@example.com",
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
                        SimpleNamespace(gmail_read_available=lambda owner, *, legacy=False: legacy))
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
                        SimpleNamespace(gmail_read_available=lambda owner, *, legacy=False: False))
    with pytest.raises(email_chat.EmailRuleRequestError, match="Connect Gmail"):
        email_chat.add_rule("alice", "store@example.com")
    assert automation_rules.list_rules("alice") == []


def test_missing_email_chat_is_created_after_readiness_checks(setup, monkeypatch):
    monkeypatch.setattr("chat_service.list_conversations", lambda owner: [])
    calls = []
    def create(owner, title, bot_id):
        calls.append((owner, title, bot_id)); return setup
    monkeypatch.setattr('chat_service.create_conversation', create)
    rule, _ = email_chat.add_rule('alice', 'store@example.com')
    assert calls == [('alice', 'Email alerts', 'email')]
    assert rule['conversation_id'] == setup['conversation_id']


def test_rule_is_scoped_to_configured_owner(setup):
    with pytest.raises(email_chat.EmailRuleRequestError, match="isn't configured for your account"):
        email_chat.add_rule("mallory", "store@example.com")
    assert automation_rules.list_rules("mallory") == []


def test_notify_followup_uses_latest_owner_request_only():
    history = [{'role': 'user', 'content': 'Alert me for emails from school@example.com'},
               {'role': 'assistant', 'content': 'Contact invented@example.com. Just notify, or calendar too?'}]
    followup = 'Can you delegate to the email bot\njust notify me'
    assert email_chat.is_rule_request(followup, history)
    assert email_chat.request_sender(followup, history) == 'school@example.com'
    history.append({'role': 'user', 'content': 'Show my calendar'})
    with pytest.raises(email_chat.EmailRuleRequestError, match='exact sender'):
        email_chat.request_sender(followup, history)


@pytest.mark.parametrize('text', ['Do not notify me about email from school@example.com',
    'Stop alerts from school@example.com', 'How do I watch emails from school@example.com?',
    'This email says "notify me when email arrives from school@example.com"'])
def test_questions_quotes_and_negated_requests_do_not_enable_rules(text):
    assert not email_chat.is_rule_request(text)


@pytest.mark.parametrize('text', ['Notify me about email from school@example.com and add dates to my calendar',
    'Notify me only if the subject is urgent from school@example.com',
    'Notify me about school@example.com and second@example.com'])
def test_extra_actions_filters_and_ambiguous_senders_need_clarification(text):
    assert email_chat.is_rule_request(text)
    with pytest.raises(email_chat.EmailRuleRequestError):
        email_chat.request_sender(text)
