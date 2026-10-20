"""Shared class-domain words for the native workout and restaurant routers."""
import re

WORKOUT = re.compile(
    r'\b(?:workout|fitness class|gym class|level[\s-]*(?:6|six)|l6|glofox)\b', re.I)


def normalize_booking_text(text):
    """Normalize pasted double quotes without discarding intent or negation."""
    text = str(text or '').strip()
    if text.startswith(('"', '“')):
        inner = text[1:].lstrip()
        # A quotation followed by commentary is an example, not a command.
        if not re.search(r'["”]\s*\S', inner):
            text = inner[:-1].rstrip() if inner.endswith(('"', '”')) else inner
    return text.replace('’', "'")


def booking_text(text):
    """Recover an explicit class request after rejecting a restaurant booking.

    Only remove the rejected restaurant clause, never a refusal to book a class
    or a general negation. The remaining request still passes normal intent,
    scope and approval checks.
    """
    text = normalize_booking_text(text)
    correction = re.match(
        r"^i (?:don't|do not) want (?:a |the )?(?:restaurant )?reservation[.!;]\s+(.+)$",
        text, re.I)
    if correction and WORKOUT.search(correction[1]):
        return correction[1]
    return text
