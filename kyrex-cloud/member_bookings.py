"""Existing member bookings -> reviewed Calendar creates on CloudTaskStore.

This module cannot prepare or submit a workout booking. Browser credentials
stay in the enrolled profile; Calendar writes use the existing owner connector
and approval protocol. Provider event IDs make repeated creates idempotent.
"""
import calendar
import hashlib
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import member_bookings_schema as schema
import calendar_windows

class FlowError(ValueError):
    pass


PREFIX = schema.ACTION
_STUDIO = re.compile(r'\b(?:level\s*6|level six|l6|glofox)\b', re.I)
_BOOKED = re.compile(r'\b(?:booked|bookings?|reservations|signed up)\b', re.I)
_READ = re.compile(r'\b(?:what|which|show|list|check|verify|see|find|have)\b', re.I)
_CALENDAR = re.compile(r'\b(?:add|put|save|copy)\b.{0,100}\b(?:to|on|in)\s+(?:my\s+|the\s+)?calendar\b', re.I)
_MONTH = '|'.join((*calendar.month_name[1:], *calendar.month_abbr[1:]))
_DAY = re.compile(r'\b(?:\d{4}-\d{1,2}-\d{1,2}|(?:' + _MONTH
                  + r')\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:(?:,\s*|\s+)\d{4})?)\b', re.I)
_WEEKDAY = re.compile(r'\b(?:(this|next)\s+)?(' + '|'.join(calendar_windows.WEEKDAYS) + r')\b', re.I)
_RELATIVE = re.compile(r'\b(?:this week|next week|today|tomorrow)\b', re.I)


def read_window(text, current):
    """One owner-supplied day or existing bounded relative window; no model guesses."""
    dates, weekdays, relative = list(_DAY.finditer(text)), list(_WEEKDAY.finditer(text)), list(_RELATIVE.finditer(text))
    if (len(dates) > 1 or len(weekdays) > 1 or len(relative) > 1
            or relative and (dates or weekdays)
            or re.search(r'\b(?:between|through|until|before|after|since|last|yesterday)\b|\bfrom\b.+\bto\b', text, re.I)
            or re.search(r'\b(?:not|except|excluding|other than)\s+(?:(?:on|for)\s+)?(?:'
                         + _MONTH + '|' + '|'.join(calendar_windows.WEEKDAYS) + r'|today|tomorrow|\d)', text, re.I)
            or dates and re.match(r'\s*(?:[-–—/]|to\b|and\b|or\b)\s*\d', text[dates[0].end():], re.I)):
        raise FlowError('Use one date or a supported window, such as Wednesday, October 21 or next week.')
    if weekdays:
        _, start, _ = calendar_windows.window_bounds(weekdays[0][2].lower(), now=current)
        weekday_day = datetime.fromisoformat(start).date()
        if weekdays[0][1] and weekdays[0][1].lower() == 'next' and weekday_day == current.date():
            weekday_day += timedelta(days=7)
    if dates:
        if re.match(r'\s*,?\s*\d', text[dates[0].end():]):
            raise FlowError('Please give the full four-digit year, such as October 21, 2026.')
        key = calendar_windows.named_date_key(dates[0][0].replace('.', ''), now=current)
        if not key:
            raise FlowError('Please give a valid date, such as October 21, 2026 or 2026-10-21.')
        first = datetime.fromisoformat(key).date()
        if weekdays and (weekdays[0][2].lower() != calendar_windows.WEEKDAYS[first.weekday()]
                         or weekdays[0][1] and first != weekday_day):
            raise FlowError('The weekday and date do not match. Please give one date to check.')
    elif weekdays:
        first = weekday_day
    elif relative:
        key = relative[0][0].lower().replace(' ', '-')
        _, start, end = calendar_windows.window_bounds(key, now=current)
        first, last = datetime.fromisoformat(start).date(), datetime.fromisoformat(end).date()
        # Preserve the existing member-reader's remaining-days this-week window.
        if key == 'this-week': first = current.date()
        return schema.parse({'url': schema.URL, 'start': first.isoformat(), 'end': last.isoformat()})
    else:
        if re.search(r'\b(?:' + _MONTH + '|' + '|'.join(calendar_windows.WEEKDAYS)
                     + r'|last|yesterday)\b|\d{4}-\d|\b\d{1,2}/\d{1,2}', text, re.I):
            raise FlowError('Give one date, such as October 21 or 2026-10-21, or ask for upcoming bookings.')
        return schema.parse({'url': schema.URL, 'start': current.date().isoformat(),
                             'end': (current.date() + timedelta(days=30)).isoformat()})
    try:
        last = first + timedelta(days=1)
    except OverflowError:
        raise FlowError('Please give a date with a valid following day.') from None
    return schema.parse({'url': schema.URL, 'start': first.isoformat(), 'end': last.isoformat()})


