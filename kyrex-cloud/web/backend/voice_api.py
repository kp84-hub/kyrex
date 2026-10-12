"""One voice account for browser, Android and Windows; reuse ordinary Chat execution."""
import asyncio
import json
import os
import re
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response
from starlette.concurrency import run_in_threadpool
import chat_service
from chat_api import _drive_stream, _with_chat_keepalive
from voice_devices import VoiceDevices
from voice_speech import VoiceSpeech, SpeechError

router = APIRouter()


def owner(request):
    from connections_api import _require_user
    return _require_user(request)


def same_origin(request):
    # Cookie-authenticated mutations cannot be initiated from another origin.
    origin = request.headers.get('origin')
    if origin:
        source = urlsplit(origin)
        if source.scheme not in ('http', 'https') or source.netloc.lower() != request.headers.get('host', '').lower():
            raise HTTPException(403, 'Open Voice settings from your Kyrex Chat page.')


def phone(request):
    return identity(request)[:2]


def identity(request):
    auth = request.headers.get('authorization', '')
    if auth:
        try:
            user, cid, device = VoiceDevices().resolve_device(auth[7:] if auth.startswith('Bearer ') else '')
            return user, cid, 'device:' + device
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from None
    same_origin(request)
    user = owner(request)
    client = request.headers.get('x-voice-client', '')
    if not re.fullmatch('[a-f0-9]{32}', client):
        raise HTTPException(400, 'Start voice in Kyrex Chat to identify this browser session.')
    return user, voice_conversation(user), 'browser:' + client


def speech_owner(request):
    if request.headers.get('authorization'):
        return identity(request)[0]
    same_origin(request)
    return owner(request)


def voice_conversation(user):
    registry = chat_service.bots.load_bots()
    import serve
    matches = [bot for bot in registry.values() if bot.get('owner') == user
               and bot.get('status') == 'running' and serve.coordinator_granted(bot)]
    if len(matches) != 1:
        raise HTTPException(409, 'Start one owned Overwatcher before using voice.')
    existing = VoiceDevices().view(user).get('conversation_id')
    conv = chat_service.get_conversation(user, existing) if existing else None
    if conv and conv.get('bot_id') == matches[0]['id']:
        return existing
    cid = chat_service.create_conversation(user, title='Voice · The Overwatcher', bot_id=matches[0]['id'])['conversation_id']
    VoiceDevices().remember(user, cid)
    return cid


async def read_bytes(request, limit):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise HTTPException(413, 'Voice request is too large.')
    return bytes(raw)


async def body(request, limit=12000):
    raw = await read_bytes(request, limit)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Invalid voice request.') from None
    if not isinstance(value, dict):
        raise HTTPException(400, 'Expected a voice request object.')
    return value


def lease_identity(request):
    user, cid, client = identity(request)
    secret = request.headers.get('x-voice-lease', '')
    if not secret:
        raise HTTPException(426, 'Update Kyrex Voice to v0.4, or start voice in Kyrex Chat.')
    return user, cid, client, secret


def require_lease(request):
    user, cid, client, secret = lease_identity(request)
    try:
        VoiceDevices().check(user, client, secret)
    except ValueError as exc:
        raise HTTPException(409, str(exc), headers={'X-Voice-Reason': getattr(exc, 'reason', 'unavailable')}) from None
    return user, cid, client, secret


@router.get('/api/connections/voice')
def view(request: Request):
    user = owner(request)
    return JSONResponse({**VoiceDevices().view(user), 'settings': VoiceSpeech().view(user), 'client_version': '0.4'}, headers={'Cache-Control': 'no-store'})


@router.post('/api/connections/voice/connect')
async def connect(request: Request):
    same_origin(request)
    user = owner(request)
    raw = await read_bytes(request, 12000)
    try:
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            raise ValueError()
        result = VoiceDevices().begin(user, voice_conversation(user), data.get('name', 'Android phone'), data.get('platform', 'android'))
    except ValueError:
        raise HTTPException(400, 'Choose an Android or Windows device name of up to 60 characters.') from None
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/api/connections/voice/disconnect')
def disconnect(request: Request):
    same_origin(request)
    VoiceDevices().revoke(owner(request))
    return {'paired': False}


