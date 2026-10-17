"""Real session + encrypted fitness store + HTTP routes; no provider account."""
import base64
import hashlib
import json
import secrets
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import android_chat_api
import connections_api
os.environ.setdefault('GITHUB_CLIENT_ID', 'test-client')
os.environ.setdefault('GITHUB_CLIENT_SECRET', 'test-secret')
os.environ.setdefault('WEB_ALLOWED_GITHUB_USERNAME', 'allowed-user')
os.environ.setdefault('KYREX_DATA_DIR', tempfile.mkdtemp(prefix='kyrex-android-tests-'))
import main
from fitness_connections import FitnessConnections
from web_sessions import SessionStore

ORIGIN = 'https://chat.example'


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET', 'android-health-test-only')
    monkeypatch.setenv('KYREX_ANDROID_CHAT_ORIGIN', ORIGIN)
    sessions = SessionStore(secret='android-sessions-test-only', path=tmp_path/'sessions.db')
    monkeypatch.setattr(main, 'sessions', sessions)
    store = FitnessConnections(tmp_path/'fitness.db')
    monkeypatch.setattr(connections_api, '_fitness', lambda: store)
    sessions['alice-cookie'] = 'alice'
    sessions['bob-cookie'] = 'bob'
    app = FastAPI()
    app.include_router(android_chat_api.router)
    app.include_router(connections_api.router)
    return TestClient(app, base_url=ORIGIN), store, sessions


def start(client, cookie='alice-cookie'):
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state = secrets.token_urlsafe(32)
    client.cookies.set('session', cookie)
    response = client.post('/api/android/health/authorize', headers={'Origin': ORIGIN},
                           json={'challenge': challenge, 'state': state})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['state'] == state
    return {'code': response.json()['code'], 'verifier': verifier}


def test_connect_without_copying_cookie_and_token_is_ingestion_only(api):
    client, store, sessions = api
    body = start(client)
    client.cookies.clear()
    result = client.post('/api/android/health/exchange', json=body)
    assert result.status_code == 200 and result.json()['owner'] == 'alice'
    assert result.headers['cache-control'] == 'no-store'
    token = result.json()['device_token']
    assert client.post('/api/connections/samsung_health/sync', headers={'Authorization': 'Bearer '+token},
                       json={'records': [], 'complete': True}).status_code == 200
    assert client.get('/api/connections/fitness/read', headers={'Authorization': 'Bearer '+token}).status_code == 401
    assert client.post('/api/android/health/exchange', json=body).status_code == 400
    raw = store.path.read_bytes()
    assert b'alice-cookie' not in raw and token.encode() not in raw and body['verifier'].encode() not in raw
    assert store.view('alice', 'samsung_health')['connected']
    assert not store.view('bob', 'samsung_health')['connected']


def test_intercepted_callback_cannot_use_legacy_exchange_or_wrong_proof(api):
    client, _, _ = api
    body = start(client)
    assert client.post('/api/connections/samsung_health/exchange', json={'pairing_code': body['code']}).status_code == 400
    assert client.post('/api/android/health/exchange', json={**body, 'verifier': 'x'*43}).status_code == 400
    assert client.post('/api/android/health/exchange', json=body).status_code == 200


@pytest.mark.parametrize('change', ['logout', 'expire', 'disconnect', 'new_pairing', 'origin'])
def test_handoff_invalidated_by_identity_grant_and_deployment_changes(api, monkeypatch, change):
    client, store, sessions = api
    body = start(client)
    if change == 'logout': sessions.pop('alice-cookie')
    elif change == 'expire': sessions._clock = lambda: 10**12
    elif change == 'disconnect': store.disconnect('alice', 'samsung_health')
    elif change == 'new_pairing': store.begin('alice', 'samsung_health')
    else: monkeypatch.setenv('KYREX_ANDROID_CHAT_ORIGIN', 'https://other.example')
    assert client.post('/api/android/health/exchange', json=body).status_code == 400


def test_two_minute_expiry_and_other_owner_cannot_change_bound_owner(api):
    client, store, _ = api
    body = start(client)
    client.cookies.set('session', 'bob-cookie')
    # A callback derives the owner from its proof-bound authorization, never
    # the cookie in the redemption request (native requests have no cookie).
    assert client.post('/api/android/health/exchange', json=body).json()['owner'] == 'alice'
    body = start(client)
    with store.db() as db: db.execute('UPDATE handoffs SET expires=0')
    assert client.post('/api/android/health/exchange', json=body).status_code == 400


