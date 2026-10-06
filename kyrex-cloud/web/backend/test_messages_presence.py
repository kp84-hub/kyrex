import pytest
import device_messages as dm
import messages_send as ms


@pytest.fixture
def phone(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'presence-tests')
    store = dm.MessagesStore()
    token = store.redeem(store.begin('alice')['pairing_code'])
    store.sync(token, [{'conversation_id': 'c1', 'conversation': 'Ethan', 'body': 'Saved text'}])
    return store, token


def test_snapshot_does_not_imply_live_connection_and_lease_expires(phone, monkeypatch):
    store, token = phone
    assert store.view('alice')['connected']
    assert store.phone_view('alice')['status'] == 'unknown'
    monkeypatch.setattr(dm.time, 'time', lambda: 1000)
    ms.SendQueue(store).poll(token, True)
    store.heartbeat(token, 'ready')
    assert store.phone_view('alice') == {'status': 'ready', 'last_seen': 1000, 'expires_in': 45, 'send_ready': True}
    monkeypatch.setattr(dm.time, 'time', lambda: 1045)
    assert store.phone_view('alice')['status'] == 'offline'
    assert not store.phone_view('alice')['send_ready']
    assert store.search('alice')['messages'][0]['body'] == 'Saved text'
    store.heartbeat(token, 'reconnecting')
    assert store.phone_view('alice')['status'] == 'reconnecting'
    assert not store.phone_view('alice')['send_ready']
    store.heartbeat(token, 'needs_attention')
    assert store.phone_view('alice')['status'] == 'needs_attention'


def test_presence_is_owner_scoped_revoked_and_not_a_send_command(phone):
    store, token = phone
    queue = ms.SendQueue(store)
    store.heartbeat(token, 'ready')
    assert not store.phone_view('alice')['send_ready'], 'Heartbeat cannot enable sending'
    assert store.phone_view('bob')['status'] == 'not_linked'
    queue.poll(token, True)
    job = queue.start('alice', 'Ethan', 'Test preview')
    store.heartbeat(token, 'ready')
    assert queue.get('alice', job['id'])['state'] == 'queued', 'Heartbeat must not claim even a preview'
    for invalid in ['connected', 'send', {}, None]:
        with pytest.raises(dm.MessagesError): store.heartbeat(token, invalid)
    new = store.redeem(store.begin('alice')['pairing_code'])
    assert store.phone_view('alice')['status'] == 'unknown'
    with pytest.raises(dm.MessagesError): store.heartbeat(token, 'ready')
    store.heartbeat(new, 'ready')
    store.disconnect('alice')
    with pytest.raises(dm.MessagesError): store.heartbeat(new, 'ready')
    assert store.phone_view('alice')['status'] == 'not_linked'


def test_presence_api_authentication_private_response_and_saved_history(phone, monkeypatch):
    store, token = phone
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import connections_api, messages_api
    def owner(request):
        who = request.headers.get('x-owner')
        if not who: raise HTTPException(401)
        return who
    monkeypatch.setattr(connections_api, '_require_user', owner)
    app = FastAPI(); app.include_router(messages_api.router)
    client = TestClient(app)
    status = '/api/connections/messages/status'
    heartbeat = '/api/connections/messages/device/heartbeat'
    headers = {'Authorization': 'Bearer ' + token}
    assert client.get(status, headers=headers).status_code == 401
    assert client.post(heartbeat, json={'state': 'ready'}, headers={'x-owner': 'alice'}).status_code == 401
    assert client.post(heartbeat, headers=headers, json={'state': 'ready'}).status_code == 200
    result = client.get(status, headers={'x-owner': 'alice'})
    assert result.headers['cache-control'] == 'no-store'
    assert result.json()['phone']['status'] == 'ready'
    assert result.json()['connected']
    assert 'Saved text' not in result.text and token not in result.text
    assert client.get(status, headers={'x-owner': 'bob'}).json()['phone']['status'] == 'not_linked'
    assert client.post(heartbeat, headers=headers, json={'state': 'send'}).status_code == 400