@router.post('/api/connections/voice/devices/{device_id}/disconnect')
def disconnect_device(device_id: str, request: Request):
    same_origin(request)
    VoiceDevices().revoke(owner(request), device_id)
    return {'disconnected': True}


@router.get('/api/voice/settings')
def settings(request: Request):
    return JSONResponse(VoiceSpeech().view(speech_owner(request)), headers={'Cache-Control': 'no-store'})


@router.post('/api/connections/voice/settings')
async def save_settings(request: Request):
    same_origin(request)
    user = owner(request)
    data = await body(request, 32000)
    try:
        result = VoiceSpeech().configure(user, data)
    except SpeechError as exc:
        raise HTTPException(400, str(exc)) from None
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/api/connections/voice/preview')
async def preview(request: Request):
    same_origin(request)
    user = owner(request)
    try:
        audio = await run_in_threadpool(VoiceSpeech().speak, user, "Hi, I'm Kyrex. This is your saved reply voice.")
    except SpeechError as exc:
        raise HTTPException(400, str(exc)) from None
    return Response(audio, media_type='audio/wav', headers={'Cache-Control': 'no-store'})


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
    user, cid, _ = identity(request)
    conv = chat_service.get_conversation(user, cid)
    if not conv:
        raise HTTPException(409, 'Voice conversation was removed. Open Voice in Chat again.')
    return {'conversation_id': cid, 'title': conv.get('title'), 'messages': conv.get('messages', [])[-4:],
            'settings': VoiceSpeech().view(user), 'client_version': '0.4'}


@router.post('/api/voice/listen')
async def listen(request: Request):
    user, _, client = identity(request)
    data = await body(request)
    name = data.get('name', 'Browser')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
        raise HTTPException(400, 'Choose a device name of up to 60 characters.')
    try:
        result = VoiceDevices().claim(user, client, name.strip(), data.get('takeover') is True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/api/voice/heartbeat')
def heartbeat(request: Request):
    # One atomic check/renew allows a delayed heartbeat to recover the same
    # lease. Command and speech routes continue to require an unexpired lease.
    user, _, client, secret = lease_identity(request)
    try:
        return VoiceDevices().check(user, client, secret, renew=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc), headers={'X-Voice-Reason': getattr(exc, 'reason', 'unavailable')}) from None


@router.post('/api/voice/stop')
def stop(request: Request):
    user, _, client = identity(request)
    VoiceDevices().release(user, client, request.headers.get('x-voice-lease', ''))
    return {'stopped': True}


@router.post('/api/voice/transcribe')
async def transcribe(request: Request):
    user, _, _, _ = require_lease(request)
    if request.headers.get('content-type', '').split(';')[0] != 'audio/wav':
        raise HTTPException(415, 'Record the command as WAV audio.')
    audio = await read_bytes(request, 1_000_000)
    try:
        result = await run_in_threadpool(VoiceSpeech().transcribe, user, audio)
    except SpeechError as exc:
        raise HTTPException(400, str(exc)) from None
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/api/voice/speech')
async def speech(request: Request):
    user, _, _, _ = require_lease(request)
    data = await body(request)
    try:
        audio = await run_in_threadpool(VoiceSpeech().speak, user, data.get('text'))
    except SpeechError as exc:
        raise HTTPException(400, str(exc)) from None
    return Response(audio, media_type='audio/wav', headers={'Cache-Control': 'no-store'})


@router.post('/api/voice/chat')
async def chat(request: Request):
    user, cid, client, secret = require_lease(request)
    data = await body(request)
    text = data.get('message')
    request_id = data.get('request_id')
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise HTTPException(400, 'Speak a request of up to 4000 characters.')
    if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
        raise HTTPException(400, 'A voice request identity is required.')
    conv = chat_service.get_conversation(user, cid)
    if not conv or not conv.get('bot_id'):
        raise HTTPException(409, 'Voice conversation was removed. Open Voice in Chat again.')
    try:
        VoiceDevices().reserve(user, client, secret, request_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    async def stream():
        complete = False
        try:
            frames = chat_service.stream_chat(user, cid, text.strip(), asyncio.Event(), request_id=request_id)
            async for frame in _with_chat_keepalive(_drive_stream(frames, request_id, cid)):
                yield frame
            complete = True
        finally:
            if complete:
                VoiceDevices().finish(user, request_id)
    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
