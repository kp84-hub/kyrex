"""Resolve a send follow-up against saved assistant draft metadata, never prose."""
import re

_REQUEST = re.compile(
    r"(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?"
    r"(?:send|text|share|forward)\s+"
    r"(?:this|that|it|the\s+(?:workout\s+)?(?:message|preview|draft))"
    r"(?:\s+(?:workout\s+)?(?:message|preview|draft))?"
    r"(?:\s+to\s+([^:\n!?]{1,200}?))?[.!?]?",
    re.IGNORECASE,
)
_LEGACY_CLARIFICATION = (
    'What event, date, and start time should I use? Include an end time or duration, or say all day.'
)


def request(text):
    """Return the named recipient (or empty default), or None for other intents."""
    match = _REQUEST.fullmatch(str(text or '').strip())
    if not match:
        return None
    recipient = (match.group(1) or '').strip().strip('"“”')
    # Compound instructions need their own clarification; don't treat another
    # action as part of a recipient name or silently ignore a negative clause.
    if re.search(r'\b(?:(?:and|but)\s+(?:send|add|delete|schedule|change)|without|instead|don[’\']t|do\s+not)\b', recipient, re.I):
        return None
    return recipient


def latest_draft(messages):
    """Only the latest substantive assistant answer can supply 'this'."""
    if not isinstance(messages, list):
        return None
    for message in reversed(messages[-12:]):
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            continue
        content = str(message.get('content') or '').strip()
        # Allow a retry of the exact erroneous Calendar response this fixes.
        if content == _LEGACY_CLARIFICATION:
            continue
        draft = message.get('message_draft')
        source_id = message.get('workout_preview_message_id')
        recipient = message.get('workout_preview_recipient')
        if source_id:
            source = next((item for item in messages if isinstance(item, dict)
                           and item.get('role') == 'assistant' and item.get('id') == source_id), None)
            if source:
                message = source
                draft = source.get('message_draft')
        if (isinstance(draft, dict) and isinstance(draft.get('text'), str)
                and draft['text'].startswith('#L6Workout\n')
                and isinstance(draft.get('recipient'), str) and draft['recipient'].strip()
                and message.get('id')):
            return {**message, 'followup_recipient': recipient}
        if content:
            return None
    return None
