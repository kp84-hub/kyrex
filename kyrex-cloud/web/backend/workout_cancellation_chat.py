"""Cancellation preparation in the existing workout producer and BookingStore."""
import asyncio
import re
import time
from datetime import datetime, timedelta

import booking_store
import member_bookings
import member_bookings_schema as member
import workout_cancellation_schema as schema
from workout_booking_intent import WORKOUT, booking_text

UNKNOWN = 'The cancellation outcome is unknown. Check Glofox My bookings before trying again. No automatic retry.'
START = re.compile(r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+|i(?:'d| would) like (?:you )?to\s+)?(?:cancel|unbook|remove me from|drop)\b", re.I)
YES = {'yes', 'yes please', 'yes, please', 'go ahead', 'that works', 'ok', 'okay', 'sure', 'confirm',
       'confirm cancellation', 'cancel it', 'cancel the class', 'cancel this reservation'}
NO = {'no', 'no thanks', 'keep it', 'keep my reservation', 'keep the booking', 'cancel', 'cancel request', 'stop', 'never mind', 'nevermind'}


def requested(text):
    text = booking_text(text)
    return bool(START.match(text) and WORKOUT.search(text) and re.search(r'\b(?:class(?:es)?|bookings?|reservations?|sessions?)\b', text, re.I)
        and not re.search(r'\b(?:calendar|membership|subscription|email|implement|debug|code|example|daughter|son|spouse|wife|husband|child|family)\b', text, re.I))


def candidate(text, conv):
    import booking_chat
    import workout_booking_chat as flow
    last = booking_chat.latest(conv)
    entry = last.get('workout_cancellation') or {}
    value = booking_text(text).lower().rstrip('.!')
    return bool(requested(text) or entry.get('id') and (
        value in YES | NO | {'book it'}
        or entry.get('state') in {'collecting', 'proposed'}
        and (flow.DATE.search(text) or flow.TIME.search(text))))


def review(data):
    return (f"Cancel **{schema.label(data['details'])}** using your signed-in Glofox member account?\n\n"
        f"Portal confirmation: {data['terms']}\n\n"
        'Reply **yes** to cancel this exact reservation once, or **no** to keep it. Nothing has been cancelled yet.')


def unresolved(store, owner, target):
    with store.db() as db:
        ids = [r['id'] for r in db.execute("SELECT id FROM bookings WHERE owner=? AND state IN ('unknown','submitting')", (owner,))]
    return any((r := store.get(owner, ident))['data'].get('kind') == 'workout_cancel'
        and r['data'].get('details', {}).get('reference') == target for ident in ids)


async def turn(user, conv, text, request_id, cancel, workspace_id):
    import booking_chat
    import chat_service as chat
    import workout_booking_chat as flow
    if conv.get('bot_id'):
        selected = chat.resolve_bot_for_user(user, conv['bot_id'])
        if selected.get('owner') != user: raise chat.ChatUnavailable('Class cancellation requires your own Bot.')
        try:
            member_bookings.permitted(selected, 'browser:read')
            member_bookings.permitted(selected, 'browser:cancel_booking')
        except ValueError as exc:
            raise chat.ChatUnavailable(str(exc)) from None
        if workspace_id is not chat._WORKSPACE_UNSET and str(workspace_id or '').strip():
            raise chat.ChatUnavailable('A Bot-bound cancellation cannot attach another workspace.')
    if cancel is not None and cancel.is_set():
        yield {'type': 'status', 'status': 'cancelled', 'content': ''}; return
    store = booking_store.BookingStore(); cid = conv['conversation_id']
    aid = f'turn-{request_id}-assistant' if request_id else None
    old = next((m for m in conv.get('messages', []) if aid and m.get('id') == aid and m.get('role') == 'assistant'), None)
    if old:
        yield {'type': 'conversation', 'conversation_id': cid}
        yield {'type': 'status', 'status': 'complete', 'content': old['content']}; return
    previous = booking_chat.latest(conv)
    entry = previous.get('workout_cancellation') or {}
    fresh = requested(text)
    row = None; claimed = False; displayed = False
    chat._append_message(user, conv, 'user', text, identity=f'turn-{request_id}-user' if request_id else None)
    message = chat._append_message(user, conv, 'assistant', 'Checking your existing Level 6 reservation for cancellation…', identity=aid)
    message['workout_pending'] = {'deadline': time.time() + flow.TURN_SECONDS + 30, 'operation': 'cancel'}
    chat._write(user, conv)
    yield {'type': 'conversation', 'conversation_id': cid}
    yield {'type': 'progress', 'payload': {'stage': message['content']}}
    try:
        if entry.get('id') and not fresh:
            saved = store.get(user, entry['id'])
            if saved['conversation'] != cid or saved['data'].get('kind') != 'workout_cancel':
                raise booking_store.BookingError('Use the cancellation review from this conversation.')
            if saved['data'].get('initiator') != conv.get('bot_id'):
                raise booking_store.BookingError('The selected Bot changed. Prepare a new cancellation review.')
            row = saved
        else:
            # A new cancellation request cannot leave an old unsubmitted
            # booking/cancellation review active in this conversation.
            prior = previous.get('workout_cancellation') or previous.get('workout_booking') or {}
            if prior.get('id'):
                old_row = store.get(user, prior['id'])
                if old_row['conversation'] == cid and old_row['state'] in {'collecting', 'proposed'}:
                    store.update(old_row, old_row['data'], 'cancelled')
            row = store.create(user, cid, f'workout-cancel:{cid}:{request_id}' if request_id else None)
            row = store.update(row, {'kind': 'workout_cancel', 'initiator': conv.get('bot_id'),
                'requested': {}, 'messages': [], 'steps': [], 'url': member.URL}, 'collecting')
        message['workout_pending']['id'] = row['id']; chat._write(user, conv)
        low = booking_text(text).lower().rstrip('.!')
        if not fresh and (low in NO or low == 'book it'):
            if row['state'] not in {'collecting', 'proposed'}: raise booking_store.BookingError('This cancellation already had an attempt. Check Glofox My bookings.')
            row = store.update(row, row['data'], 'cancelled')
            answer = 'Stopped the cancellation proposal. Your reservation was not cancelled.'
            if low == 'book it': answer += ' Booking is a separate action; give a new class date and time to book another class.'
        elif not fresh and low in YES:
            if row['state'] != 'proposed': raise booking_store.BookingError('There is no current cancellation review to approve. No cancellation was repeated.')
            row = store.claim(user, row['id'], entry['revision'], cid, previous['id']); claimed = True
            message['workout_pending']['submitting'] = True; chat._write(user, conv)
            try:
                row, result = await flow.dispatch(store, row, 'submit', chat._task_store(), cancel)
            except booking_store.BookingError:
                result = {'outcome': 'unknown'}
            outcome = result.get('outcome')
            if outcome not in {'confirmed', 'changed', 'cancelled', 'unknown'}: outcome = 'unknown'
            if outcome == 'confirmed' and result.get('reference') != row['data']['details']['reference']: outcome = 'unknown'
            data = row['data']; data['receipt'] = {'reference': result.get('reference', '')}
            row = store.update(row, data, outcome)
            answer = (schema.confirmed(data) if outcome == 'confirmed' else UNKNOWN if outcome == 'unknown'
                else 'The reservation, member session or cancellation confirmation changed. Nothing was cancelled. Start a fresh cancellation review.')
        else:
            if row['state'] not in {'collecting', 'proposed'}: raise booking_store.BookingError('Start a new cancellation request explicitly. No previous attempt will be repeated.')
            data = row['data']; data['requested'] = flow.explicit_details(text, data.get('requested', {}))
            row = store.update(row, data, 'collecting')  # Invalidates the old review before any new read.
            missing = [k for k in ('date', 'time') if not data['requested'].get(k)]
            if missing:
                answer = 'What ' + ' and '.join(missing) + ' is the Level 6 reservation you want to cancel?'
            else:
                wanted = datetime.fromisoformat(data['requested']['date'] + 'T' + data['requested']['time']).replace(tzinfo=member.TZ)
                if wanted.timestamp() <= time.time(): raise booking_store.BookingError('That class has already started. No cancellation was submitted.')
                window = {'url': member.URL, 'start': wanted.date().isoformat(), 'end': (wanted.date() + timedelta(days=1)).isoformat()}
                found = await asyncio.to_thread(member_bookings.read, user, window, 'workout-cancel-' + row['id'], chat._task_store())
                matches = [b for b in found['bookings'] if b['title'] == 'Group Fitness Class'
                    and datetime.fromtimestamp(b['start'], member.TZ).strftime('%Y-%m-%d %H:%M') == wanted.strftime('%Y-%m-%d %H:%M')]
                if len(matches) != 1:
                    raise booking_store.BookingError('I could not verify one exact booked Level 6 class at that date and time. Nothing was cancelled.')
                if unresolved(store, user, matches[0]['reference']): raise booking_store.BookingError(UNKNOWN)
                data.update(details=matches[0], window=window, identity=found['identity'], browser_binding=found['browser_binding'])
                row = store.update(row, data, 'preparing')
                row, result = await flow.dispatch(store, row, 'prepare', chat._task_store(), cancel)
                plan = result.get('review') or {}
                spec = {'details': data['details'], 'window': window, 'identity': data['identity']}
                expected = schema.digest(spec, plan['submit'], plan['terms'], plan['request'])
                if plan.get('expected') != expected: raise booking_store.BookingError('The cancellation review could not be verified. Nothing was cancelled.')
                data.update(submit=plan['submit'], request=plan['request'], terms=plan['terms'], expected=expected)
                row = store.update(row, data, 'proposed')
                answer = review(data); displayed = True
    except (Exception, asyncio.CancelledError) as exc:
        answer = UNKNOWN if claimed else (str(exc) if isinstance(exc, (booking_store.BookingError, member_bookings.FlowError, flow.WorkoutClarification))
            else 'The cancellation could not be prepared safely. No cancellation was submitted.')
        if row:
            current = store.get(user, row['id'])
            if current['state'] not in {'confirmed', 'unknown', 'changed', 'cancelled'}:
                store.request_cancel(user, row['id']); current = store.get(user, row['id'])
                row = store.update(current, current['data'], 'unknown' if claimed else 'blocked')
    current = chat.get_conversation(user, cid) or conv
    stopped = cancel is not None and cancel.is_set() and not claimed
    if stopped:
        answer = 'Cancellation preparation stopped. No cancellation was submitted.'
        displayed = False
        if row:
            store.request_cancel(user, row['id']); row = store.get(user, row['id'])
            row = store.update(row, row['data'], 'blocked')
    message = chat._append_message(user, current, 'assistant', answer, identity=aid)
    message['content'] = answer
    message.pop('workout_pending', None)
    if row:
        if displayed:
            data = row['data']; data['assistant_id'] = message['id']; row = store.update(row, data, 'proposed')
        message['workout_cancellation'] = {'id': row['id'], 'revision': row['revision'], 'state': row['state']}
    chat._write(user, current)
    yield {'type': 'status', 'status': 'cancelled' if stopped else 'complete', 'content': answer}
