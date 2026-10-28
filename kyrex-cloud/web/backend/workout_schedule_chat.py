"""Read the supported studio's exact class through the booking source adapter."""
import asyncio
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import workout_booking_chat as booking
import workout_booking_schema as schema
import workout_class_reader

STUDIO = re.compile(r'\b(?:level[\s-]*(?:6|six)|l6|glofox)\b', re.I)
WRITE_OR_OTHER = re.compile(
    r"\b(?:book|reserve|register|cancel|delete|reschedule|schedule|add|create|send|email|mail|calendar|"
    r"implement|debug|code|fix)\b|\b(?:don't|do not|never|sign me up)\b", re.I)
TRAINER = re.compile(r'\b(?:trainer|instructor|coach)\b', re.I)
READ = re.compile(r'\b(?:who|which|what|check|look\s+up|lookup|find|show|tell|know)\b', re.I)
CONTEXT_SECONDS = 30 * 60


def _trainer_question(text):
    # Intent words can occur anywhere: "Look up who is the trainer" and
    # "I just want to know who the trainer is" are the same read intent.
    return bool(TRAINER.search(text) and (READ.search(text) or booking.DATE.search(text)
                                         or booking.TIME.search(text) or text.endswith('?'))
                or re.search(r'\b(?:class|session|workout|gym)\b', text, re.I)
                and re.search(r'\bwho\b.+\b(?:running|teaching|leading|coaching|instructing)\b', text, re.I))


def _continuation(text):
    if WRITE_OR_OTHER.search(text):
        return False
    if (re.search(r'\b(?:another|other)\s+(?:studio|gym|class)|\belsewhere\b', text, re.I)
            or re.search(r'\blevel[\s-]*\d+\b', text, re.I) and not STUDIO.search(text)):
        return False
    if _trainer_question(text):
        return True
    if re.fullmatch(r'(?:please )?(?:try|check|look) again[.!?]*', text, re.I):
        return True
    # A detail reply continues a known read; it cannot establish the intent.
    if not (booking.DATE.search(text) or booking.TIME.search(text)):
        return False
    remaining = booking.TIME.sub(' ', booking.DATE.sub(' ', STUDIO.sub(' ', text)))
    words = set(re.findall(r'\w+', remaining.lower()))
    return words <= {'training', 'class', 'session', 'workout', 'the', 'that', 'at', 'on', 'for', 'instead', 'please'}


def request_sources(text, conv=None):
    """Recover only a recent same-conversation owner-authored trainer read."""
    text = booking.normalize_booking_text(text)
    if WRITE_OR_OTHER.search(text):
        return None
    direct = bool(STUDIO.search(text) and _trainer_question(text))
    if direct and booking.DATE.search(text) and booking.TIME.search(text):
        return [(text, None)]
    if not direct and not _continuation(text):
        return None
    tail = [(text, None)]
    now = datetime.now(timezone.utc)
    for message in reversed((conv or {}).get('messages', [])[-16:]):
        if message.get('role') != 'user':
            continue  # Assistant guesses and erroneous choices are not facts.
        value = booking.normalize_booking_text(message.get('content', ''))
        try:
            created = datetime.fromisoformat(message['created_at'])
            if created.tzinfo is None or not 0 <= (now - created).total_seconds() <= CONTEXT_SECONDS:
                break
        except (KeyError, TypeError, ValueError):
            break
        if WRITE_OR_OTHER.search(value):
            break
        tail.append((value, created))
        if STUDIO.search(value) and _trainer_question(value):
            return list(reversed(tail))
        if not _continuation(value):
            break  # An unrelated owner turn ends this read's context.
    return [(text, None)] if direct else None


def candidate(text, conv=None):
    return request_sources(text, conv) is not None


def class_references(conv):
    """Owner-requested class facts for semantic reference resolution, not approval.

    Older native replies did not save structured class results. Their owner
    date/time evidence can still seed preparation, which always reads Glofox
    afresh before presenting an exact booking review.
    """
    references, seen = [], set()
    now = datetime.now(timezone.utc)
    messages = conv.get('messages', [])
    for index in range(len(messages) - 1, max(-1, len(messages) - 17), -1):
        message = messages[index]
        if message.get('role') != 'user': continue
        try:
            created = datetime.fromisoformat(message['created_at'])
            if created.tzinfo is None or not 0 <= (now - created).total_seconds() <= CONTEXT_SECONDS:
                continue
            sources = request_sources(message.get('content', ''), {**conv, 'messages': messages[:index]})
            if not sources: continue
            sources[-1] = (sources[-1][0], created)
            details = _details(sources)
            if set(details) != {'date', 'time'} or not message.get('id'): continue
        except (KeyError, TypeError, ValueError, booking.WorkoutClarification):
            continue
        key = (details['date'], details['time'])
        if key in seen: continue
        seen.add(key)
        reference = {'id': message['id'], 'details': details, 'source': schema.URL}
        following = next((m for m in messages[index+1:] if m.get('role') in {'user', 'assistant'}), {})
        observed = following.get('class_schedule_result')
        if (following.get('role') == 'assistant' and isinstance(observed, dict)
                and observed.get('request_id') == message['id'] and observed.get('bot_id') == conv.get('bot_id')):
            reference['observed_class'] = observed.get('class')
        references.append(reference)
        if len(references) == 3: break
    return references


def _details(sources):
    details = {}
    for index, (text, created) in enumerate(sources):
        now = created.astimezone(ZoneInfo('America/New_York')) if created else None
        try:
            details = booking.explicit_details(text, details, now=now)
        except (booking.WorkoutClarification, ValueError):
            if index == len(sources) - 1:
                raise
            # A bad time must not discard a valid date in the same owner
            # message (or vice versa). A subsequent correction can fix it.
            for key, supplied, other in (('date', booking.DATE, booking.TIME),
                                         ('time', booking.TIME, booking.DATE)):
                if not supplied.search(text): continue
                details.pop(key, None)
                try:
                    partial = booking.explicit_details(other.sub(' ', text), now=now)
                except (booking.WorkoutClarification, ValueError):
                    continue
                if key in partial: details[key] = partial[key]
    return details


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
    sources = request_sources(text, conv)
    if sources is None:
        raise chat.ChatUnavailable('Please state the class trainer question.')
    # Retire an earlier mistaken scheduling choice without touching bookings.
    import workout_action_choice
    conv.pop(workout_action_choice.KEY, None)
    owner_message = chat._append_message(user, conv, 'user', text, identity=f'turn-{request_id}-user' if request_id else None)
    chat._write(user, conv)
    observed = None
    try:
        details = _details(sources)
        if not details.get('date') or not details.get('time'):
            missing = [key for key in ('date', 'time') if not details.get(key)]
            answer = 'What ' + ' and '.join(missing) + ' should I check for the Level 6 trainer?'
            if missing == ['time']: answer += ' Please include AM or PM.'
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
            observed = {'request_id': owner_message['id'], 'bot_id': conv.get('bot_id'), 'class': result}
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
    message = chat._append_message(user, current, 'assistant', answer, identity=aid)
    if observed: message['class_schedule_result'] = observed
    chat._write(user, current)
    yield {'type': 'status', 'status': 'complete', 'content': answer}
