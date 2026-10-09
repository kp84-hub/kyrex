"""Owner isolation, controls, Chat publication, and explicit manual resends."""
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import level6_trainer_api as api
import level6_trainer_monitor as monitor
import serve
from level6_trainer_store import TrainerStore, EASTERN, starts

NOW = datetime(2026, 10, 9, 16, tzinfo=EASTERN).timestamp()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(api.chat_service, '_chat_root', lambda: tmp_path / 'chat')
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(require_user=lambda req: req.headers.get('x-owner', 'alice')))
    bot = {'id': 'calendar', 'name': 'Calendar Bot', 'owner': 'alice', 'status': 'running', 'policy': serve.CALENDAR_PRESET}
    monkeypatch.setattr(api.bots, 'get_bot', lambda bid: bot if bid == bot['id'] else None)
    monkeypatch.setattr(api.bots, 'load_bots', lambda: {bot['id']: bot})
    monkeypatch.setattr(serve, 'build_context', lambda *a: SimpleNamespace(bot_owner=bot['owner'], policy=bot['policy']))
    monkeypatch.setattr(monitor, 'group_ready', lambda *a: 'Group delivery is disabled on the server')
    monkeypatch.setattr(api.chat_service, '_task_store', lambda: SimpleNamespace(tasks_for_conversation=lambda *a: []))
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app), bot


def settings(**updates):
    return {'enabled': True, 'delivery': 'group', 'bot_id': 'calendar', 'interval_seconds': 3600, 'horizon_days': 14, **updates}


def seed_change(store):
    cfg = store.claim('alice', now=NOW)
    for trainer, when in [('A', NOW), ('B', NOW + 3600), ('B', NOW + 3690)]:
        store.apply_read(cfg, {'rows': [{'date': '2026-10-12', 'trainer_id': trainer, 'trainer_name': f'Coach {trainer}'}],
            'occupied_dates': ['2026-10-12']}, now=when)
    store.release(cfg)
    return store.history('alice')[0]


def test_defaults_and_save_round_trip(client):
    c, _ = client
    data = c.get('/api/automations/level6-trainers').json()
    assert data['settings']['enabled'] == 0 and data['settings']['delivery'] == 'chat'
    assert data['bots'] == [{'id': 'calendar', 'name': 'Calendar Bot'}]
    saved = c.put('/api/automations/level6-trainers', json=settings())
    assert saved.status_code == 200
    assert saved.json()['settings']['horizon_days'] == 14
    assert c.get('/api/automations/level6-trainers', headers={'x-owner': 'bob'}).json()['settings']['enabled'] == 0
    assert c.put('/api/automations/level6-trainers', headers={'x-owner': 'bob'}, json=settings()).status_code == 422
    assert c.put('/api/automations/level6-trainers', json=settings(enabled=False)).status_code == 200


@pytest.mark.parametrize('updates', [{'delivery': 'other'}, {'horizon_days': 31}, {'interval_seconds': 20},
    {'interval_seconds': True}, {'destination': 'https://attacker.example'}, {'owner': 'bob'}])
def test_client_cannot_broaden_the_monitor_or_choose_a_destination(client, updates):
    assert client[0].put('/api/automations/level6-trainers', json=settings(**updates)).status_code == 422


def test_paused_bot_can_disable_but_cannot_enable(client):
    c, bot = client
    assert c.put('/api/automations/level6-trainers', json=settings()).status_code == 200
    bot['status'] = 'stopped'
    assert c.put('/api/automations/level6-trainers', json=settings()).status_code == 422
    assert c.put('/api/automations/level6-trainers', json=settings(enabled=False)).status_code == 200


def test_chat_projection_is_owner_scoped_idempotent_and_keeps_pending_hidden(client):
    c, _ = client
    c.put('/api/automations/level6-trainers', json=settings(delivery='chat'))
    store = TrainerStore()
    change = seed_change(store)
    cid = api.view('alice')['conversation_id']
    first = api.chat_service.get_conversation('alice', cid)
    assert first['messages'][-1]['content'] == 'Your Monday class has a new trainer: Coach B.'
    assert len(api.chat_service.get_conversation('alice', cid)['messages']) == 1
    assert api.chat_service.get_conversation('bob', cid) is None
    assert c.get('/api/automations/level6-trainers/history', headers={'x-owner': 'bob'}).json()['alerts'] == []
    history = c.get('/api/automations/level6-trainers/history').json()['alerts'][0]
    assert history['id'] == change['id'] and history['starts_at'] == starts('2026-10-12')


def test_later_occurrence_is_not_published_in_chat_until_eligible(client):
    c, _ = client
    c.put('/api/automations/level6-trainers', json=settings(delivery='chat'))
    store = TrainerStore()
    cfg = store.claim('alice', now=NOW)
    for trainer, when in [('A', NOW), ('B', NOW + 3600), ('B', NOW + 3690)]:
        rows = [{'date': '2026-10-12', 'trainer_id': 'A', 'trainer_name': 'Coach A'},
                {'date': '2026-10-19', 'trainer_id': trainer, 'trainer_name': f'Coach {trainer}'}]
        store.apply_read(cfg, {'rows': rows, 'occupied_dates': [r['date'] for r in rows]}, now=when)
    store.release(cfg)
    assert store.history('alice')[0]['state'] == 'pending'
    cid = api.view('alice')['conversation_id']
    assert api.chat_service.get_conversation('alice', cid)['messages'] == []
    with store.db() as db:
        db.execute('UPDATE monitor SET next_read=0')
    cfg = store.claim('alice', now=starts('2026-10-12') + 1)
    store.apply_read(cfg, {'rows': [rows[1]], 'occupied_dates': ['2026-10-19']}, now=starts('2026-10-12') + 1)
    store.release(cfg)
    assert api.chat_service.get_conversation('alice', cid)['messages'][0]['content'] == 'Your Monday class has a new trainer: Coach B.'


def test_explicit_resend_idempotency_and_reset(client, monkeypatch):
    c, _ = client
    c.put('/api/automations/level6-trainers', json=settings())
    store = TrainerStore()
    change = seed_change(store)
    with store.db() as db:
        db.execute("UPDATE monitor SET next_read=0")
    cfg = store.claim('alice', now=NOW + 3700)
    attempt = store.begin_delivery(cfg, change['id'], now=NOW + 3700)
    store.finish_delivery(attempt, 'unknown', now=NOW + 3710)
    store.release(cfg)
    monkeypatch.setattr('level6_trainer_store.time.time', lambda: NOW + 4000)
    path = f"/api/automations/level6-trainers/history/{change['id']}/resend"
    assert c.post(path, json={'confirm': False, 'request_id': 'resend-request-123'}).status_code == 422
    assert c.post(path, headers={'x-owner': 'bob'}, json={'confirm': True, 'request_id': 'resend-request-123'}).status_code == 404
    assert c.post(path, json={'confirm': True, 'request_id': 'resend-request-123'}).status_code == 200
    assert c.post(path, json={'confirm': True, 'request_id': 'resend-request-123'}).status_code == 200
    assert store.history('alice')[0]['attempt'] == 1
    assert c.post('/api/automations/level6-trainers/reset').status_code == 200
    assert store.history('alice')[0]['state'] == 'superseded'
