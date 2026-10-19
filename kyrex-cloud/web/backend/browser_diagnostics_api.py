"""Owner-scoped, durable read-only tests on the existing dispatch queue."""
import hashlib
import asyncio
import uuid
import json
import re

from fastapi import APIRouter, HTTPException, Request

import bots
import browser_diagnostics as diagnostics
import browser_host_channel
import browser_hosts
import serve
from task_store import DuplicateTaskId

router = APIRouter(prefix='/api/browser-diagnostics', tags=['browser-diagnostics'])


def _services(request):
    import main
    return main.require_user(request), main.store


def _bot(owner, ident):
    bot = bots.get_bot(ident)
    if not bot or bot.get('owner') != owner:
        raise HTTPException(404, 'Browser Bot not found')
    if not serve.is_browser_bot_policy(bot.get('policy')):
        raise HTTPException(409, 'Choose a Bot with browser read permissions.')
    return bot


def _failed(row):
    result = row.get('result')
    if isinstance(result, str):
        try: result = json.loads(result)
        except ValueError: result = {}
    return row.get('status') == 'failed' or isinstance(result, dict) and result.get('status') == 'error'


@router.get('')
def status(request: Request):
    owner, store = _services(request)
    options = []
    manager = browser_host_channel.default_manager()
    for bot in bots.list_bots():
        if bot.get('owner') != owner or not serve.is_browser_bot_policy(bot.get('policy')):
            continue
        host = browser_hosts.host_for(owner, bot['id'])
        ch = manager.channel_for(host.host_id) if host else None
        options.append({'id': bot['id'], 'name': bot.get('name') or bot['id'],
                        'host_id': host.host_id if host else '',
                        'available': bool(host and host.is_available()),
                        'connected': bool(ch and ch.authenticated and not ch.closed),
                        'host_build': getattr(ch, 'host_build', 'unknown')})
        options[-1]['reconnect_available'] = bool(ch and getattr(ch, 'browser_reconnect', False))
    rows = store.list_browser_dispatches(owner, limit=50)
    reports = [diagnostics.report(row) for row in rows
               if diagnostics.is_probe(row.get('task_text')) or _failed(row)]
    return {'bots': options, 'reports': reports[:10], 'cloud_build': diagnostics.build_version()}


def _reconnect_channel(owner, bot_id):
    bot = _bot(owner, bot_id)
    if bot.get('status') != 'running': raise HTTPException(409, 'Start your Browser Bot first.')
    host = browser_hosts.host_for(owner, bot_id)
    channel = browser_host_channel.default_manager().channel_for(host.host_id) if host else None
    if not channel or channel.closed or not getattr(channel, 'browser_reconnect', False):
        raise HTTPException(409, 'Private sign-in recovery is not configured on this Browser Host.')
    return channel


@router.get('/reconnect/{bot_id}')
async def reconnect_status(bot_id: str, request: Request):
    owner, _ = _services(request)
    channel = _reconnect_channel(owner, bot_id)
    spec = {'request_id': uuid.uuid4().hex, 'owner': owner, 'bot_id': bot_id,
            'action': 'status', 'url': '', 'lease': ''}
    return await asyncio.to_thread(channel.reconnect_browser, spec)


