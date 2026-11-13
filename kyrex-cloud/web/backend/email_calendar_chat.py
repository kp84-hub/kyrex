"""Prepare an exact Calendar review from a fresh, explicitly requested email."""

import hashlib
import json
import re
from contextlib import aclosing

import cal_writer
import email_event
import serve
from job_contracts import JobContractError
from task_store import DuplicateTaskId


_HANDOFF = re.compile(
    r"(?P<lookup>.+?)(?:,?\s+(?:and(?:\s+then)?|then)\s+|[.;]\s+)"
    r"(?:add|put|schedule|save)\s+(?P<event>[^.!?;]{1,120}?)\s+"
    r"(?:to|on|in)\s+(?:my\s+|the\s+)?(?:Google\s+)?calendar"
    r"(?:,?\s+please)?[.!?]*", re.I)
_MAIL = re.compile(r'\be-?mail\b|\b(?:gmail|inbox|mailbox)\b', re.I)
_REFERENCE = re.compile(r'(?:it|this|that|the\s+(?:email|message|event|invite|appointment))', re.I)
_OTHER_ACTION = re.compile(
    r"\b(?:and|then)\s+(?:send|reply|delete|forward|archive|mark|add|put|schedule|save|create)\b"
    r"|\b(?:don't|do not|never)\b", re.I)
_CLOCK = re.compile(r'\b\d{1,2}(?::\d{2})?\s*[ap]\.?\s*m\b|\b\d{1,2}:\d{2}\b|\b(?:noon|midnight)\b', re.I)


def request(text):
    """Compile only an explicit read followed by one calendar create."""
    match = _HANDOFF.fullmatch(' '.join(str(text or '').split()))
    if not match or not _MAIL.search(match['lookup']) or _OTHER_ACTION.search(match['lookup']):
        return None
    command = serve.natural_gmail_command(match['lookup'])
    if not command:
        return None
    for prefix in ('gmail: search ', 'gmail: read ', 'gmail: latest '):
        if command.startswith(prefix):
            query = command[len(prefix):]
            break
    else:
        return None  # Do not search an unqualified inbox or pick its latest mail.
    if command.startswith('gmail: latest --limit '):
        return None
    topic = match['event'].strip()
    if _REFERENCE.fullmatch(topic):
        topic = '' if command.startswith('gmail: read id ') else serve._gmail_focus_anchor(query)
    else:
        topic = re.sub(r'^(?:the|a|an)\s+', '', topic, flags=re.I)
        if not re.fullmatch(r"[\w '&’\u2011-]+", topic) or re.search(r'\b(?:and|or)\b', topic, re.I):
            return None
        if not serve._gmail_focus_anchor(query):
            query += ' ' + serve._gmail_quote_phrase(topic)
    if command.startswith('gmail: read id '):
        return command, topic
    command = serve.canonical_gmail_task('gmail: read ' + query)
    return (command, topic) if command else None


def _intent(result, topic):
    if result.get('outcome') in {'unavailable', 'failed'}:
        return None, (result.get('final_response') or 'Email lookup was unavailable.') + ' No event was created.'
    selected = result.get('selected')
    if not isinstance(selected, dict) or not selected.get('id'):
        if result.get('count') == 0:
            return None, "I couldn’t find a matching email in your selected inboxes. No event was created."
        return None, ("I found more than one matching email. Read the one you want, then ask me to add it to your calendar.\n\n"
                      + (result.get('final_response') or '')[:3500])

    headers = selected.get('headers') or {}
    if selected.get('truncated'):
        return None, "I could only read part of the matching email. What date and time should I use?"
    body = selected.get('body') or ''
    facts = selected.get('event_facts') or {}
    title = None
    if topic:
        event = email_event.localize_target_event(body, topic, explicit_topic=True)
        names_topic = serve._gmail_names_topic
        if event['status'] == 'ambiguous':
            return None, f"I found multiple dates for {topic} in the email. Which date should I use?"
        if event['status'] == 'matched':
            body = event['text']
            title = event['title']
            facts = {}  # The explicitly named event takes precedence over the search topic.
        elif not names_topic(headers.get('Subject') or '', topic):
            return None, f"I found an email, but couldn’t verify the details for {topic}. What date and time should I use?"
        elif facts and not names_topic(facts.get('title') or '', topic):
            facts = {}
    if not facts:
        facts = email_event.extract_event_facts(subject=headers.get('Subject'),
            sender=headers.get('From'), date=headers.get('Date'), body=body, title=title)
    if re.search(r'\b(?:PT|PST|PDT|CT|CST|CDT|MT|MST|MDT|UTC|GMT)\b'
                 r'|(?i:\b(?:Pacific|Central|Mountain) Time\b)', body):
        return None, "The email specifies a different timezone. What time should I use in Eastern time?"
    if (not facts.get('all_day') and not (facts.get('start') and facts.get('end'))
            and _CLOCK.search(body)):
        return None, "The email mentions a time, but I couldn’t verify the full time range. What are the start and end times?"
    try:
        title, date, start, end, all_day = email_event.event_intent_args(facts)
        intent = cal_writer.build_intent(title, date, start, end, all_day=all_day)
        if facts.get('location') and not facts.get('location_ambiguous'):
            intent = cal_writer.validate_intent({**intent, 'location': facts['location']})
    except email_event.EmailEventError:
        return None, "The email doesn’t give me enough detail to prepare a Calendar review.\n\n" + email_event.render_details(facts)
    except cal_writer.CalendarWriterError as exc:
        return None, str(exc)
    return intent, ''


