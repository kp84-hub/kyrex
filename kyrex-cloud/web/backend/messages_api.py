"""Phone upload credentials have no Cloud read or account-management rights."""
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import FileResponse
import device_messages
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
    return call(store().begin, owner(request))


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
    return call(store().search, owner(request), q, max_results)


@router.post('/api/connections/messages/disconnect')
def disconnect(request: Request):
    call(store().disconnect, owner(request))
    return {'disconnected': True}


@router.get('/api/connections/messages/bridge.py')
def bridge():
    # Public source only; pairing information is never embedded in this file.
    return FileResponse(Path(__file__).resolve().parents[2] / 'messages_phone.py', filename='messages_phone.py', media_type='text/x-python')
