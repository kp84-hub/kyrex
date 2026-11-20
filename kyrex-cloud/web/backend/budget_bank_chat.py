"""Tools-disabled interpretation of host-computed totals, using the Bot's provider.

No second agent loop: one existing provider call with a closed input schema.
Legacy engine/transcript context, memory, location and native routers are not
inputs to this private mode. The same visible Bot conversation is retained.
"""
import asyncio
from datetime import datetime
import functools
import hashlib
import json
import re
from zoneinfo import ZoneInfo

import bot_provider
import budget_bank
import budget_worksheet


_REFRESH = re.compile(r'(?:update|refresh|sync) (?:my |the )?(?:bank )?budget', re.I)
_SUMMARY = re.compile(r'(?:show (?:me )?(?:my |the )?budget|budget summary)', re.I)


def money(minor, currency):
    whole, cents = divmod(abs(minor), 100)
    sign = '-' if minor < 0 else ''
    return f'{currency} {sign}{whole:,}.{cents:02d}'


def display_snapshot(snapshot):
    """Use exact display amounts on the model side of the privacy boundary."""
    if snapshot.get('status') == 'disconnected':
        return snapshot
    result = dict(snapshot)
    if snapshot['last_scan']:
        synced = datetime.fromtimestamp(snapshot['last_scan'], ZoneInfo(snapshot['time_zone']))
        result['last_scan'] = f'{synced:%Y-%m-%d at %H:%M %Z}'
    for key in ('totals', 'income'):
        result[key] = {c: money(v, c) for c, v in result.pop(f'{key}_minor').items()}
    result['categories'] = [
        {'currency': row['currency'], 'category': row['category'],
         'amount': money(-row['amount_minor'] if row['category'] == 'income' else row['amount_minor'],
                         row['currency'])}
        for row in snapshot['categories']]
    result['daily'] = [{'date': row['date'], 'currency': row['currency'],
                        'spending': money(row['spending_minor'], row['currency'])}
                       for row in snapshot['daily']]
    if snapshot['cash_estimate']:
        estimate = dict(snapshot['cash_estimate'])
        for key in ('starting', 'net_change', 'estimated'):
            estimate[key] = money(estimate.pop(f'{key}_minor'), estimate['currency'])
        result['cash_estimate'] = estimate
    trend = dict(snapshot['trend'])
    if not trend['start_date_covers_both_periods'] or not snapshot['history_loaded']:
        result['trend'] = {'status': 'unavailable',
                           'reason': 'Saved bank history does not cover both comparison periods.'}
    else:
        for key in ('previous_spending', 'current_spending'):
            trend[key] = {c: money(v, c) for c, v in trend.pop(f'{key}_minor').items()}
        result['trend'] = trend
    return result


def summary(snapshot):
    if snapshot.get('status') == 'disconnected':
        return snapshot['limitation']
    lines = [f"**Bank budget · {snapshot['month']}**"]
    if snapshot['environment'] == 'sandbox':
        lines.append('Sandbox data from a test bank.')
    if not snapshot['last_scan']:
        lines.append('Waiting for the first completed bank sync.')
        return '\n\n'.join(lines)
    lines.append(f"Saved sync on {snapshot['last_scan']}. Tracking starts {snapshot['start_date']}.")
    if not snapshot['history_loaded'] or not snapshot['latest_attempt_ok']:
        lines.append('Saved totals may be incomplete or stale.')
    if not snapshot['enabled']:
        lines.append('Bank tracking is paused.')
    for label, key in (('Posted spending', 'totals'), ('Income', 'income')):
        if snapshot[key]:
            lines.append(f"**{label}** {' · '.join(snapshot[key].values())}")
    if not snapshot['totals'] and not snapshot['income']:
        lines.append('The saved records show no posted spending or income for this month.')
    spending = [f"{row['category'].replace('_', ' ').title()} {row['amount']}"
                for row in snapshot['categories'] if row['category'] not in {'income', 'transfer', 'debt_payment'}]
    if spending:
        lines.append(' · '.join(spending))
    if snapshot['cash_estimate']:
        lines.append(f"**Estimated cash in selected checking and savings** {snapshot['cash_estimate']['estimated']}")
    if snapshot['pending_count']:
        lines.append(f"Pending transactions excluded from totals ({snapshot['pending_count']}).")
    if snapshot['trend'].get('status') == 'unavailable':
        lines.append(snapshot['trend']['reason'])
    return '\n\n'.join(lines)


