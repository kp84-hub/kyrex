"""Route an explicit recurring reminder through the existing Calendar writer."""
import json
import re

import bots
import cal_writer


def _turn(messages, request_id, role):
    if request_id:
        identity = f'turn-{request_id}-{role}'
        return next((message for message in messages if message.get('id') == identity), {})
    return {}


def requested(text, conv, request_id):
    messages = (conv or {}).get('messages') or []
    if (_turn(messages, request_id, 'user').get('daily_reminder_intent')
            or _turn(messages, request_id, 'assistant').get('daily_reminder_prompt')):
        return True
    if cal_writer.daily_reminder_details(text) is not None:
        return True
    # Only the immediately preceding native question can supply recurrence.
    # Assistant-generated discussion and old questions confer no write intent.
    return bool(messages and messages[-1].get('daily_reminder_prompt')
                and re.search(r'\bat\s+\d', text, re.I))


async def stream(chat, user, conv, bot, text, request_id, cancel, workspace_id):
    if bot.get('owner') != user or not bots.is_running(bot):
        raise chat.ChatUnavailable('Use one of your own running Bots to set the reminder.')
    if workspace_id not in (chat._WORKSPACE_UNSET, None, ''):
        raise chat.ChatUnavailable('A reminder cannot attach a workspace.')
    if cancel is not None and cancel.is_set():
        yield {'type': 'status', 'status': 'cancelled', 'content': ''}
        return
    cid = conv['conversation_id']
    messages = conv.get('messages') or []
    identity = f'turn-{request_id}' if request_id else None
    previous_reply = _turn(messages, request_id, 'assistant')
    intent = _turn(messages, request_id, 'user').get('daily_reminder_intent')
    prompt = False
    if previous_reply.get('daily_reminder_prompt'):
        answer = previous_reply['content']
    elif not chat.dev_bot._calendar_write_available(user):
        answer = 'Connect Google Calendar write access in Settings to set a daily reminder.'
        intent = None
    elif intent is None:
        details = cal_writer.daily_reminder_details(text)
        if details == '':
            answer = cal_writer.DAILY_REMINDER_QUESTION
            prompt = True
        else:
            try:
                intent = cal_writer.daily_reminder_intent(text if details is None else details)
            except cal_writer.CalendarWriterError as exc:
                answer = str(exc)
                prompt = True
    if not previous_reply:
        message = chat._append_message(user, conv, 'user', text,
                                       identity=f'{identity}-user' if identity else None)
        if intent:
            message['daily_reminder_intent'] = intent
        else:
            reply = chat._append_message(user, conv, 'assistant', answer,
                                         identity=f'{identity}-assistant' if identity else None)
            if prompt:
                reply['daily_reminder_prompt'] = True
        chat._write(user, conv)
    if intent:
        # Freeze the resolved date across replays, including a retry after
        # midnight. The normal writer deduplicates the validated intent.
        async for frame in chat._stream_writable_bot_task(
                user, conv, bot, json.dumps(intent), cid, cancel, calendar_intent=intent):
            yield frame
        return
    yield {'type': 'conversation', 'conversation_id': cid}
    yield {'type': 'status', 'status': 'complete', 'content': answer}
