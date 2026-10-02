"""Security boundaries and actual read-only phone/Cloud flow."""
import asyncio
import concurrent.futures
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest

CLOUD = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CLOUD))
sys.path.insert(0, str(CLOUD.parent / 'kyrex_engine'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import device_messages as dm
import connectors


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET', 'test-key-for-sms')
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    return dm.MessagesStore()


def upload(store, owner='alice'):
    code = store.begin(owner)['pairing_code']
    token = store.redeem(code)
    store.sync(token, [{'sender': 'School', 'number': '+15550001', 'received': '2026-10-02 08:00:00', 'body': 'Field trip bus arrives at 9'}])
    return token


def test_pair_sync_search_isolated_and_encrypted(store):
    token = upload(store)
    assert store.view('alice')['connected']
    assert not store.view('bob')['connected']
    assert store.search('alice', 'FIELD trip')['messages'][0]['sender'] == 'School'
    assert store.search('alice', 'missing')['messages'] == []
    with pytest.raises(dm.MessagesError):
        store.search('bob')
    raw = store.path.read_bytes()
    assert b'Field trip' not in raw and b'+15550001' not in raw and token.encode() not in raw
    assert 'token' not in json.dumps(store.view('alice'))


def test_pair_once_and_expiry(store):
    code = store.begin('alice')['pairing_code']
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        def attempt():
            try:
                return store.redeem(code)
            except dm.MessagesError:
                return None
        assert sum(bool(x) for x in pool.map(lambda _: attempt(), range(2))) == 1
    code = store.begin('bob')['pairing_code']
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE pairings SET expires=?', (time.time() - 1,))
    with pytest.raises(dm.MessagesError):
        store.redeem(code)


def test_disconnect_revokes_all_and_repair_rotates(store):
    old = upload(store)
    new = store.redeem(store.begin('alice')['pairing_code'])
    with pytest.raises(dm.MessagesError):
        store.sync(old, [])
    assert not store.view('alice')['connected']
    store.sync(new, [])
    pending = store.begin('alice')['pairing_code']
    store.disconnect('alice')
    for fn, args in [(store.sync, (new, [])), (store.redeem, (pending,)), (store.search, ('alice',))]:
        with pytest.raises(dm.MessagesError):
            fn(*args)
    assert not store.view('alice')['paired']


@pytest.mark.parametrize('payload', [[{}]*101, {}, [{'body': 'x'*10001}], [{'number': {'evil': True}}], [None]])
def test_bounded_snapshots(store, payload):
    token = upload(store)
    with pytest.raises(dm.MessagesError):
        store.sync(token, payload)
    assert store.search('alice')['snapshot_count'] == 1


def test_missing_key_fails_closed(store, monkeypatch):
    upload(store)
    monkeypatch.delenv('WEB_SESSION_SECRET')
    monkeypatch.delenv('KYREX_PROVIDER_SECRETS_KEY', raising=False)
    assert not store.view('alice')['connected']
    with pytest.raises(connectors.ConnectorConfigError):
        store.begin('alice')


def test_only_read_intents():
    for text in ['Show my texts', 'Read my latest text messages', 'messages: latest']:
        assert dm.read_command(text) == ''
    assert dm.read_command('Find my text messages about school.') == 'school'
    assert dm.read_command('messages: search field trip') == 'field trip'
    for text in ['Send my texts to Bob', 'Delete my text messages', 'Show my emails', 'What is SMS?', 'summarize this email about text messages']:
        assert dm.read_command(text) is None


def test_phone_command_read_only(store, monkeypatch):
    spec = importlib.util.spec_from_file_location('phone_bridge', CLOUD / 'messages_phone.py')
    phone = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(phone)
    captured = []
    def run(command, **kwargs):
        captured.append(command)
        class Result:
            stdout = json.dumps([{'body': 'older', 'received': '2026-01-01'}, {'body': 'newer', 'received': '2026-01-02'}])
        return Result()
    monkeypatch.setattr(phone.subprocess, 'run', run)
    assert phone.sms()[0]['body'] == 'newer'
    assert captured == [['termux-sms-list', '-t', 'inbox', '-l', '100', '-n', '-d']]
    for url in ['http://example.com', 'https://example.com@evil.test', 'https://example.com/api', 'https://example.com/?token=x']:
        with pytest.raises(ValueError):
            phone.cloud_url(url)
    with pytest.raises(ValueError):
        phone.NoRedirect().redirect_request(None)
    monkeypatch.setattr(phone, 'CONFIG', store.path.parent / 'phone.json')
    phone.save({'upload_token': 'private'})
    assert phone.CONFIG.stat().st_mode & 0o777 == 0o600


def test_api_flow_authentication(store, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import connections_api
    import messages_api
    def auth(request):
        who = request.headers.get('x-owner')
        if not who:
            raise HTTPException(401, 'Sign in')
        return who
    monkeypatch.setattr(connections_api, '_require_user', auth)
    app = FastAPI()
    app.include_router(messages_api.router)
    client = TestClient(app)
    assert client.post('/api/connections/messages/connect').status_code == 401
    code = client.post('/api/connections/messages/connect', headers={'x-owner': 'alice'}).json()['pairing_code']
    token = client.post('/api/connections/messages/pair', json={'pairing_code': code}).json()['upload_token']
    assert client.post('/api/connections/messages/pair', json={'pairing_code': code}).status_code == 400
    headers = {'Authorization': 'Bearer ' + token}
    assert client.post('/api/connections/messages/sync', headers=headers, json={'messages': [{'body': 'school bus'}]}).status_code == 200
    assert client.get('/api/connections/messages/search', headers=headers).status_code == 401
    assert client.get('/api/connections/messages/search', headers={'x-owner': 'bob'}).status_code == 400
    assert client.get('/api/connections/messages/search?q=bus', headers={'x-owner': 'alice'}).json()['messages'][0]['body'] == 'school bus'
    assert client.get('/api/connections/messages/search?max_results=101', headers={'x-owner': 'alice'}).status_code == 422
    assert client.post('/api/connections/messages/sync', headers=headers, content=b'x'*1100001).status_code == 413
    assert client.get('/api/connections/messages/bridge.py').status_code == 200
    assert client.post('/api/connections/messages/disconnect', headers={'x-owner': 'alice'}).status_code == 200
    assert client.post('/api/connections/messages/sync', headers=headers, json={'messages': []}).status_code == 400


def test_chat_reads_snapshot_without_provider_and_checks_bot(store, monkeypatch):
    import chat_service as chat
    upload(store)
    conv = {'conversation_id': 'test', 'messages': []}
    monkeypatch.setattr(chat, 'get_conversation', lambda *a: conv)
    monkeypatch.setattr(chat, '_write', lambda *a: None)
    monkeypatch.setattr(chat, '_resolve_provider', lambda *a, **k: pytest.fail('SMS read invoked provider'))
    async def run(owner):
        return [f async for f in chat.stream_chat(owner, 'test', 'Show my texts')]
    frames = asyncio.run(run('alice'))
    assert 'Field trip bus' in frames[-1]['content']
    assert 'synced' in frames[-1]['content']
    assert 'Connect Messages' in asyncio.run(run('bob'))[-1]['content']
    conv['bot_id'] = 'selected'
    monkeypatch.setattr(chat, 'resolve_bot_for_user', lambda *a: {'owner': 'bob'})
    with pytest.raises(chat.ChatUnavailable):
        asyncio.run(run('alice'))