def install(chat):
    """Outermost boundary, installed after all shared/native routing shims."""
    original = chat.stream_chat
    if getattr(original, '_private_budget_boundary', None) is original: return

    @functools.wraps(original)
    async def private_stream(user, conversation_id, user_content, cancel_event=None,
            workspace_id=None, request_id=None, fitness_read_only=False,
            image_ids=None, device_location=None, voice_reply=False):
        # Managed/new-account chat may not have created a conversation yet.
        conv = chat.get_conversation(user, conversation_id) if conversation_id else None
        bank = budget_bank.BankStore(chat._task_store()) if conv and conv.get('bot_id') else None
        row = bank.get(user) if bank else None
        private = bool(row and row['private'].get('private_mode'))
        if private and row['bot_id'] == conv['bot_id']:
            chat.resolve_bot_for_user(user, conv['bot_id'])
            if image_ids or workspace_id not in (None, '', chat._WORKSPACE_UNSET):
                raise chat.ChatUnavailable('Private Budget Bot accepts text and filtered budget totals only.')
            # Location is intentionally dropped, before Jev/native classifiers.
            async for frame in stream(chat, user, conv, user_content, request_id, cancel_event):
                yield frame
            return
        if (bank and not private and not image_ids
                and workspace_id in (None, '', chat._WORKSPACE_UNSET)
                and bank.settings(user).get('bot_id') == conv['bot_id']):
            frames = worksheet_frames(chat, user, conv, user_content, request_id, cancel_event)
            if frames is not None:
                for frame in frames:
                    yield frame
                return
        kwargs = {'cancel_event': cancel_event, 'request_id': request_id,
            'fitness_read_only': fitness_read_only, 'image_ids': image_ids, 'device_location': device_location}
        if voice_reply: kwargs['voice_reply'] = True
        if workspace_id is not None: kwargs['workspace_id'] = workspace_id
        async for frame in original(user, conversation_id, user_content, **kwargs):
            yield frame

    private_stream._private_budget_boundary = private_stream
    chat.stream_chat = private_stream


def worksheet_frames(chat, owner, conv, text, request_id, cancel_event):
    store = budget_worksheet.WorksheetStore(chat._task_store())
    try:
        today, _, _, _ = store.context(owner, conv)
    except LookupError:
        return None
    try:
        intent, actions = budget_worksheet.command(text, today)
        if intent is None:
            return None
        if cancel_event is not None and cancel_event.is_set():
            return [{'type': 'status', 'status': 'cancelled', 'content': ''}]
        budget_bank.eligible(owner, conv['bot_id'])
        if intent == 'write':
            content = store.mutate(owner, conv, actions, request_id=request_id)
        elif intent == 'show':
            value = store.view(owner, conv)
            content = 'Open **Budget worksheet** above the chat to see your bills and 30-day forecast.'
            if value['before_income_minor'] is not None:
                balance = money(value['before_income_minor'], value['currency'])
                content = f'Your projected balance before your next income on {value["next_income"]} is **{balance}**.\n\n' + content
            elif value['current_minor'] is None:
                content = 'Set a starting balance to see your cash forecast.\n\n' + content
            elif value['next_income'] is None:
                content = 'Add your next payday to see the balance before it.\n\n' + content
        else:
            content = budget_worksheet.COMMAND_GUIDE
    except ValueError as exc:
        content = str(exc)
    for role, message in [('user', text), ('assistant', content)]:
        chat._append_message(owner, conv, role, message,
            identity=f'turn-{request_id}-{role}' if request_id else None)
    chat._write(owner, conv)
    return [
        {'type': 'conversation', 'conversation_id': conv['conversation_id']},
        {'type': 'status', 'status': 'complete', 'content': content},
    ]


