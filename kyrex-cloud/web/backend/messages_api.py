"""Owner-authenticated phone pairing and snapshot reads; legacy browser routes remain compatible."""
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import device_messages
import web_messages
import messages_send
from connectors import ConnectorConfigError

router = APIRouter()


def owner(request):
    from connections_api import _require_user
    return _require_user(request)


def store():
    return device_messages.MessagesStore()


def call(fn, *args):
    try:
        return fn(*args)
    except ConnectorConfigError:
        raise HTTPException(503, 'Messages encryption is not configured')
    except device_messages.MessagesError as exc:
        raise HTTPException(400, str(exc))


async def body(request):
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > 1100000:
            raise HTTPException(413, 'Message snapshot is too large')
    try:
        data = json.loads(chunks)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid JSON')
    if not isinstance(data, dict):
        raise HTTPException(400, 'Expected an object')
    return data


@router.post('/api/connections/messages/connect')
def connect(request: Request):
    result = call(store().begin, owner(request))
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/api/connections/messages/pair')
async def pair(request: Request):
    data = await body(request)
    return JSONResponse({'upload_token': call(store().redeem, data.get('pairing_code'))}, headers={'Cache-Control': 'no-store'})


@router.post('/api/connections/messages/sync')
async def sync(request: Request):
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer ') or not 30 <= len(auth[7:]) <= 100:
        raise HTTPException(401, 'Phone upload credential required')
    data = await body(request)
    return JSONResponse(call(store().sync, auth[7:], data.get('messages')), headers={'Cache-Control': 'no-store'})


@router.get('/api/connections/messages/search')
def search(request: Request, q: str = Query('', max_length=200), max_results: int = Query(10, ge=1, le=20)):
    who = owner(request)
    if store().view(who)['paired']:
        return JSONResponse(call(store().search, who, q, max_results), headers={'Cache-Control': 'no-store'})
    result = web_call(web_messages.WebMessages().rpc, who, 'read', {'query': q})
    result['messages'] = result.get('messages', [])[:max_results]
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/api/connections/messages/disconnect')
def disconnect(request: Request):
    who = owner(request)
    call(store().disconnect, who)  # revoke legacy phone uploads too
    return web_messages.WebMessages().disconnect(who)


@router.get('/api/connections/messages/bridge.py')
def bridge():
    # Public source only; pairing information is never embedded in this file.
    return FileResponse(Path(__file__).resolve().parents[2] / 'messages_phone.py', filename='messages_phone.py', media_type='text/x-python')


def web_call(fn, *args):
    try:
        return fn(*args)
    except web_messages.WebMessagesError as exc:
        raise HTTPException(503, str(exc))


@router.post('/api/connections/messages/browser')
async def browser(request: Request):
    who = owner(request)
    data = await body(request)
    action = data.get('action')
    if action not in {'screen', 'input', 'finish'}:
        raise HTTPException(400, 'Unsupported pairing action')
    import asyncio
    input_data = data.get('data') or {}
    if not isinstance(input_data, dict):
        raise HTTPException(400, 'Invalid pairing input')
    result = await asyncio.to_thread(web_call, web_messages.WebMessages().rpc, who, action, input_data)
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.get('/api/connections/messages/setup')
def setup(request: Request):
    owner(request)
    return HTMLResponse(Path(__file__).with_name('messages_setup.html').read_text(), headers={
        'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'self'; img-src data:; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'",
        'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})


def phone_credential(request):
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer ') or not 30 <= len(auth[7:]) <= 100:
        raise HTTPException(401, 'Phone credential required')
    return auth[7:]


def private_json(data):
    return JSONResponse(data, headers={'Cache-Control': 'no-store'})


@router.get('/api/connections/messages/status')
def messages_status(request: Request):
    view = call(store().view, owner(request))
    return private_json({key: view[key] for key in ('paired', 'connected', 'synced_at', 'phone')})


@router.post('/api/connections/messages/device/heartbeat')
async def device_heartbeat(request: Request):
    credential = phone_credential(request)
    data = await body(request)
    return private_json(call(store().heartbeat, credential, data.get('state')))


@router.post('/api/connections/messages/device/poll')
async def device_poll(request: Request):
    credential = phone_credential(request)
    data = await body(request)
    return private_json(call(messages_send.SendQueue().poll, credential, data.get('allow_send')))


@router.post('/api/connections/messages/device/ack')
async def device_ack(request: Request):
    credential = phone_credential(request)
    data = await body(request)
    return private_json(call(messages_send.SendQueue().acknowledge, credential, data.get('id'), data.get('action'), data.get('result')))


@router.post('/api/connections/messages/sends')
async def prepare_send(request: Request):
    who = owner(request)
    data = await body(request)
    key = data.get('request_id')
    if key is not None and (not isinstance(key, str) or len(key)>200):
        raise HTTPException(400, 'Invalid request id')
    return private_json(call(messages_send.SendQueue().start, who, data.get('recipient'), data.get('text'), None, key))


@router.get('/api/connections/messages/sends/{send_id}')
def send_status(request: Request, send_id: str):
    return private_json(call(messages_send.SendQueue().get, owner(request), send_id))


@router.post('/api/connections/messages/sends/{send_id}/decision')
async def send_decision(request: Request, send_id: str):
    who = owner(request)
    data = await body(request)
    return private_json(call(messages_send.SendQueue().decide, who, send_id, data.get('decision')))


@router.get('/api/connections/messages/sends/{send_id}/reply')
def send_reply(request: Request, send_id: str):
    who = owner(request)
    job = call(messages_send.SendQueue().get, who, send_id)
    if job['state'] not in {'accepted', 'unknown', 'sending'}:
        raise HTTPException(400, 'Send the message before checking its reply')
    return private_json(call(messages_send.latest_reply, who, '', {'conversation_id': job['conversation_id'], 'send_id': send_id}))
