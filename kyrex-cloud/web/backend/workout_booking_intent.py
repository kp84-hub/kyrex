"""Shared class-domain words for the native workout and restaurant routers."""
import re

WORKOUT = re.compile(
    r'\b(?:workout|fitness class|gym class|level[\s-]*(?:6|six)|l6|glofox)\b', re.I)


def booking_text(text):
    """Recover an explicit class request after rejecting a restaurant booking.

    Only remove the rejected restaurant clause, never a refusal to book a class
    or a general negation. The remaining request still passes normal intent,
    scope and approval checks.
    """
    text = str(text or '').strip().replace('’', "'")
    correction = re.match(
        r"^i (?:don't|do not) want (?:a |the )?(?:restaurant )?reservation[.!;]\s+(.+)$",
        text, re.I)
    if correction and WORKOUT.search(correction[1]):
        return correction[1]
    return text