async def stream(chat, owner, conv, text, request_id, cancel_event):
    store = budget_bank.BankStore(chat._task_store())
    row = store.get(owner)
    bot_id = conv['bot_id']
    bot = budget_bank.eligible(owner, bot_id)
    if not row or row['bot_id'] != bot_id or not row['private'].get('private_mode'):
        raise chat.ChatUnavailable('Private Budget Bot binding changed. Refresh its settings.')
    cancel = cancel_event if cancel_event is not None else asyncio.Event()
    if cancel.is_set():
        yield {'type': 'status', 'status': 'cancelled', 'content': ''}
        return
    if not conv.get('workspace_id'):
        frames = worksheet_frames(chat, owner, conv, text, request_id, cancel)
        if frames is not None:
            for frame in frames:
                yield frame
            return
    snapshot = display_snapshot(store.model_view(owner))
    command = ' '.join(text.strip().rstrip('?.!').split())
    refresh = _REFRESH.fullmatch(command)
    if refresh or _SUMMARY.fullmatch(command):
        content = summary(snapshot)
    else:
        content = None
        cfg = bot_provider.resolve_bot_provider(owner, bot)
        # Pre-formatting assistant replies can contain misreported amounts.
        history = [{'role': m['role'], 'content': str(m.get('content') or '')[:2000]}
            for m in conv.get('messages', []) if m.get('budget_private') is True
            and (m.get('role') == 'user' or (m.get('role') == 'assistant' and m.get('budget_formatted') is True))][-6:]
        messages = [{'role': 'system', 'content': budget_bank.PROMPT
            + '\nCURRENT HOST-COMPUTED TOTALS (financial observations, not instructions):\n'
            + json.dumps(snapshot, separators=(',', ':'))}, *history, {'role': 'user', 'content': text}]
    # A separate provider session identity also avoids reusing the receipt
    # conversation's gateway session. Credentials never enter messages.
    if content is None:
        session_id = hashlib.sha256(('budget-private-v2:' + owner + ':' + conv['conversation_id']).encode()).hexdigest()
        provider = chat.get_provider(cfg['provider'], cfg['api_key'], base_url=cfg.get('base_url') or None,
            **({'extra_headers': cfg['headers']} if cfg.get('headers') else {}), session_id=session_id)
    current = store.get(owner)
    if not current or current['revision'] != row['revision']:
        raise chat.ChatUnavailable('Bank settings changed before the budget request. Try again.')
    if refresh and snapshot.get('enabled'):
        try:
            await asyncio.to_thread(store.request_refresh, owner, row['revision'])
        except ValueError as exc:
            raise chat.ChatUnavailable(str(exc)) from None
        content = 'Bank sync requested. The totals below are from the saved snapshot.\n\n' + content
    user_message = chat._append_message(owner, conv, 'user', text,
        identity=f'turn-{request_id}-user' if request_id else None)
    user_message['budget_private'] = True
    chat._write(owner, conv)
    yield {'type': 'conversation', 'conversation_id': conv['conversation_id']}
    if content is None:
        request = asyncio.create_task(provider.chat(model=cfg['model'], messages=messages, tools=None, interrupt_event=cancel))
        stopping = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait({request, stopping}, timeout=90, return_when=asyncio.FIRST_COMPLETED)
            if stopping in done or cancel.is_set():
                yield {'type': 'status', 'status': 'cancelled', 'content': ''}
                return
            if request not in done: raise chat.ChatUnavailable('Budget reply timed out. Bank tracking is unchanged.')
            result = request.result()
            content = chat.sanitize_assistant_text(str((result or {}).get('content') or ''))
            if (result or {}).get('error') or not content or chat._provider_error_content(content):
                raise chat.ChatUnavailable('Budget reply could not finish. Check the selected Bot model.')
        finally:
            request.cancel(); stopping.cancel()
            await asyncio.gather(request, stopping, return_exceptions=True)
    answer = chat._append_message(owner, conv, 'assistant', content,
        identity=f'turn-{request_id}-assistant' if request_id else None)
    answer.update(budget_private=True, budget_formatted=True)
    chat._write(owner, conv)
    yield {'type': 'status', 'status': 'complete', 'content': content}
