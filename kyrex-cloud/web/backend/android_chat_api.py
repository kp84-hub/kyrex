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
    return no_store({'health': True, 'origin': configured_origin, 'package': PACKAGE})


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
    if (set(body) != {'challenge', 'state'} or not isinstance(challenge, str) or
            len(challenge) != 43 or not PROOF.fullmatch(challenge) or
            not isinstance(state, str) or not PROOF.fullmatch(state)):
        raise HTTPException(400, detail='Invalid Android connection. Open Health setup again.')
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
