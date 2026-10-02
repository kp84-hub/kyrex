"""Owner-authenticated web pairing/read routes; legacy uploads stay isolated."""
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import device_messages
import web_messages
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
            raise HTTPException(413, 'SMS snapshot is too large')
    try:
        data = json.loads(chunks)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid JSON')
    if not isinstance(data, dict):
        raise HTTPException(400, 'Expected an object')
    return data


@router.post('/api/connections/messages/connect')
def connect(request: Request):
    web_call(web_messages.WebMessages().rpc, owner(request), 'connect')
    return {'authorization_url': '/api/connections/messages/setup'}


@router.post('/api/connections/messages/pair')
async def pair(request: Request):
    data = await body(request)
    return {'upload_token': call(store().redeem, data.get('pairing_code'))}


@router.post('/api/connections/messages/sync')
async def sync(request: Request):
    auth = request.headers.get('authorization', '')
    if not auth.startswith('Bearer ') or not 30 <= len(auth[7:]) <= 100:
        raise HTTPException(401, 'Phone upload credential required')
    data = await body(request)
    return call(store().sync, auth[7:], data.get('messages'))


@router.get('/api/connections/messages/search')
def search(request: Request, q: str = Query('', max_length=200), max_results: int = Query(10, ge=1, le=20)):
    result = web_call(web_messages.WebMessages().rpc, owner(request), 'read', {'query': q})
    result['messages'] = result.get('messages', [])[:max_results]
    return result


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
