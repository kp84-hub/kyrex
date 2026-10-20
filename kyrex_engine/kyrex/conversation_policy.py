"""Shared conversational uncertainty policy and side-effect-free question payload."""
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
    "but never execute a dependent action in the same batch as a clarification question."
)


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
