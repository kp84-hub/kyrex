"""Voice credentials are single-use paired, owner scoped and revocable."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import voice_api as api
import chat_service
from voice_devices import VoiceDevices


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    def owner(request):
        if not request.headers.get('x-owner'):
            raise HTTPException(401)
        return request.headers['x-owner']
    monkeypatch.setattr(api, 'owner', owner)
    monkeypatch.setattr(api, 'voice_conversation', lambda user: 'voice-' + user)
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    code = client.post('/api/connections/voice/connect', headers={'x-owner': 'alice'}).json()['pairing_code']
    pair = client.post('/api/voice/pair', json={'pairing_code': code})
    assert pair.status_code == 200
    token = pair.json()['device_token']
    return client, token, code


def test_pairing_is_single_use_replaced_and_revoked(setup):
    client, token, code = setup
    assert client.post('/api/voice/pair', json={'pairing_code': code}).status_code == 400
    assert client.get('/api/connections/voice').status_code == 401
    assert client.get('/api/voice/session').status_code == 401
    assert client.get('/api/connections/voice', headers={'x-owner': 'bob'}).json()['paired'] is False
    assert VoiceDevices().resolve(token) == ('alice', 'voice-alice')
    client.post('/api/connections/voice/disconnect', headers={'x-owner': 'bob'})
    assert VoiceDevices().resolve(token)[0] == 'alice'
    code2 = client.post('/api/connections/voice/connect', headers={'x-owner': 'alice'}).json()['pairing_code']
    token2 = client.post('/api/voice/pair', json={'pairing_code': code2}).json()['device_token']
    assert VoiceDevices().resolve(token)[0] == 'alice'  # Pairing a PC does not revoke the phone.
    client.post('/api/connections/voice/disconnect', headers={'x-owner': 'alice'})
    with pytest.raises(ValueError): VoiceDevices().resolve(token2)
    assert token2 not in VoiceDevices().path.read_bytes().decode('utf-8', errors='ignore')


def test_phone_text_uses_existing_chat_and_ignores_identity_injection(setup, monkeypatch):
    client, token, _ = setup
    calls = []
    monkeypatch.setattr(chat_service, 'get_conversation', lambda user, cid: {'bot_id': 'overwatcher'} if (user, cid) == ('alice', 'voice-alice') else None)
    async def stream(user, cid, text, cancel, **kwargs):
        calls.append((user, cid, text, kwargs['request_id']))
        yield {'type': 'status', 'status': 'complete', 'content': 'Your calendar has two events.'}
    monkeypatch.setattr(chat_service, 'stream_chat', stream)
    response = client.post('/api/voice/chat', headers=lease(client, token), json={
        'message': ' Show my calendar ', 'request_id': 'a' * 32, 'owner': 'bob', 'conversation_id': 'foreign', 'workspace_id': 'foreign'})
    assert response.status_code == 200 and 'text/event-stream' in response.headers['content-type']
    assert calls == [('alice', 'voice-alice', 'Show my calendar', 'a' * 32)], response.text
    assert 'Your calendar has two events.' in response.text
    assert 'done' in response.text
    assert client.post('/api/voice/chat', json={'message': 'hi', 'request_id': 'a' * 32}).status_code == 401


@pytest.mark.parametrize('message,identity', [('', 'a' * 32), ('x' * 4001, 'a' * 32), ([], 'a' * 32), ('hello', 'invalid')])
def test_bad_transcripts_fail_before_execution(setup, message, identity):
    client, token, _ = setup
    result = client.post('/api/voice/chat', headers=lease(client, token), json={'message': message, 'request_id': identity})
    assert result.status_code == 400


def test_pairing_expiry_and_code_reissue(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    devices = VoiceDevices()
    old = devices.begin('alice', 'voice-alice')['pairing_code']
    code = devices.begin('alice', 'voice-alice')['pairing_code']
    with pytest.raises(ValueError): devices.redeem(old)
    with devices.db() as db: db.execute('UPDATE codes SET expires=0')
    with pytest.raises(ValueError): devices.redeem(code)


def lease(client, token):
    headers = {'Authorization': 'Bearer ' + token}
    result = client.post('/api/voice/listen', headers=headers, json={'name': 'Test phone', 'takeover': True})
    assert result.status_code == 200, result.text
    headers['X-Voice-Lease'] = result.json()['lease']
    return headers