def candidate(text):
    return bool(_BOOKED.search(text) and _READ.search(text) or _CALENDAR.search(text))


def latest_selection(store, owner, conv):
    """Only the latest native assistant result can bind a pronoun follow-up."""
    latest = next((m for m in reversed(conv.get('messages', []))
                   if m.get('role') == 'assistant' and str(m.get('content', '')).strip()), {})
    for task in reversed(store.tasks_for_conversation(conv['conversation_id'], owner)):
        if (task.get('executor_prefix') == PREFIX and task.get('status') == 'done'
                and latest.get('id') == 'task-' + task['task_id'] + '-result'):
            result = task.get('result') or {}
            if result.get('verified_bookings') and result.get('bookings'):
                return task
    return None


def request(text, conv, store, owner, *, now=None):
    """Conservative read/Calendar continuation; never steal a booking command."""
    text = str(text or '').strip().replace('’', "'")
    if (not text or len(text) > 2000
            or re.match(r"^(?:please\s+)?(?:don't|do not|never|stop)\b", text, re.I)
            or re.search(r'\b(?:cancel|delete|remove|reschedule|unbook|implement|debug|code|fix|email|mail)\b', text, re.I)
            or re.match(r'^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:book|reserve|schedule|sign me up)\b', text, re.I)):
        return None
    add = bool(_CALENDAR.search(text))
    if re.search(r"\b(?:don't|do not|never|not)\s+(?:add|put|save|copy)\b", text, re.I): add = False
    if not (add or _BOOKED.search(text) and _READ.search(text)): return None
    previous = [m.get('content', '') for m in conv.get('messages', [])[-6:] if m.get('role') == 'user']
    anchor = False
    for old in reversed(previous):
        if re.search(r'\b(?:hotel|flight|restaurant|table|dentist|doctor|email|mail)\b', old, re.I): break
        if _STUDIO.search(old) and (_BOOKED.search(old) or re.search(r'\bbook\b', old, re.I)):
            anchor = True
            break
        if not (_BOOKED.search(old) or _CALENDAR.search(old)): break
    source = latest_selection(store, owner, conv)
    explicit = bool(_STUDIO.search(text) and _BOOKED.search(text) and (_READ.search(text) or add))
    pronoun = bool(re.match(r'^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?'
                            r'(?:add|put|save|copy)\s+(?:those|these|them|that|this|it)\b', text, re.I))
    contextual = bool((_READ.search(text) and _BOOKED.search(text) and anchor)
                      or (add and (source and pronoun or anchor and _BOOKED.search(text))))
    if add and pronoun and anchor and not source:
        raise FlowError('Ask me to verify your Level 6 bookings first, then add that verified list to Calendar.')
    plain = bool((source or anchor) and _BOOKED.search(text) and _READ.search(text))
    if not (explicit or contextual or plain) and re.fullmatch(r"what(?:'s| is| do i have| have i) booked[?.!]?", text, re.I):
        raise FlowError('Which service should I check? For Level 6, ask “What have I booked at Level 6?”')
    if not (explicit or contextual or plain): return None
    current = calendar_windows.local_now(now).astimezone(schema.TZ)
    window = read_window(text, current)
    # Pronouns mean exactly the classes shown, not newly discovered bookings.
    selected = source if add and not explicit else None
    if selected: window = selected['result']['window']
    return {'operation': 'calendar' if add else 'read', 'window': schema.parse(window),
            'source_task': selected['task_id'] if selected else None}


