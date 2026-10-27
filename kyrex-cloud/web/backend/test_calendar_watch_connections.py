"""Event-review setup through real owner connection stores; no live services."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import bots
import calendar_watch as watch
import calendar_watch_api as api
import calendar_watch_sources as sources
import chat_service
import connectors
import device_messages
import provider_profiles
from task_store import CloudTaskStore


@pytest.fixture
def connected(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'event-review-connection-test-only')
    tasks = CloudTaskStore(tmp_path / 'tasks.sqlite3')
    google = connectors.ConnectorStore(tmp_path / 'connectors.json')
    scopes = [connectors.GOOGLE_CALENDAR_READ_SCOPE, connectors.GOOGLE_CALENDAR_WRITE_SCOPE,
              connectors.GOOGLE_GMAIL_READ_SCOPE]
    record = {'owner': 'alice', 'provider': 'google', 'status': 'connected',
              'connected_at': 123, 'scopes': scopes, 'calendar_id': 'family@example.test',
              'sealed': connectors.seal_tokens({'access_token': 'event-review-test-access'})}
    def save_record():
        google._write({'owners': {google._owner_key('alice'): {'owner': 'alice', 'providers': {'google': record}}}})
    save_record()
    monkeypatch.setattr(connectors, 'default_store', lambda: google)
    calls = []
    def transport(method, url, token, params=None, **kwargs):
        calls.append(url)
        assert method == 'GET' and url == 'https://oauth2.googleapis.com/tokeninfo'
        return {'email': 'alice@example.test'}
    monkeypatch.setattr(connectors, 'default_transport', transport)
    provider_profiles.save_profile('alice', {'id': 'fixture', 'name': 'Fixture', 'provider': 'openai',
        'base_url': 'https://model.example.test/v1', 'models': ['fixture-model'], 'api_key': 'event-review-test-key'})
    registry = {'assistant': {'id': 'assistant', 'owner': 'alice', 'status': 'running',
        'name': 'Assistant', 'policy': {}, 'rift': '', 'provider_profile_id': 'fixture', 'model': 'fixture-model'}}
    monkeypatch.setattr(bots, 'load_bots', lambda: registry)
    monkeypatch.setattr(chat_service, '_task_store', lambda: tasks)
    phone = device_messages.MessagesStore()
    credential = phone.redeem(phone.begin('alice')['pairing_code'])
    phone.sync(credential, [])
    monkeypatch.setattr(api, 'owner', lambda request, mutation=False: request.headers.get('x-user', 'alice'))
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app)
    payload = {'enabled': True, 'emails': True, 'messages': True,
               'bot_id': 'assistant', 'interval_seconds': 3600}
    return SimpleNamespace(**locals())


def test_enable_uses_real_calendar_mail_phone_and_model_connections(connected):
    c = connected
    response = c.client.put('/api/automations/calendar-events', json=c.payload)
    assert response.status_code == 200, response.text
    assert response.json()['settings']['enabled'] is True
    cfg = watch.WatchStore(c.tasks).settings('alice')
    assert cfg['binding'] == sources.binding('alice', True, True)
    assert cfg['destination'] == {'connected_at': 123, 'calendar': 'family@example.test', 'email': 'alice@example.test'}
    assert chat_service.get_conversation('alice', response.json()['conversation_id'])['title'] == 'Calendar suggestions'
    assert c.calls == ['https://oauth2.googleapis.com/tokeninfo']
    assert c.tasks.list_tasks() == [], 'saving only enables the existing scheduler; it never writes a calendar event'
    assert 'event-review-test-access' not in response.text and 'event-review-test-key' not in response.text


@pytest.mark.parametrize('denied', ['read', 'write', 'disconnected', 'foreign'])
def test_calendar_scope_and_owner_failures_leave_review_paused(connected, denied):
    c = connected
    if denied in ('read', 'write'):
        c.record['scopes'].remove(connectors.GOOGLE_CALENDAR_READ_SCOPE if denied == 'read' else connectors.GOOGLE_CALENDAR_WRITE_SCOPE)
    elif denied == 'disconnected':
        c.record['status'] = 'disconnected'
    else:
        c.registry['assistant']['owner'] = 'bob'
    c.save_record()
    # Mail validation has its own grant; keep this check about Calendar scope.
    response = c.client.put('/api/automations/calendar-events', json={**c.payload, 'emails': False})
    assert response.status_code == 409, response.text
    assert watch.WatchStore(c.tasks).settings('alice')['enabled'] is False
    assert c.calls == [] and c.tasks.list_tasks() == []


def test_phone_repair_and_calendar_reconnection_invalidate_saved_bindings(connected):
    c = connected
    assert c.client.put('/api/automations/calendar-events', json=c.payload).status_code == 200
    store = watch.WatchStore(c.tasks)
    cfg = store.settings('alice')
    c.phone.redeem(c.phone.begin('alice')['pairing_code'])
    with pytest.raises(ValueError, match='Source connections'):
        store.valid('alice', cfg['revision'])
    assert c.client.put('/api/automations/calendar-events', json=c.payload).status_code == 200
    cfg = store.settings('alice')
    c.record['connected_at'] = 456
    c.save_record()
    with pytest.raises(ValueError, match='changed'):
        store.valid('alice', cfg['revision'])
    assert c.tasks.list_tasks() == []