def test_logout_revokes_android_upload_but_preserves_history_and_other_owner(api):
    client, store, sessions = api
    token = client.post('/api/android/health/exchange', json=start(client)).json()['device_token']
    bob = store.pair(store.begin('bob', 'samsung_health')['pairing_code'])['device_token']
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc)-timedelta(minutes=1)
    row = {'type': 'steps', 'id': 'retained-reading', 'origin': 'com.sec.android.app.shealth',
           'start': (end-timedelta(minutes=1)).isoformat(), 'end': end.isoformat(), 'count': 20}
    store.upload(token, [row], session_owner=sessions.owner_for_reference)
    sessions.pop('alice-cookie')
    assert client.post('/api/connections/samsung_health/sync', headers={'Authorization': 'Bearer '+token},
                       json={'records': []}).status_code == 400
    assert client.post('/api/connections/samsung_health/sync', headers={'Authorization': 'Bearer '+bob},
                       json={'records': []}).status_code == 200
    assert store.read('alice', 'samsung_health')['sources']['samsung_health']['records'][0]['id'] == 'retained-reading'


def test_signing_in_again_retires_previous_browser_session_and_device_grant(api, monkeypatch):
    from unittest.mock import patch
    client, store, sessions = api
    token = client.post('/api/android/health/exchange', json=start(client)).json()['device_token']
    monkeypatch.setattr(main, 'ALLOWED_USERNAME', 'bob')
    class Response:
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(self.value).encode()
    browser = TestClient(main.app, base_url=ORIGIN)
    browser.cookies.set('session', 'alice-cookie', domain='chat.example')
    state = main.create_oauth_state()
    with patch.object(main.urllib.request, 'urlopen', side_effect=[Response({'access_token': 'synthetic-provider-token'}), Response({'login': 'bob'})]):
        response = browser.get('/auth/callback', params={'code': 'synthetic-code', 'state': state}, follow_redirects=False)
    assert response.status_code == 307
    assert sessions.get('alice-cookie') is None
    assert sessions.get(browser.cookies.get('session')) == 'bob'
    assert client.post('/api/connections/samsung_health/sync', headers={'Authorization': 'Bearer '+token},
                       json={'records': []}).status_code == 400


def test_concurrent_exchange_has_one_winner(api):
    client, _, _ = api
    body = start(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post('/api/android/health/exchange', json=body).status_code, range(2)))
    assert sorted(results) == [200, 400]


@pytest.mark.parametrize('header', [None, 'https://foreign.example', 'null'])
def test_authorize_rejects_foreign_or_missing_origin(api, header):
    client, _, _ = api
    client.cookies.set('session', 'alice-cookie')
    assert client.post('/api/android/health/authorize', headers={'Origin': header} if header else {}, json={}).status_code == 403


def test_auth_body_origin_and_asset_configuration_are_fail_closed(api, monkeypatch):
    client, _, _ = api
    assert client.post('/api/android/health/authorize', headers={'Origin': ORIGIN}, json={}).status_code == 401
    client.cookies.set('session', 'alice-cookie')
    assert client.post('/api/android/health/authorize', headers={'Origin': ORIGIN}, json={'owner': 'bob'}).status_code == 400
    assert client.post('/api/android/health/authorize', headers={'Origin': ORIGIN}, content='x'*256001).status_code == 413
    assert client.get('/.well-known/assetlinks.json').status_code == 404
    fingerprint = ':'.join(['AB']*32)
    monkeypatch.setenv('KYREX_ANDROID_CERT_SHA256', fingerprint)
    assert client.get('/.well-known/assetlinks.json').json()[0]['target']['sha256_cert_fingerprints'] == [fingerprint]
    for value in ['', 'http://chat.example', 'https://chat.example/path', 'https://user@chat.example']:
        monkeypatch.setenv('KYREX_ANDROID_CHAT_ORIGIN', value)
        assert client.get('/api/android/config').json() == {'health': False}
        assert client.post('/api/android/health/authorize', json={}).status_code == 503


def test_session_reference_is_not_an_authentication_credential(api):
    client, _, sessions = api
    reference = sessions.reference('alice-cookie')
    assert sessions.owner_for_reference(reference) == 'alice'
    assert sessions.get(reference) is None
    assert sessions.reference('bob-cookie') != reference
    client.cookies.set('session', reference)
    assert client.post('/api/android/health/authorize', headers={'Origin': ORIGIN}, json={}).status_code == 401