def validate_task(value):
    if (not isinstance(value, dict) or set(value) != {'operation', 'window', 'source_task'}
            or value['operation'] not in {'read', 'calendar'}
            or value['source_task'] is not None and not re.fullmatch(r'task-[\w-]{1,100}', str(value['source_task']))
            or value['operation'] == 'read' and value['source_task']):
        raise FlowError('Invalid member bookings task.')
    schema.parse(value['window'])
    return value


def permitted(bot, operation):
    """Use existing policy matching; preserve explicit restrictions."""
    import policy
    decision = policy.evaluate(bot.get('policy') or {}, operation, 0)
    if decision['matched_rule'] is not None and decision['effective_tier'] != 0:
        raise FlowError('This Bot restricts the requested connected operation.')


def submit(user, bot, payload, store, conversation_id):
    import bots
    validate_task(payload)
    if bot.get('owner') != user or not bots.is_running(bot):
        raise FlowError('Use one of your running Bots to read member bookings.')
    permitted(bot, 'browser:read')
    return store.submit(session_key=bot['id'], bot_id=bot['id'], rift=bot.get('rift') or '',
        chat_id=user, executor_prefix=PREFIX, task_text=json.dumps(payload),
        conversation_id=conversation_id, resolve_bot=True)


def read(owner, window, task_id, store, *, expected_binding=None):
    # Reuse the same owner-isolated Browser Bot/profile selection as bookings.
    import sys
    from pathlib import Path
    backend = str(Path(__file__).parent / 'web' / 'backend')
    if backend not in sys.path: sys.path.insert(0, backend)
    import booking_chat
    import browser_host_bridge
    bot, host = booking_chat.browser(owner)
    binding = {'bot': bot['id'], 'host': host.host_id, 'allowlist': bot['browser_allowlist']}
    if expected_binding is not None and binding != expected_binding:
        raise FlowError('The Browser Bot or Host changed. Ask to verify your bookings again.')
    result, error = browser_host_bridge.request_browser_dispatch(owner=owner, bot_id=bot['id'],
        host_id=host.host_id, task_text=json.dumps({schema.ACTION: schema.parse(window)}),
        task_id=task_id, session_id=task_id, timeout=100, store=store, with_diagnostics=True)
    if error:
        import browser_diagnostics
        raise FlowError(browser_diagnostics.explain(error))
    if isinstance(result, dict) and isinstance(result.get('result'), dict): result = result['result']
    if not isinstance(result, dict) or result.get('mode') != schema.ACTION:
        raise FlowError('Update the Browser Host to enable reading existing member bookings.')
    if result.get('status') != 'no_changes' or result.get('complete') is not True or result.get('window') != window:
        raise FlowError(schema.failure_message(result.get('member_bookings_failure')))
    verified = schema.result(result.get('bookings'), result.get('identity'), window)
    return {**verified, 'browser_binding': binding}


def render(rows, window):
    last = (datetime.fromisoformat(window['end']) - timedelta(days=1)).date().isoformat()
    heading = f"Verified in Glofox · {len(rows)} booked class(es) · {window['start']} through {last} ET"
    if not rows: return heading + '\n\nNo confirmed classes were found in this window.'
    def line(row):
        start = datetime.fromtimestamp(row['start'], schema.TZ)
        end = datetime.fromtimestamp(row['end'], schema.TZ)
        title = re.sub(r'([\\`*_\[\]<>])', r'\\\1', row['title'])
        return f"- {start:%a, %b %d, %Y} · {start:%I:%M %p}–{end:%I:%M %p} ET — {title}"
    return heading + '\n\n' + '\n'.join(line(row) for row in rows)


def calendar_events(rows, owner, account):
    import cal_writer
    events = []
    for row in rows:
        start = datetime.fromtimestamp(row['start'], schema.TZ)
        end = datetime.fromtimestamp(row['end'], schema.TZ)
        if start.date() != end.date(): raise FlowError('An overnight class needs manual calendar review.')
        intent = cal_writer.build_intent(schema.STUDIO_LABEL + ' · ' + row['title'], start.date().isoformat(),
            start.strftime('%H:%M'), end.strftime('%H:%M'), all_day=False)
        event = cal_writer.to_google_event(intent)
        # Google accepts a caller-provided base32hex ID. Keep it identical for
        # the same owner/account/calendar/reservation across retries/devices.
        event['id'] = hashlib.sha256(json.dumps([owner, account, row['reference']], sort_keys=True).encode()).hexdigest()
        events.append(event)
    return events


