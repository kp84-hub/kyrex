"""Shared conversational uncertainty policy and side-effect-free question payload."""
import re


_REPEAT_REQUEST = re.compile(
    r'\s*(?:please\s+)?(?:try(?:\s+that)?\s+again|retry|check\s+again)\s*[.!?]?\s*', re.I)


def _repeat_text(value):
    return isinstance(value, str) and bool(_REPEAT_REQUEST.fullmatch(value))


CLARIFICATION_POLICY = (
    "Clarification applies to EVERY request, not just one connector or workflow. "
    "Understand the user's intended outcome before choosing a tool or delegation. "
    "When the goal, meaning, target, reference, scope, required detail or consequence is unclear, "
    "ask one focused, natural question that resolves the uncertainty. Do not guess missing facts, "
    "invent dates/durations/recipients, silently substitute another action, or force the request into a fixed script. "
    "Use available context and authorized read-only lookups when they can resolve the uncertainty; "
    "do not ask the user to repeat details already supplied or answer something you can verify yourself. "
    "When a user decision is needed, use request_clarification if that tool is available and wait. "
    "Otherwise ask the question conversationally. Preserve the original goal and all known details so the "
    "next reply continues the same task. Ask a follow-up if the reply still does not resolve the uncertainty. "
    "A clarification answer is not approval to send, book, delete, purchase or perform another external effect; "
    "the existing exact-action review and permissions still apply. Never claim completion while waiting for input. "
    "Proceed directly on clear requests. Do not turn ordinary factual uncertainty into needless questions: "
    "verify it when possible, or state what remains unknown. Independent safe work may continue, "
    "but never execute a dependent action in the same batch as a clarification question. "
    "Interpret follow-ups using the latest relevant owner ask and reply, including native and delegated "
    "results that bypassed your local model history. A new topic supersedes an older topic. "
    "Use the shared conversation context to understand references such as 'that one', 'what about tomorrow', "
    "and 'try again'; do not silently return to an older unrelated task. If the reference is ambiguous, "
    "ask which request the owner means. A retry of pending, completed or uncertain external work requires "
    "checking its existing task status and receipt before preparing any further action."
)


def repeated_request(text, messages):
    """Recover an omitted subject for an exact repeat, independent of Bot/tool.

    This is context, never permission to replay an action. Consumers still
    validate their operation, scope, current task state and approval normally.
    Other follow-ups keep their wording for the model to interpret in context.
    """
    if not _repeat_text(text):
        return text
    for message in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        previous = message.get('content')
        if message.get('truncated'):
            return text
        if _repeat_text(previous):
            continue
        return previous if isinstance(previous, str) and previous.strip() and len(previous) <= 8000 else text
    return text


def bounded_dialogue(messages, *, limit=12, budget=12000, item_limit=2500):
    """Project conversation data without granting message-supplied authority."""
    out = []
    for message in reversed(messages[-limit:] if isinstance(messages, list) else []):
        if not isinstance(message, dict) or message.get('role') not in {'user', 'assistant'}:
            continue
        content = message.get('content')
        if not isinstance(content, str) or not content.strip():
            continue
        content = content[:min(item_limit, budget)]
        out.append({'role': message['role'], 'content': content})
        budget -= len(content)
        if budget <= 0:
            break
    return list(reversed(out))


def routing_dialogue(messages):
    """Routing needs prior owner asks, rather than full private tool results."""
    owners = [m for m in messages if isinstance(m, dict) and m.get('role') == 'user'
              and not _repeat_text(m.get('content'))] if isinstance(messages, list) else []
    out = bounded_dialogue(owners, limit=3, budget=5000, item_limit=2000)
    originals = [m for m in owners[-3:] if isinstance(m.get('content'), str) and m['content'].strip()]
    for item, original in zip(reversed(out), reversed(originals)):
        if original.get('truncated') or len(item['content']) < len(original['content']):
            item['truncated'] = True
    return out


def clarification_payload(question, choices=None):
    if not isinstance(question, str) or not question.strip() or len(question) > 1200:
        raise ValueError('Clarification needs one non-empty question (max 1200 characters).')
    if choices is None:
        choices = []
    if (not isinstance(choices, list) or len(choices) > 4
            or any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in choices)):
        raise ValueError('Clarification choices must be at most four short strings.')
    question = question.strip()
    choices = [item.strip() for item in choices]
    content = question + ('\n\n' + '\n'.join('- ' + item for item in choices) if choices else '')
    return {'status': 'needs_input', 'question': question, 'choices': choices, 'content': content}
