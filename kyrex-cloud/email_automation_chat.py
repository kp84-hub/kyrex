"""Bounded natural-language entry point for owner email rules in The Overwatcher."""
from __future__ import annotations

import re
import os

import automation_rules
import bots
import connectors

_SENDER = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
_RULE_INTENT = re.compile(
    r"^\s*(?:(?:please\s+)?|(?:i want to|i'd like to|can you)\s+)"
    r"(?:watch|monitor)\s+(?:for\s+)?emails?\b|"
    r"^\s*(?:(?:please\s+)?|(?:i want to|i'd like to|can you)\s+)"
    r"(?:notify|alert)\s+me\s+(?:when|about)\s+emails?\b|"
    r"^\s*(?:(?:please\s+)?|(?:i want to|i'd like to|can you)\s+)"
    r"add\s+(?:(?:a|an)\s+)?(?:new\s+)?email\s+"
    r"(?:sender|address|rule|automation)\b",
    re.IGNORECASE,
)


class EmailRuleRequestError(ValueError):
    """A user-facing, fail-closed email rule setup error."""


def is_rule_request(text: str) -> bool:
    return bool(_RULE_INTENT.search(str(text or "")))


def parse_sender(text: str) -> str:
    found = _SENDER.findall(str(text or ""))
    if len(found) != 1:
        raise EmailRuleRequestError(
            "Which exact sender email address should I watch? For example: "
            "“Watch emails from sender@example.com.”")
    sender = found[0].lower()
    if not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
            r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", sender):
        raise EmailRuleRequestError("That doesn't look like one valid sender email address.")
    return sender


def add_rule(owner: str, sender: str) -> tuple[dict, str]:
    """Create/reactivate an exact-sender rule for the owner's Email Bot chat."""
    owner = str(owner or "").strip()
    if not owner:
        raise EmailRuleRequestError("I couldn't verify your account for this email rule.")
    configured_owner = os.environ.get("KYREX_AUTOMATION_OWNER", "").strip()
    token = os.environ.get("KYREX_AUTOMATION_TOKEN", "")
    if (os.environ.get("KYREX_AUTOMATION_ENABLED") != "1"
            or configured_owner != owner or len(token) < 32
            or any(char.isspace() for char in token)):
        raise EmailRuleRequestError(
            "The always-on email watcher isn't configured for your account yet. "
            "Finish its automation setup, then ask me again.")
    if not connectors.default_store().gmail_read_available(owner):
        raise EmailRuleRequestError(
            "Connect Gmail read access in Settings before I can watch a sender.")

    owned = [b for b in bots.load_bots().values()
             if str(b.get("owner") or "").strip() == owner
             and bots.is_running(b)
             and (str(b.get("name") or "").strip().casefold() == "email bot"
                  or str(b.get("role") or "").strip().casefold() == "email")]
    if len(owned) != 1:
        raise EmailRuleRequestError(
            "I need exactly one running Email Bot owned by you. Check Bots, "
            "then try again.")
    email_bot = owned[0]

    import chat_service
    chats = [c for c in chat_service.list_conversations(owner)
             if c.get("bot_id") == email_bot.get("id")]
    if not chats:
        raise EmailRuleRequestError(
            "I couldn't find an existing Email Bot chat to deliver messages to. "
            "Open Email Bot once, then try again.")
    # list_conversations is newest-first, so keep new mail in the latest
    # existing Email Bot conversation, as requested by the user.
    destination = chats[0]
    conversation_id = str(destination.get("conversation_id") or "")
    conv = chat_service.get_conversation(owner, conversation_id)
    if not conv or conv.get("bot_id") != email_bot.get("id"):
        raise EmailRuleRequestError("The Email Bot chat is unavailable; no rule was added.")

    existing = next((r for r in automation_rules.list_rules(owner)
                     if r.get("sender", "").lower() == sender
                     and r.get("bot_id") == email_bot.get("id")
                     and r.get("conversation_id") == conversation_id), None)
    if existing:
        if not existing.get("enabled"):
            automation_rules.set_enabled(owner, existing["rule_id"], True)
            existing["enabled"] = 1
        return existing, str(destination.get("title") or "Email Bot chat")
    try:
        rule = automation_rules.create_rule(
            owner, sender, str(email_bot["id"]), conversation_id)
    except ValueError as exc:
        raise EmailRuleRequestError(str(exc)) from None
    return rule, str(destination.get("title") or "Email Bot chat")