def missing(events, existing):
    """Exact timed matches are duplicates; all-day workout labels are not."""
    if not isinstance(existing, list): raise FlowError('The calendar lookup was incomplete.')
    pending = []
    def stamp(value):
        try:
            parsed = datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00'))
            # Calendar intents carry a local wall time plus an explicit zone;
            # Google reads return an offset. Compare the same instant without
            # depending on the Cloud process's timezone.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(value.get('timeZone')))
            return parsed.timestamp()
        except (KeyError, ValueError, TypeError, AttributeError): return None
    for event in events:
        found = []
        for old in existing:
            if not isinstance(old, dict): raise FlowError('The calendar returned a malformed event.')
            if old.get('status') == 'cancelled': continue
            same_times = stamp(old.get('start')) == stamp(event['start']) and stamp(old.get('end')) == stamp(event['end'])
            if old.get('id') == event['id'] and (old.get('summary') != event['summary'] or not same_times):
                raise FlowError('An imported booking was edited in Calendar. Review it manually before importing again.')
            if old.get('id') == event['id'] or (same_times and old.get('summary') == event['summary']): found.append(old)
        if len(found) > 1: raise FlowError('Duplicate booked-class events already exist. Review your calendar first.')
        if not found: pending.append(event)
    return pending


def run_task(ctx, owner, text, task_id, conversation_id, send, *, on_progress=None,
             on_result=None, on_approval=None, on_approval_resolved=None):
    import bots
    import connectors
    import serve
    from task_store import CloudTaskStore
    store = CloudTaskStore()
    created = []
    write_attempted = False
    evidence = None
    payload = None
    def reply(message, *, failed=False):
        count = len(created)
        if not failed and evidence is not None and payload['operation'] == 'read': count = len(evidence['bookings'])
        result = {'status': 'error' if failed else 'no_changes', 'mode': PREFIX,
                  'final_response': message, 'count': count}
        if evidence is not None:
            result.update(verified_bookings=True, bookings=evidence['bookings'], window=evidence['window'],
                          identity=evidence['identity'], browser_binding=evidence['browser_binding'])
        if on_result: on_result(result)
        send(owner, message)
    try:
        payload = validate_task(json.loads(text))
        bot = bots.get_bot(ctx.bot_id)
        def active():
            task = store.get(task_id) or {}
            live = bots.get_bot(ctx.bot_id)
            if (ctx.bot_owner != owner or live.get('owner') != owner or not bots.is_running(live)
                    or task.get('chat_id') != owner or task.get('bot_id') != ctx.bot_id
                    or task.get('conversation_id') != conversation_id or task.get('executor_prefix') != PREFIX
                    or task.get('task_text') != text or task.get('status') != 'running' or task.get('cancel_requested')):
                raise FlowError('The member-bookings task is unavailable or stopped.')
            permitted(live, 'browser:read')
            return live
        active()
        if on_progress: on_progress({'stage': 'member_bookings', 'message': 'Reading your confirmed Glofox bookings (no booking writes).'})
        evidence = read(owner, payload['window'], task_id, store)
        active()
        rows = evidence['bookings']
        if payload['source_task']:
            source = store.get(payload['source_task']) or {}
            old = source.get('result') or {}
            if (source.get('chat_id') != owner or source.get('bot_id') != ctx.bot_id
                    or source.get('conversation_id') != conversation_id or source.get('status') != 'done'
                    or source.get('executor_prefix') != PREFIX or not old.get('verified_bookings')
                    or old.get('window') != payload['window'] or old.get('identity') != evidence['identity']
                    or old.get('browser_binding') != evidence['browser_binding']):
                raise FlowError('The saved booking selection changed. Ask what is booked again.')
            selected = old.get('bookings') or []
            if not selected or any(row not in rows for row in selected):
                raise FlowError('A selected class changed or is no longer booked. Ask what is booked again.')
            rows = selected
        if payload['operation'] == 'calendar':
            evidence = {**evidence, 'bookings': rows}
        if payload['operation'] == 'read' or not rows:
            reply(render(rows, payload['window']) + ('\n\nSay “add these to my calendar” to review calendar events.' if rows else ''))
            return
        if len(rows) > 25: raise FlowError('Choose a smaller booking window before adding more than 25 calendar events.')
        live = active(); permitted(live, 'cal:list'); permitted(live, 'cal:create')
        connection = connectors.default_store()
        def account():
            if (not connection.scope_granted(owner, connectors.GOOGLE_CALENDAR_READ_SCOPE)
                    or not connection.calendar_write_available(owner)):
                raise FlowError('Connect Google Calendar read and write access before adding bookings.')
            status = connection.status(owner)
            view = connection.account_view(owner)
            if not status.get('connected_at'):
                raise FlowError('The selected Google Calendar account could not be verified.')
            # Existing Calendar grants do not require a profile/email scope.
            # The encrypted owner's connection revision is authoritative;
            # tokeninfo's optional email is only a helpful review label.
            return {'email': view.get('email'), 'connected_at': status['connected_at'],
                    'calendar': connection.preferred_calendar(owner)}
        destination = account()
        events = calendar_events(rows, owner, destination)
        low, high = schema.bounds(payload['window'])
        reader = connection.calendar(owner)
        def pending():
            return missing(events, reader.events(time_min=datetime.fromtimestamp(low, schema.TZ).isoformat(),
                time_max=datetime.fromtimestamp(high, schema.TZ).isoformat(), max_results=100,
                calendar_id=destination['calendar'], require_complete=True))
        proposed = pending()
        if not proposed:
            reply('All verified booked classes are already on your calendar. No duplicates were added.')
            return
        detail = json.dumps({'account': destination['email'] or 'Your connected Google account', 'calendar': destination['calendar'],
                             'events': [{k: v for k, v in event.items() if k != 'id'} for event in proposed]},
                            ensure_ascii=False, indent=2)
        approved = serve._request_in_process_approval(ctx, owner, send, tier=1,
            summary=f'Add {len(proposed)} verified booked class(es) to Calendar', detail=detail,
            on_approval=on_approval, on_approval_resolved=on_approval_resolved)
        if not approved:
            reply('Calendar review declined or expired. No calendar events were added.')
            return
        active()
        if account() != destination: raise FlowError('The Google account or destination changed. Review a new calendar import.')
        fresh = read(owner, payload['window'], task_id, store, expected_binding=evidence['browser_binding'])
        if fresh['identity'] != evidence['identity'] or any(row not in fresh['bookings'] for row in rows):
            raise FlowError('A booking or member session changed during review. Review a new calendar import.')
        if pending() != proposed: raise FlowError('The calendar changed during review. Ask again for a fresh duplicate check.')
        writer = connection.calendar_writer(owner)
        for event in proposed:
            live = active(); permitted(live, 'cal:create')
            if account() != destination: raise FlowError('The Calendar account or destination changed before creation.')
            # Do not automatically replay a provider error/timeout. A fresh
            # request reconciles by exact ID and the same ID cannot create twice.
            write_attempted = True
            receipt = writer.create_event(event, expected_binding=destination)
            if receipt.get('id') != event['id']: raise FlowError('Calendar returned an unexpected event reference.')
            created.append(event['id'])
        reply(f'Added {len(created)} verified booked class(es) to your calendar. '
              f'Skipped {len(events) - len(proposed)} existing event(s). Your workout bookings were not changed.')
    except Exception as exc:
        # Adapter ValueErrors are fixed messages; never echo provider responses,
        # credentials, untrusted page text, URLs or exception details.
        detail = str(exc) if isinstance(exc, FlowError) else 'The connected service could not complete this check.'
        if payload and payload['operation'] == 'calendar':
            suffix = (f' Calendar events confirmed: {len(created)}. Check Calendar before trying again.'
                      if write_attempted else ' No calendar events were added.')
        else:
            suffix = ' Your bookings were not changed.'
        reply(detail + suffix, failed=True)
