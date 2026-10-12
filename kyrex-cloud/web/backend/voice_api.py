"""Native voice sends text to the existing Overwatcher Chat stream."""
import asyncio
import json
import re
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import chat_service
from chat_api import _drive_stream, _with_chat_keepalive
from voice_devices import VoiceDevices

router = APIRouter()


def owner(request):
    from connections_api import _require_user
    return _require_user(request)


def phone(request):
    auth = request.headers.get('authorization', '')
    try:
        return VoiceDevices().resolve(auth[7:] if auth.startswith('Bearer ') else '')
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from None


def voice_conversation(user):
    registry = chat_service.bots.load_bots()
    import serve
    matches = [bot for bot in registry.values() if bot.get('owner') == user
               and bot.get('status') == 'running' and serve.coordinator_granted(bot)]
    if len(matches) != 1:
        raise HTTPException(409, 'Start one owned Overwatcher before linking voice.')
    existing = VoiceDevices().view(user).get('conversation_id')
    conv = chat_service.get_conversation(user, existing) if existing else None
    if conv and conv.get('bot_id') == matches[0]['id']:
        return existing
    return chat_service.create_conversation(user, title='Voice · The Overwatcher', bot_id=matches[0]['id'])['conversation_id']


async def body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 12000:
            raise HTTPException(413, 'Voice request is too large.')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid voice request.') from None
    if not isinstance(value, dict):
        raise HTTPException(400, 'Expected a voice request object.')
    return value


@router.get('/api/connections/voice')
def view(request: Request):
    return JSONResponse(VoiceDevices().view(owner(request)), headers={'Cache-Control': 'no-store'})


@router.post('/api/connections/voice/connect')
def connect(request: Request):
    user = owner(request)
    return JSONResponse(VoiceDevices().begin(user, voice_conversation(user)), headers={'Cache-Control': 'no-store'})


@router.post('/api/connections/voice/disconnect')
def disconnect(request: Request):
    VoiceDevices().revoke(owner(request))
    return {'paired': False}


@router.post('/api/voice/pair')
async def pair(request: Request):
    data = await body(request)
    try:
        result = VoiceDevices().redeem(data.get('pairing_code'))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.get('/api/voice/session')
def session(request: Request):
    user, cid = phone(request)
    conv = chat_service.get_conversation(user, cid)
    if not conv:
        raise HTTPException(409, 'Voice conversation was removed. Pair again.')
    return {'conversation_id': cid, 'title': conv.get('title'), 'messages': conv.get('messages', [])[-4:]}


@router.post('/api/voice/chat')
async def chat(request: Request):
    user, cid = phone(request)
    data = await body(request)
    text = data.get('message')
    request_id = data.get('request_id')
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise HTTPException(400, 'Speak a request of up to 4000 characters.')
    if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
        raise HTTPException(400, 'A voice request identity is required.')
    conv = chat_service.get_conversation(user, cid)
    if not conv or not conv.get('bot_id'):
        raise HTTPException(409, 'Voice conversation was removed. Pair again.')
    # The phone cannot choose an owner, bot, conversation, provider or workspace.
    # Client disconnect ends only this viewer; durable tasks recover in Chat.
    async def stream():
        frames = chat_service.stream_chat(user, cid, text.strip(), asyncio.Event(), request_id=request_id)
        async for frame in _with_chat_keepalive(_drive_stream(frames, request_id, cid)):
            yield frame
    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
