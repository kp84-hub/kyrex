"""Native capabilities reuse real Voice/Messages grants and enforce session boundaries."""
import base64
import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_android_chat_health import api, ORIGIN
import android_chat_api
import android_native_grants
import messages_api
import voice_api
import device_messages
from voice_devices import VoiceDevices


@pytest.fixture
def native(api, tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(voice_api, 'voice_conversation', lambda user: 'voice-'+user)
    app = FastAPI()
    app.include_router(android_chat_api.router)
    app.include_router(voice_api.router)
    app.include_router(messages_api.router)
    client = TestClient(app, base_url=ORIGIN)
    return client, api[2]


def begin(client, capability, cookie='alice-cookie'):
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state = secrets.token_urlsafe(32)
    client.cookies.set('session', cookie)
    result = client.post('/api/android/native/authorize', headers={'Origin': ORIGIN}, json={
        'challenge': challenge, 'state': state, 'capability': capability, 'expected_owner': cookie.removesuffix('-cookie')})
    assert result.status_code == 200, result.text
    assert result.headers['cache-control'] == 'no-store'
    return {'capability': capability, 'code': result.json()['code'], 'verifier': verifier}


@pytest.mark.parametrize('capability', ['voice', 'messages'])
def test_proof_bound_owner_scoped_grant_and_replay(native, capability):
    client, sessions = native
    data = begin(client, capability)
    client.cookies.clear()
    assert client.post('/api/android/native/exchange', json={**data, 'verifier': 'x'*43}).status_code == 400
    result = client.post('/api/android/native/exchange', json=data)
    assert result.status_code == 200, result.text
    assert result.json()['owner'] == 'alice'
    assert result.headers['cache-control'] == 'no-store'
    assert client.post('/api/android/native/exchange', json=data).status_code == 400
    token = result.json()['device_token' if capability == 'voice' else 'upload_token']
    assert token.encode() not in android_native_grants.NativeGrants().path.read_bytes()
    if capability == 'voice':
        assert VoiceDevices().resolve(token) == ('alice', 'voice-alice')
        assert client.post('/api/voice/listen', headers={'Authorization':'Bearer '+token}, json={'name':'Kyrex','takeover':True}).status_code == 200
    else:
        assert client.post('/api/connections/messages/sync', headers={'Authorization':'Bearer '+token}, json={'messages':[]}).status_code == 200
        assert not device_messages.MessagesStore().view('bob')['paired']
    sessions.pop('alice-cookie')
    if capability == 'voice':
        assert client.get('/api/voice/settings', headers={'Authorization':'Bearer '+token}).status_code == 401
    else:
        for path, body in [('sync', {'messages':[]}), ('device/poll', {'allow_send':False}), ('device/heartbeat',{'state':'ready'}), ('device/ack',{})]:
            assert client.post('/api/connections/messages/'+path, headers={'Authorization':'Bearer '+token}, json=body).status_code == 401


@pytest.mark.parametrize('capability', ['voice', 'messages'])
def test_different_capability_cannot_redeem_or_use_the_grant(native, capability):
    client, _ = native
    data = begin(client, capability)
    other = 'messages' if capability == 'voice' else 'voice'
    assert client.post('/api/android/native/exchange', json={**data,'capability':other}).status_code == 400
    token_result = client.post('/api/android/native/exchange', json=data).json()
    token = token_result.get('device_token') or token_result['upload_token']
    with pytest.raises(ValueError): android_native_grants.check_native(token, other)


def test_concurrent_redemption_issues_one_device(native):
    client, _ = native
    data = begin(client, 'voice')
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: client.post('/api/android/native/exchange', json=data).status_code, range(2)))
    assert sorted(results) == [200,400]
    assert len(VoiceDevices().view('alice')['devices']) == 1


@pytest.mark.parametrize('change', ['session', 'expiry', 'origin'])
def test_changed_handoff_cannot_issue_native_credential(native, monkeypatch, change):
    client, sessions = native
    data = begin(client,'messages')
    if change == 'session': sessions.pop('alice-cookie')
    elif change == 'expiry':
        with android_native_grants.NativeGrants().db() as db: db.execute('UPDATE handoffs SET expires=0')
    else: monkeypatch.setenv('KYREX_ANDROID_CHAT_ORIGIN','https://changed.example')
    assert client.post('/api/android/native/exchange',json=data).status_code == 400
    assert not device_messages.MessagesStore().view('alice')['paired']


def test_messages_migration_preserves_snapshot_and_receipts_and_disables_sending(native):
    import messages_send
    client, _ = native
    store = device_messages.MessagesStore()
    old = store.redeem(store.begin('alice')['pairing_code'])
    store.sync(old, [{'id':'one','sender':'School','conversation':'School','body':'Retain this saved text'}])
    with store._db() as db:
        db.execute('INSERT INTO message_sends VALUES (?,?,?,?,?,?,?,?)', ('receipt',device_messages._owner('alice'),device_messages._hash(old),'unknown','sealed-receipt',0,None,None))
    result = client.post('/api/android/native/exchange', json=begin(client,'messages'))
    assert result.status_code == 200, result.text
    token = result.json()['upload_token']
    assert store.search('alice')['messages'][0]['body'] == 'Retain this saved text'
    with store._db() as db: assert db.execute('SELECT state FROM message_sends WHERE id=?',('receipt',)).fetchone()[0] == 'unknown'
    assert not store.view('alice')['send_enabled']
    with pytest.raises(device_messages.MessagesError): store.sync(old, [])
    assert messages_send.SendQueue().poll(token,False)['command'] is None


@pytest.mark.parametrize('state',['send_queued','sending'])
def test_messages_migration_waits_for_confirmed_pending_effects(native,state):
    client, _ = native
    store = device_messages.MessagesStore()
    old = store.redeem(store.begin('alice')['pairing_code'])
    with store._db() as db:
        db.execute('INSERT INTO message_sends VALUES (?,?,?,?,?,?,?,?)', ('pending',device_messages._owner('alice'),device_messages._hash(old),state,'sealed-pending',10**12,1,None))
    result = client.post('/api/android/native/exchange', json=begin(client,'messages'))
    assert result.status_code == 409
    store.heartbeat(old,'ready')
    with store._db() as db: assert db.execute('SELECT state FROM message_sends WHERE id=?',('pending',)).fetchone()[0] == state


def test_native_consent_requires_current_owner_and_origin(native):
    client,_ = native
    client.cookies.set('session','bob-cookie')
    body = {'challenge':'c'*43,'state':'s'*43,'capability':'voice','expected_owner':'alice'}
    assert client.post('/api/android/native/authorize',headers={'Origin':ORIGIN},json=body).status_code == 409
    assert client.post('/api/android/native/authorize',headers={'Origin':'https://foreign.example'},json=body).status_code == 403


@pytest.mark.parametrize('capability', ['voice','messages'])
def test_missing_native_binding_cannot_downgrade_to_legacy_auth(native,capability):
    client,_ = native
    result = client.post('/api/android/native/exchange',json=begin(client,capability)).json()
    token = result.get('device_token') or result['upload_token']
    assert token.startswith('kyrex_native_v1_')
    with android_native_grants.NativeGrants().db() as db: db.execute('DELETE FROM bindings')
    with pytest.raises(ValueError): android_native_grants.check_native(token,capability)
