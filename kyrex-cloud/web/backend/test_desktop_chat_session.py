"""Native PKCE handoff to the existing durable, owner-scoped Chat session."""
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.requests import Request

os.environ.setdefault('GITHUB_CLIENT_ID', 'test-client')
os.environ.setdefault('GITHUB_CLIENT_SECRET', 'test-secret')
os.environ.setdefault('WEB_ALLOWED_GITHUB_USERNAME', 'allowed-user')
sys.path.insert(0, os.path.dirname(__file__))
import main
from web_sessions import SessionStore


def request(credential=None, cookie=None):
    headers = []
    if credential: headers.append((b'authorization', ('Bearer ' + credential).encode()))
    if cookie: headers.append((b'cookie', ('session=' + cookie).encode()))
    return Request({'type': 'http', 'headers': headers})


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'sessions', SessionStore(secret='inert-test-secret', path=tmp_path / 'sessions.db'))
    for name in ['desktop_access_tokens', 'desktop_refresh_tokens', 'desktop_transactions', 'desktop_handoffs', 'oauth_states']:
        monkeypatch.setattr(main, name, {})


def test_chat_pkce_flow_still_checks_owner_state_redirect_and_one_time_code(monkeypatch):
    # The existing GitHub confidential-client exchange and independent PKCE
    # handoff remain authoritative. No embedded GitHub/Google sign-in is used.
    verifier = 'an-inert-verifier-for-controlled-native-tests'
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    response = main.desktop_start('native-state', main.CHAT_DESKTOP_REDIRECT_URI, challenge, request=SimpleNamespace(base_url='https://cloud.example/'))
    page = response.body.decode()
    target = json.loads(re.search(r'window.location.replace\((".*?")\)', page).group(1))
    query = parse_qs(urlparse(target).query)
    assert query['redirect_uri'][0].endswith('/auth/callback')
    assert 'code_verifier' not in page
    monkeypatch.setattr(main, '_github_user', lambda *args: 'alice')
    callback = main.callback('github-code', query['state'][0], SimpleNamespace(base_url='https://cloud.example/'))
    destination = urlparse(callback.headers['location'])
    assert destination.scheme == 'kyrex-chat'
    params = parse_qs(destination.query)
    assert params['state'] == ['native-state']
    async def exchange(value):
        async def body(): return {'code': params['code'][0], 'redirect_uri': main.CHAT_DESKTOP_REDIRECT_URI, 'code_verifier': value}
        return await main.desktop_exchange(SimpleNamespace(json=body))
    with pytest.raises(main.HTTPException): asyncio.run(exchange('wrong-verifier'))
    token = asyncio.run(exchange(verifier))
    assert token['username'] == 'alice'
    with pytest.raises(main.HTTPException): asyncio.run(exchange(verifier))
    session = main.desktop_session(request(token['access_token']))
    cookies = SimpleCookie(); cookies.load(session.headers['set-cookie'])
    assert main.get_session_user(request(cookie=cookies['session'].value)) == 'alice'


def test_cookie_only_is_rejected_and_one_desktop_family_mints_only_one_session():
    main.sessions['browser-cookie'] = 'bob'
    with pytest.raises(main.HTTPException): main.desktop_session(request(cookie='browser-cookie'))
    tokens = main._new_desktop_tokens('alice')
    other = main._new_desktop_tokens('bob')
    response = main.desktop_session(request(tokens['access_token'], cookie='browser-cookie'))
    assert response.status_code == 204 and response.body == b''
    assert response.headers['cache-control'] == 'no-store'
    cookies = SimpleCookie(); cookies.load(response.headers['set-cookie']); session = cookies['session']
    assert session['secure'] and session['httponly'] and session['samesite'] == 'lax'
    assert session['path'] == '/' and not session['domain']
    assert main.sessions[session.value] == 'alice'
    assert main.sessions['browser-cookie'] == 'bob', 'another browser/account is unchanged'
    assert main._bearer_user(request(tokens['access_token'])) is None
    assert main._bearer_user(request(other['access_token'])) == 'bob'
    assert main.desktop_refresh_tokens[main._token_hash(tokens['refresh_token'])]['revoked']
    with pytest.raises(main.HTTPException): main.desktop_session(request(tokens['access_token']))
    # Same persistent session implementation as the website, across restarts.
    reopened = SessionStore(secret='inert-test-secret', path=main.sessions.path)
    assert reopened[session.value] == 'alice'


def test_expired_or_revoked_bearer_cannot_create_browser_session():
    for field, value in [('expires_at', 0), ('revoked', True)]:
        token = main._new_desktop_tokens('alice')['access_token']
        main.desktop_access_tokens[main._token_hash(token)][field] = value
        with pytest.raises(main.HTTPException) as failure: main.desktop_session(request(token))
        assert failure.value.status_code == 401
