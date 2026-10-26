"""Text-only supervised chat through Kyrex's existing provider adapters.

This pilot has no agent loop, tools, connectors, memory, images or filesystem.
The guardian grants one exact provider/model; no keys are copied to the child.
"""
import asyncio
import json
import time
import threading

import account_store as accounts
import bot_provider
import chat_service
from kyrex.providers import get_provider

_active = set()
_active_lock = threading.Lock()


def configuration(user):
    child = accounts.get(user)
    if not accounts.managed_enabled() or not child or child['kind'] != 'managed' or not child['active']:
        raise chat_service.ChatUnavailable('This managed account is paused or unavailable.')
    guardian = accounts.get(child['guardian'])
    if not guardian or not guardian['active']:
        raise chat_service.ChatUnavailable('The parent account is unavailable.')
    try:
        profile = child['profile']
        cfg = bot_provider.resolve_bot_provider(child['guardian'], {
            'id': 'managed-chat', 'provider_profile_id': profile.get('provider_profile_id'), 'model': profile.get('model')})
        allowed = json.loads(__import__('os').environ.get('KYREX_MANAGED_MODEL_ALLOWLIST', '[]'))
        if not isinstance(allowed, list) or [cfg['provider'], cfg['base_url'].rstrip('/'), cfg['model']] not in allowed:
            raise ValueError()
        return child, cfg
    except (bot_provider.BotProviderError, ValueError, KeyError):
        raise chat_service.ChatUnavailable('Your parent needs to choose an enabled learning model in Family settings.') from None


async def stream(user, conversation_id, text, cancel, request_id):
    with _active_lock:
        if user in _active:
            raise chat_service.ChatUnavailable('Your previous reply is still running. Wait for it or use Stop.')
        _active.add(user)
    try:
        async for frame in _stream_turn(user, conversation_id, text, cancel, request_id):
            yield frame
    finally:
        with _active_lock:
            _active.discard(user)


async def _stream_turn(user, conversation_id, text, cancel, request_id):
    child, cfg = configuration(user)
    if len(text) > 8000:
        raise chat_service.ChatUnavailable('Try a shorter message, up to 8,000 characters.')
    conv = chat_service.get_conversation(user, conversation_id) if conversation_id else None
    if conversation_id and conv is None:
        raise chat_service.ChatUnavailable('Conversation not found.')
    conv = conv or chat_service.create_conversation(user, chat_service._title_from(text))
    if conv.get('bot_id') or conv.get('workspace_id'):
        raise chat_service.ChatUnavailable('Managed learning chat cannot attach Bots or workspaces.')
    previous = next((m for m in conv['messages'] if m.get('id') == f'turn-{request_id}-assistant'), None)
    if previous:
        yield {'type': 'conversation', 'conversation_id': conv['conversation_id']}
        yield {'type': 'status', 'status': 'complete', 'content': previous['content']}
        return
    try:
        accounts.throttle('managed-turn:' + user, 5, 60)
        accounts.throttle('managed-day:' + user + ':' + time.strftime('%Y-%m-%d', time.gmtime()),
                          child['profile']['daily_messages'], 86400)
    except accounts.AccountError as exc:
        raise chat_service.ChatUnavailable(str(exc)) from None
    history = conv['messages'][-12:]
    messages = [{'role': 'system', 'content': (
        'You are Kyrex, a learning assistant for a young person in a parent-managed account. '
        'Their parent can review these conversations. Explain clearly and respectfully, ask '
        'questions that help them think, and match depth to the question. For coding, teach '
        'with small examples and encourage experimentation. Handle educational questions '
        'about history, health and growing up appropriately for a young learner. '
        'You have no tools, email, calendar, browser, files, execution or saved memory. '
        'Never claim to have read accounts, run code, sent messages or taken actions. '
        'Do not ask for passwords, API keys, home addresses or other unnecessary personal details. '
        'Do not encourage secrecy from trusted adults. If asked for unsafe instructions, '
        'offer a safe explanation or alternative.')}] + [
            {'role': m['role'], 'content': str(m.get('content', ''))[:8000]} for m in history
            if m.get('role') in ('user', 'assistant')]
    messages.append({'role': 'user', 'content': text})
    chat_service._append_message(user, conv, 'user', text, identity=f'turn-{request_id}-user')
    chat_service._write(user, conv)
    provider = get_provider(cfg['provider'], cfg['api_key'], base_url=cfg['base_url'],
        extra_headers=cfg.get('headers'), session_id=chat_service._opencode_session_for(user, conv))
    queue = asyncio.Queue()
    interrupt = asyncio.Event()
    size = 0

    def token(value):
        nonlocal size
        if value:
            size += len(value)
            if size > 24000: interrupt.set()
            else: queue.put_nowait(value)

    work = asyncio.create_task(provider.chat(model=cfg['model'], messages=messages,
        tools=None, stream_callback=token, interrupt_event=interrupt))
    yield {'type': 'conversation', 'conversation_id': conv['conversation_id']}
    try:
        last_check = 0
        deadline = time.monotonic() + 90
        while not work.done() or not queue.empty():
            if time.monotonic() >= deadline:
                raise chat_service.ChatUnavailable('The learning model took too long. Try again later.')
            if cancel.is_set():
                interrupt.set(); work.cancel()
                yield {'type': 'status', 'status': 'cancelled', 'content': ''}
                return
            if time.monotonic() - last_check > 1:
                configuration(user)  # Pause/revocation/model changes take effect during a turn.
                last_check = time.monotonic()
            try:
                chunk = await asyncio.wait_for(queue.get(), .25)
                yield {'type': 'delta', 'content': chunk}
            except asyncio.TimeoutError:
                pass
        reply = await work
        content = chat_service.sanitize_assistant_text(str((reply or {}).get('content') or ''))[:24000]
        if (reply or {}).get('error') or chat_service._provider_error_content(content):
            raise chat_service.ChatUnavailable('The learning model could not finish its reply. Try again later.')
        current, current_cfg = configuration(user)
        if current_cfg != cfg:
            raise chat_service.ChatUnavailable('Your parent changed the learning model. Start a new request.')
        chat_service._append_message(user, conv, 'assistant', content, identity=f'turn-{request_id}-assistant')
        chat_service._write(user, conv)
        yield {'type': 'status', 'status': 'complete', 'content': content}
    finally:
        interrupt.set()
        if not work.done(): work.cancel()
        await asyncio.gather(work, return_exceptions=True)
