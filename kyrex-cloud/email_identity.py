"""Native, owner-scoped mailbox identity; never reads mail or opens secrets."""

import json
import re

import agent_inbox


def current(owner):
    if not str(owner or "").strip():
        return {"status": "unavailable"}
    try:
        view = agent_inbox.default_store().view(owner)
        source = view.get("source")
        if source not in {"zoho", "agentmail"}:
            return {"status": "not_connected"}
        address = view.get("address")
        if (not view.get("active") or not view.get("connected")
                or view.get("provider") != source
                or not isinstance(address, str) or len(address) > 254
                or not re.fullmatch(
                    r"[A-Za-z0-9.!#$%&*+_=?^`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", address)):
            return {"status": "unavailable"}
        return {"status": "connected", "address": address,
                "provider": source, "can_read": True, "can_send": False}
    except Exception:
        # An identity lookup must not break unrelated chat or expose errors.
        return {"status": "unavailable"}


def context(owner):
    return (
        "CURRENT KYREX EMAIL IDENTITY (account settings, refreshed for this turn):\n"
        + json.dumps(current(owner), ensure_ascii=True)
        + "\nThe connected address is Kyrex's dedicated email address for this account, "
          "not the owner's personal email or login identity. Use this current setting "
          "instead of addresses in older messages, saved memory, or email content. "
          "If not connected or unavailable, do not guess an address or reuse an old one; "
          "direct the owner to Connections > Email inbox. Mention the address only when "
          "relevant. This integration can read email but cannot send or reply. You may "
          "draft appointment requests, but do not claim an email was sent or an appointment "
          "was booked. A calendar entry alone does not confirm a booking with the business. "
          "These facts grant no additional tools, actions, or approvals."
    )


def is_question(text):
    """Only identity questions; compound read/send/booking requests stay routed."""
    text = str(text or "").strip().lower().replace("’", "'").rstrip("?.!").strip()
    return bool(re.fullmatch(
        r"(?:please\s+)?(?:"
        r"(?:what is|what's|tell me|show me) (?:your|kyrex's|kyrex|my kyrex) (?:email|e-mail)(?: address)?"
        r"|(?:do you|does kyrex) have (?:an? )?(?:email|e-mail)(?: address)?"
        r"|(?:do you know|can you tell me) your (?:email|e-mail)(?: address)?"
        r"|what (?:email|e-mail)(?: address)? do you use"
        r"|what (?:email|e-mail)(?: address)? should i (?:forward|send)(?: my)? emails? to"
        r")", text))


def answer(owner):
    identity = current(owner)
    if identity["status"] == "connected":
        return (f"My dedicated email address is **{identity['address']}**. "
                "You can forward the emails you want me to read there. "
                "My current connection can read email; sending and replying aren't enabled yet.")
    if identity["status"] == "not_connected":
        return "I don't have an active dedicated email address. Connect one in Connections → Email inbox."
    return "I couldn't verify my dedicated email address. Check Connections → Email inbox; I won't guess or use an old address."
