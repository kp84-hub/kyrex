"""Native Health handoff; browser cookies never leave the system browser."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import connections_api
from fitness_connections import FitnessError

router = APIRouter()
PACKAGE = 'com.kyrex.chat'
PROOF = re.compile(r'[A-Za-z0-9_-]{43,128}\Z')


def origin():
    value = os.environ.get('KYREX_ANDROID_CHAT_ORIGIN', '').strip().rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise HTTPException(503, detail='The Kyrex Android app is not configured on this server.')
    return value


def no_store(value):
    return JSONResponse(value, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


@router.get('/api/android/config')
def config():
    try:
        configured_origin = origin()
    except HTTPException:
        return no_store({'health': False})
    return no_store({'health': True, 'voice': True, 'messages': True,
                     'origin': configured_origin, 'package': PACKAGE, 'app_version': '0.2'})


@router.get('/.well-known/assetlinks.json')
def asset_links():
    origin()
    fingerprints = [f.strip().upper() for f in os.environ.get('KYREX_ANDROID_CERT_SHA256', '').split(',') if f.strip()]
    if not fingerprints or any(not re.fullmatch(r'(?:[0-9A-F]{2}:){31}[0-9A-F]{2}', f) for f in fingerprints):
        raise HTTPException(404, detail='Android app verification is not configured.')
    return no_store([{'relation': ['delegate_permission/common.handle_all_urls'],
                     'target': {'namespace': 'android_app', 'package_name': PACKAGE,
                                'sha256_cert_fingerprints': fingerprints}}])


@router.post('/api/android/health/authorize')
async def authorize(request: Request):
    configured_origin = origin()
    # An authenticated tap on this exact deployment; bearer clients and foreign
    # origins cannot mint browser-session grants. No owner or return URL input.
    if request.headers.get('origin') != configured_origin:
        raise HTTPException(403, detail='Open Health setup in your Kyrex Chat.')
    import main
    cookie = request.cookies.get('session')
    owner = main.get_session_user(request)
    if not owner or not cookie:
        raise HTTPException(401, detail='Sign in to Kyrex Chat to connect this phone.')
    body = await connections_api._fitness_body(request)
    challenge, state = body.get('challenge'), body.get('state')
    if (set(body) != {'challenge', 'state', 'expected_owner'} or not isinstance(challenge, str) or
            len(challenge) != 43 or not PROOF.fullmatch(challenge) or
            not isinstance(state, str) or not PROOF.fullmatch(state)):
        raise HTTPException(400, detail='Invalid Android connection. Open Health setup again.')
    if body.get('expected_owner') != owner:
        raise HTTPException(409, detail='Your signed-in account changed. Open Health setup again.')
    result = connections_api._fitness().begin(owner, 'samsung_health', android={
        'challenge': challenge, 'origin': configured_origin,
        'session': main.sessions.reference(cookie), 'owner': owner})
    # The app also checks its private state. Only the proof-bound, two-minute,
    # one-use code enters the callback; no token, cookie, or health data does.
    return no_store({'code': result['pairing_code'], 'state': state,
                     'expires_at': result['expires_at']})


@router.post('/api/android/health/exchange')
async def exchange(request: Request):
    configured_origin = origin()
    body = await connections_api._fitness_body(request)
    verifier, code = body.get('verifier'), body.get('code')
    if (set(body) != {'verifier', 'code'} or not isinstance(verifier, str) or
            not PROOF.fullmatch(verifier) or not isinstance(code, str) or len(code) != 43):
        raise HTTPException(400, detail='Invalid Android connection. Open Health setup again.')
    import main
    try:
        return no_store(connections_api._fitness().pair(code, android_verifier=verifier,
                        android_origin=configured_origin,
                        session_owner=main.sessions.owner_for_reference))
    except FitnessError:
        raise HTTPException(400, detail='Android connection expired or changed. Open Health setup again.') from None


def issue_native(owner, capability):
    if capability == 'voice':
        import voice_api
        devices = voice_api.VoiceDevices()
        code = devices.begin(owner, voice_api.voice_conversation(owner), 'Kyrex Android', 'android')
        return devices.redeem(code['pairing_code'])
    import messages_api
    messages = messages_api.store()
    code = messages.begin(owner)
    return {'upload_token': messages.redeem(code['pairing_code'], preserve_history=True)}


@router.post('/api/android/native/authorize')
async def authorize_native(request: Request):
    configured_origin = origin()
    if request.headers.get('origin') != configured_origin:
        raise HTTPException(403, detail='Open device settings in your Kyrex Chat.')
    import main
    owner = main.get_session_user(request)
    if not owner: raise HTTPException(401, detail='Sign in to connect this phone.')
    data = await connections_api._fitness_body(request)
    if (set(data) != {'capability', 'challenge', 'state', 'expected_owner'} or
            data.get('capability') not in ('voice', 'messages') or
            not isinstance(data.get('challenge'), str) or len(data['challenge']) != 43 or not PROOF.fullmatch(data['challenge']) or
            not isinstance(data.get('state'), str) or not PROOF.fullmatch(data['state'])):
        raise HTTPException(400, detail='Invalid device connection.')
    if data['expected_owner'] != owner:
        raise HTTPException(409, detail='Your signed-in account changed. Open device setup again.')
    from android_native_grants import NativeGrants
    return no_store({**NativeGrants().begin(owner, data['capability'], data['challenge'],
                        main.sessions.reference(request.cookies['session']), configured_origin), 'state': data['state']})


@router.post('/api/android/native/exchange')
async def exchange_native(request: Request):
    configured_origin = origin()
    data = await connections_api._fitness_body(request)
    if (set(data) != {'capability', 'code', 'verifier'} or data.get('capability') not in ('voice', 'messages') or
            not isinstance(data.get('code'), str) or len(data['code']) != 43 or not PROOF.fullmatch(data['code']) or
            not isinstance(data.get('verifier'), str) or not PROOF.fullmatch(data['verifier'])):
        raise HTTPException(400, detail='Invalid device connection.')
    from android_native_grants import NativeGrants
    import main
    try:
        return no_store(NativeGrants().redeem(data['code'], data['verifier'], data['capability'], configured_origin,
                                              main.sessions, issue_native))
    except ValueError as exc:
        if str(exc) == 'A confirmed message is pending. Check its outcome before moving Messages to Kyrex.':
            raise HTTPException(409, detail='Complete the pending confirmed message before changing phones.') from None
        raise HTTPException(400, detail='Device connection expired or changed. Open device setup again.') from None
