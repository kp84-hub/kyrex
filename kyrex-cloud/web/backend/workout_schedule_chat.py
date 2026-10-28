"""Read the supported studio's exact class through the booking source adapter."""
import asyncio
import re
from datetime import datetime

import workout_booking_chat as booking
import workout_booking_schema as schema
import workout_class_reader

STUDIO = re.compile(r'\b(?:level[\s-]*(?:6|six)|l6|glofox)\b', re.I)
WRITE_OR_OTHER = re.compile(
    r"\b(?:book|reserve|register|cancel|delete|reschedule|send|email|mail|calendar|"
    r"implement|debug|code|fix)\b|\b(?:don't|do not|never|sign me up)\b", re.I)


def candidate(text):
    """Only explicit supported-studio trainer reads; never infer a booking."""
    text = booking.normalize_booking_text(text)
    if not STUDIO.search(text) or WRITE_OR_OTHER.search(text):
        return False
    return bool(re.search(r'\b(?:class|session|workout|gym)\b', text, re.I)
                and re.search(r'\bwho\b.+\b(?:running|teaching|leading|coaching|instructing)\b', text, re.I)
                or re.search(r'\b(?:trainer|instructor|coach)\b', text, re.I)
                and re.match(r'^(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:check|show|tell|find|who|which|what)\b', text, re.I))


async def stream(user, conv, text, request_id, cancel, workspace_id):
    import chat_service as chat
    import glofox_api
    import member_bookings
    selected = None
    if conv.get('bot_id'):
        selected = chat.resolve_bot_for_user(user, conv['bot_id'])
        if selected.get('owner') != user:
            raise chat.ChatUnavailable('Class schedule reads require your own Bot.')
        try:
            member_bookings.permitted(selected, 'glofox:read')
        except member_bookings.FlowError as exc:
            raise chat.ChatUnavailable(str(exc)) from None
        if workspace_id is not chat._WORKSPACE_UNSET and str(workspace_id or '').strip():
            raise chat.ChatUnavailable('A Bot-bound class read cannot attach another workspace.')
    if cancel is not None and cancel.is_set():
        yield {'type': 'status', 'status': 'cancelled', 'content': ''}
        return
    aid = f'turn-{request_id}-assistant' if request_id else None
    existing = next((m for m in conv.get('messages', []) if aid and m.get('id') == aid), None)
    yield {'type': 'conversation', 'conversation_id': conv['conversation_id']}
    if existing:
        yield {'type': 'status', 'status': 'complete', 'content': existing['content']}
        return
    chat._append_message(user, conv, 'user', text, identity=f'turn-{request_id}-user' if request_id else None)
    chat._write(user, conv)
    try:
        details = booking.explicit_details(text)
        if not details.get('date') or not details.get('time'):
            answer = 'What date and time should I check for the Level 6 trainer? Please include both in your question.'
        else:
            yield {'type': 'progress', 'payload': {'stage': 'Checking the Glofox class schedule used for booking…'}}
            async with asyncio.timeout(30):
                result = await asyncio.to_thread(workout_class_reader.class_at,
                    details['date'], details['time'], for_booking=False)
            if selected:
                current = chat.resolve_bot_for_user(user, selected['id'])
                member_bookings.permitted(current, 'glofox:read')
            label = datetime.fromisoformat(result['date'] + 'T' + result['time']).strftime('%A, %B %d, %Y at %I:%M %p')
            # Provider text remains display data and cannot inject Markdown links.
            trainer = re.sub(r'([\\`*_{}\[\]()<>#!|])', r'\\\1', result['trainer_name'])
            answer = (f"Glofox lists **{trainer}** for **Level 6 · {result['class_name']}** "
                      f"on **{label} ET**.\n\nSource: [Level 6 class schedule]({schema.URL}).")
    except booking.WorkoutClarification as exc:
        answer = str(exc)
    except member_bookings.FlowError:
        answer = 'This Bot restricts Glofox schedule reads. Choose a Bot allowed to read the class schedule.'
    except ValueError:
        answer = 'Please give one valid date and time for the Level 6 trainer check.'
    except (glofox_api.GlofoxError, TimeoutError):
        answer = 'I could not verify that trainer from the Glofox class schedule. Please try the schedule check again later.'
    if cancel is not None and cancel.is_set():
        yield {'type': 'status', 'status': 'cancelled', 'content': ''}
        return
    current = chat.get_conversation(user, conv['conversation_id']) or conv
    chat._append_message(user, current, 'assistant', answer, identity=aid)
    chat._write(user, current)
    yield {'type': 'status', 'status': 'complete', 'content': answer}