@router.post('/reconnect')
async def reconnect(request: Request):
    owner, _ = _services(request)
    try: body = await request.json()
    except ValueError: raise HTTPException(400, 'Invalid private sign-in request.') from None
    if (not isinstance(body, dict) or set(body) != {'bot_id', 'url', 'action', 'lease', 'request_id'}
            or body.get('action') not in ('start', 'end')
            or not isinstance(body.get('bot_id'), str) or not 0 < len(body['bot_id']) <= 128
            or not isinstance(body.get('url'), str) or not isinstance(body.get('lease'), str)
            or not isinstance(body['request_id'], str) or not re.fullmatch(r'[a-f0-9-]{32,36}', body['request_id'])):
        raise HTTPException(400, 'Choose a Browser Bot and private sign-in action.')
    channel = _reconnect_channel(owner, body['bot_id'])
    # Bind a repeated acknowledgement to the exact owner, host, Bot and
    # payload. Model task text never reaches this authenticated UI route.
    identity = json.dumps([owner, body['request_id']], sort_keys=True)
    spec = {'request_id': hashlib.sha256(identity.encode()).hexdigest()[:32],
            'owner': owner, **{k: body[k] for k in ('bot_id', 'url', 'action', 'lease')}}
    import browser_reconnect_contract as contract
    try: contract.command(spec)
    except (ValueError, TypeError): raise HTTPException(400, 'Invalid private sign-in request.') from None
    if spec['action'] == 'start':
        import browser_operator
        bot = _bot(owner, body['bot_id'])
        if not browser_operator.domain_allowed(spec['url'], bot.get('browser_allowlist') or [])[0]:
            raise HTTPException(403, 'This website is outside your Browser Bot permissions.')
    return await asyncio.to_thread(channel.reconnect_browser, spec)


@router.get('/{reference}')
def result(reference: str, request: Request):
    owner, store = _services(request)
    row = store.get_browser_dispatch(reference) if diagnostics.REFERENCE.fullmatch(reference) else None
    if row is None or row.get('owner') != owner:
        raise HTTPException(404, 'Browser diagnostic not found')
    return diagnostics.report(row)


@router.post('', status_code=202)
async def start(request: Request):
    owner, store = _services(request)
    try: body = await request.json()
    except ValueError: raise HTTPException(400, 'Invalid browser test request.') from None
    if not isinstance(body, dict) or set(body) != {'bot_id', 'url', 'request_id'}:
        raise HTTPException(400, 'Choose a Browser Bot and website to test.')
    if not isinstance(body['bot_id'], str) or not isinstance(body['request_id'], str) or not re.fullmatch(r'[a-f0-9-]{32,36}', body['request_id']):
        raise HTTPException(400, 'Invalid browser test request.')
    bot = _bot(owner, body['bot_id'])
    try: wire = diagnostics.wire(body['url'])
    except ValueError as exc: raise HTTPException(400, str(exc)) from None
    host = browser_hosts.host_for(owner, bot['id'])
    ident = 'bd-' + hashlib.sha256((owner + ':' + body['request_id']).encode()).hexdigest()[:16]
    # Repeated HTTP acknowledgements observe the same saved attempt. They
    # never recreate or execute it, even after expiry or a changed binding.
    existing = store.get_browser_dispatch(ident)
    if existing:
        if existing['owner'] != owner or existing['bot_id'] != bot['id'] or existing['task_text'] != wire:
            raise HTTPException(409, 'This test request already has different details.')
        return diagnostics.report(existing)
    stage = {'stage': 'connection', 'state': 'passed' if host and host.is_available() else 'failed',
             'cloud_build': diagnostics.build_version()}
    live = browser_host_channel.default_manager().channel_for(host.host_id) if host else None
    stage['host_build'] = getattr(live, 'host_build', 'unknown')
    code = ''
    if bot.get('status') != 'running': code = 'bot_inactive'
    elif not host: code = 'host_unbound'
    elif not host.is_available(): code = 'host_offline'
    else:
        allowed, _ = __import__('browser_operator').preflight(wire, bot.get('browser_allowlist') or [])
        if not allowed: code = 'site_permissions'
        channel = browser_host_channel.default_manager().channel_for(host.host_id)
        if not code and channel and not channel.browser_diagnostics: code = 'host_update_required'
    try:
        store.submit_browser_dispatch(
            owner=owner, bot_id=bot['id'], host_id=host.host_id if host else '',
            task_id='browser-test-' + ident, task_text=wire,
            dispatch_id=ident, timeout=diagnostics.TIMEOUT,
            initial_error=code or None, initial_progress=[{diagnostics.ACTION: stage}])
    except DuplicateTaskId:
        raise HTTPException(409, 'This test request already has different details.') from None
    return diagnostics.report(store.get_browser_dispatch(ident))