async def stream(chat, user, conv, text, parsed, owner_turn, assistant_turn, cancel, workspace_id):
    """Use only the fresh mail task's result and the existing Calendar approval."""
    bot = chat.resolve_bot_for_user(user, conv['bot_id'])
    if bot.get('owner') != user or (workspace_id is not chat._WORKSPACE_UNSET and str(workspace_id or '').strip()):
        raise chat.ChatUnavailable('Email-to-Calendar requests require your own Bot conversation.')
    if cancel is not None and cancel.is_set():
        yield {'type': 'status', 'status': 'cancelled', 'content': ''}
        return
    cid = conv['conversation_id']
    old_reply = next((m for m in conv['messages'] if assistant_turn and m['id'] == assistant_turn), None)
    if old_reply:
        yield {'type': 'conversation', 'conversation_id': cid}
        yield {'type': 'status', 'status': 'complete', 'content': old_reply['content']}
        return
    chat._append_message(user, conv, 'user', text, identity=owner_turn)
    chat._write(user, conv)
    yield {'type': 'conversation', 'conversation_id': conv['conversation_id']}
    store = chat._task_store()
    command, topic = parsed
    terminal = None
    intent = None
    if not chat.dev_bot._gmail_read_available(user):
        answer = 'Connect or select an email account in Connections to look up the event. No event was created.'
    else:
        read_id = ('email-calendar-read-' + hashlib.sha256(
            f'{user}:{cid}:{owner_turn}'.encode()).hexdigest()) if owner_turn else None
        try:
            task_id = chat.dev_bot.submit_gmail_task(user, bot, command, store=store,
                                                    conversation_id=cid, task_id=read_id)
        except DuplicateTaskId:
            task = store.get(read_id) or {}
            if (task.get('chat_id'), task.get('conversation_id'), task.get('bot_id'),
                    task.get('executor_prefix'), task.get('task_text')) != (user, cid, bot['id'], 'gmail', command):
                raise chat.ChatUnavailable('The existing email lookup could not be verified.')
            task_id = read_id
        except (chat.dev_bot.DevBotError, JobContractError) as exc:
            raise chat.ChatUnavailable(str(exc)) from exc
        async with aclosing(chat._stream_existing_bot_task(
                user, conv, store, task_id, cancel, mode='gmail')) as frames:
            async for frame in frames:
                if frame['type'] == 'status':
                    terminal = frame
                elif frame['type'] not in {'conversation', 'error'}:
                    yield frame
        if terminal and terminal['status'] == 'cancelled':
            yield terminal
            return
        task = store.get(task_id) or {}
        if terminal and terminal['status'] == 'complete' and isinstance(task.get('result'), dict):
            intent, answer = _intent(task['result'], topic)
        else:
            detail = (terminal or {}).get('message') or 'Check your selected email account in Connections.'
            answer = f'Email lookup could not be completed. {detail} No event was created.'
    if cancel is not None and cancel.is_set():
        yield {'type': 'status', 'status': 'cancelled', 'content': ''}
        return
    if intent is not None:
        if not chat.dev_bot._calendar_write_available(user):
            answer = 'I found the event. Enable Calendar write access in Connections to review it.\n\n' + cal_writer.payload_display(intent)
        else:
            try:
                task_id = chat.dev_bot.submit_email_calendar_task(
                    user, bot, json.dumps(intent), store=store, conversation_id=cid)
            except (chat.dev_bot.DevBotError, JobContractError) as exc:
                raise chat.ChatUnavailable(str(exc)) from exc
            async with aclosing(chat._stream_existing_bot_task(user, conv, store, task_id, cancel)) as frames:
                async for frame in frames:
                    if frame['type'] != 'conversation':
                        yield frame
            return
    conv = chat.get_conversation(user, cid) or conv
    reply = chat._append_message(user, conv, 'assistant', chat.sanitize_assistant_text(answer),
                                 identity=assistant_turn)
    chat._write(user, conv)
    yield {'type': 'status', 'status': 'complete', 'content': reply['content']}
