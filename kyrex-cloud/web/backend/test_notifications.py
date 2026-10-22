"""Saved events, two owners, real Web Push encryption; no external delivery."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

import chat_notifications as n
import notification_api as api
from task_store import CloudTaskStore
from web_sessions import SessionStore
REAL_BROWSER_SESSION = api.browser_session


def subscription(suffix='fixture', private=None):
    key = (private or ec.generate_private_key(ec.SECP256R1())).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b'=').decode()
    return {'endpoint': 'https://fcm.googleapis.com/fcm/send/' + suffix,
            'keys': {'p256dh': encode(key), 'auth': encode(b'fixture-key-1234')}}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path)); monkeypatch.setenv('WEB_SESSION_SECRET', 'notification-test-only-secret')
    monkeypatch.setenv('GITHUB_CLIENT_ID', 'notification-test-client')
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'notification-test-client-secret')
    monkeypatch.setenv('GITHUB_REDIRECT_URI', 'https://chat.example.test/auth/callback')
    monkeypatch.setenv('WEB_ALLOWED_GITHUB_USERNAME', 'alice')
    tasks = CloudTaskStore(tmp_path / 'tasks.db'); notifications = n.NotificationStore()
    sessions = SessionStore(secret='session-fixture', path=tmp_path / 'sessions.db')
    for user in ('alice', 'bob'):
        sessions[user + '-cookie'] = user
    def path(user, cid):
        target = tmp_path / 'conversations' / user / (cid + '.json'); target.parent.mkdir(parents=True, exist_ok=True)
        return target
    registry = {'bot-a': {'id': 'bot-a', 'owner': 'alice', 'name': 'Alice Bot'},
                'bot-b': {'id': 'bot-b', 'owner': 'bob', 'name': 'Bob Bot'}}
    chat = SimpleNamespace(bots=SimpleNamespace(load_bots=lambda: registry), _conv_path=path)
    for user, bot, cid in [('alice', 'bot-a', 'a'*32), ('bob', 'bot-b', 'b'*32)]:
        path(user, cid).write_text(json.dumps({'conversation_id': cid, 'bot_id': bot}))
    sent = []
    def sender(*args): sent.append(args); return 201
    service = api.NotificationService(tasks, chat, store=notifications, sessions=sessions.owner_for_reference, sender=sender)
    def owner(request):
        user = sessions.get(request.cookies.get('session'))
        if not user: raise HTTPException(401, 'Not authenticated')
        return user
    monkeypatch.setattr(api, 'owner', owner)
    monkeypatch.setattr(api, 'browser_session', lambda request, user: sessions.reference(request.cookies['session']))
    monkeypatch.setattr(api, 'session_owner', sessions.owner_for_reference)
    app = FastAPI(); app.include_router(api.router)
    client = TestClient(app, base_url='https://chat.example.test', cookies={'session': 'alice-cookie'})
    def task(user='alice', cid=None, bot=None):
        return tasks.submit(user + '-session', 'Inspect a saved result', chat_id=user,
            bot_id=bot or ('bot-a' if user == 'alice' else 'bot-b'),
            conversation_id=cid or ('a'*32 if user == 'alice' else 'b'*32), resolve_bot=False)
    return SimpleNamespace(tasks=tasks, store=notifications, sessions=sessions, chat=chat,
                           registry=registry, service=service, client=client, task=task, sent=sent)


def enroll(f, user='alice', identity='c'*32, suffix='fixture'):
    value = subscription(suffix)
    f.store.subscribe(user, identity, f.sessions.reference(user + '-cookie'), value,
                      'https://chat.example.test', f.sessions.owner_for_reference)
    return value


def test_saved_task_results_inbox_owner_isolation_read_and_replay(setup):
    f = setup; enroll(f)
    a, b = f.task(), f.task('bob')
    f.tasks.set_status(a, 'done'); f.tasks.set_status(b, 'failed')
    f.service.tick()
    rows = f.store.list('alice'); assert rows['unread'] == 1
    item = rows['notifications'][0]
    assert item['conversation'] == 'a'*32 and item['title'] == 'Alice Bot · Task finished'
    assert len(f.sent) == 1 and f.sent[0][1] == item['id']
    assert f.store.list('bob')['unread'] == 1
    assert f.client.get('/api/notifications/' + f.store.list('bob')['notifications'][0]['id']).status_code == 404
    f.client.post('/api/notifications/read', json={'ids': [item['id']]})
    assert f.store.list('alice')['unread'] == 0 and f.store.list('bob')['unread'] == 1
    f.tasks.add_event(a, 'status', {'status': 'done'})
    # Observer restart/repeated terminal events cannot create a second alert.
    api.NotificationService(f.tasks, f.chat, store=f.store, sessions=f.sessions.owner_for_reference, sender=lambda *a: pytest.fail('Duplicate push')).tick()
    assert len(f.store.list('alice')['notifications']) == 1


def test_projection_uses_saved_owner_and_conversation_not_event_payload(setup):
    f = setup
    tid = f.task()
    f.tasks.set_status(tid, 'done')
    f.tasks.add_event(tid, 'status', {'status': 'done', 'owner': 'bob', 'conversation_id': 'b'*32,
                                    'url': 'https://foreign.example', 'token': 'do-not-copy'})
    f.service.tick()
    assert f.store.list('bob')['notifications'] == []
    assert f.store.list('alice')['notifications'][0]['conversation'] == 'a'*32
    for user, cid, bot in [('alice', 'b'*32, 'bot-a'), ('alice', 'a'*32, 'bot-b')]:
        f.tasks.set_status(f.task(user, cid, bot), 'done')
    f.service.tick()
    assert len(f.store.list('alice')['notifications']) == 1
    assert 'do-not-copy' not in f.store.path.read_bytes().decode(errors='ignore')


def test_approval_alert_is_deduped_and_never_approves_or_reexecutes(setup):
    f = setup; tid = f.task()
    f.tasks.set_status(tid, 'running')
    aid = f.tasks.persist_approval_request(tid, 'alice-session', 'message', 2, 'private-review-token', 'Private summary', 'Private details')
    f.service.tick(); f.service.tick()
    row = f.store.list('alice')['notifications'][0]
    assert row['kind'] == 'awaiting_approval'
    assert f.tasks.get_pending_approval(tid)['approval_id'] == aid
    f.client.get('/api/notifications/' + row['id'])
    f.client.post('/api/notifications/read', json={'ids': [row['id']]})
    assert f.tasks.get_pending_approval(tid)['decision'] == 'pending'
    assert f.tasks.status(tid) == 'awaiting_approval'
    assert b'private-review-token' not in f.store.path.read_bytes()
    f.tasks.resolve_approval_request(tid, 'message', 'approved')
    f.tasks.persist_approval_request(tid, 'alice-session', 'message-2', 2, 'other-token', 'Other review', '')
    f.service.tick()
    assert len(f.store.list('alice')['notifications']) == 2


def test_first_install_skips_history_but_restart_catches_saved_events(setup, tmp_path):
    f = setup; old = f.task(); f.tasks.set_status(old, 'done')
    fresh = n.NotificationStore(tmp_path / 'new-notifications.db')
    svc = api.NotificationService(f.tasks, f.chat, store=fresh)
    svc.tick(); assert fresh.list('alice')['notifications'] == []
    new = f.task(); f.tasks.set_status(new, 'done')
    restarted = api.NotificationService(f.tasks, f.chat, store=fresh)
    restarted.tick(); assert fresh.list('alice')['notifications'][0]['task'] == new


def test_superseded_approval_is_not_announced_as_still_waiting(setup):
    f = setup; tid = f.task(); f.tasks.set_status(tid, 'awaiting_approval'); f.tasks.set_status(tid, 'done')
    f.service.tick()
    assert [r['kind'] for r in f.store.list('alice')['notifications']] == ['done']


def test_explicit_enable_config_device_disable_test_and_permission_boundaries(setup):
    f = setup; client = f.client; device = 'c'*32; url = '/api/notifications/devices/' + device
    cfg = client.get('/api/notifications/settings')
    assert cfg.json()['available'] and len(cfg.json()['public_key']) == 87
    assert cfg.headers['cache-control'] == 'no-store'
    value = subscription()
    assert client.post(url, json={'subscription': value}, headers={'Origin': 'https://foreign.example'}).status_code == 403
    assert client.post(url, json={'subscription': value}).status_code == 200
    assert client.get(url).json()['enabled']
    assert client.post(url + '/test').json()['queued']
    assert client.post(url + '/test').status_code == 409
    # A foreign owner cannot inspect, test, or revoke another device.
    client.cookies.set('session', 'bob-cookie')
    assert not client.get(url).json()['enabled']
    assert client.post(url + '/test').status_code == 409
    client.delete(url)
    assert f.store.device('alice', device, f.sessions.reference('alice-cookie'))
    client.cookies.set('session', 'alice-cookie')
    assert client.delete(url).json() == {'enabled': False}
    assert not client.get(url).json()['enabled']
    client.cookies.clear()
    assert client.get('/api/notifications').status_code == 401
    assert client.post(url, json={'subscription': value}).status_code == 401


@pytest.mark.parametrize('endpoint', [
    'http://fcm.googleapis.com/fcm/send/x', 'https://127.0.0.1/push',
    'https://169.254.169.254/latest/meta-data', 'https://fcm.googleapis.com.evil.example/x',
    'https://fcm.googleapis.com:8443/x', 'https://name@fcm.googleapis.com/x',
    'https://fcm.googleapis.com/x#secret', 'https://notify.windows.com.evil.example/x',
    'https://localhost/x', 'file:///tmp/private',
])
def test_arbitrary_push_endpoints_cannot_be_saved_or_fetched(setup, endpoint):
    f = setup; value = subscription(); value['endpoint'] = endpoint
    result = f.client.post('/api/notifications/devices/' + 'c'*32, json={'subscription': value})
    assert result.status_code == 400
    assert not f.sent


def test_keys_and_endpoint_are_sealed_vapid_survives_restart_and_bad_key_is_rejected(setup):
    f = setup; value = enroll(f)
    _, pub = f.store.vapid()
    assert n.NotificationStore(f.store.path).vapid()[1] == pub
    raw = f.store.path.read_bytes()
    assert value['endpoint'].encode() not in raw and value['keys']['auth'].encode() not in raw
    bad = {**value, 'keys': {'auth': 'x', 'p256dh': 'not-a-real-curve-point'}}
    assert f.client.post('/api/notifications/devices/' + 'd'*32, json={'subscription': bad}).status_code == 400


def test_cookie_expiry_does_not_drop_consent_but_logout_revokes_pending_delivery(setup):
    f = setup; enroll(f)
    f.store.record('alice', 'one', 'done', 'Task finished', 'a'*32)
    with f.sessions._connect() as db:
        db.execute('UPDATE web_sessions SET expires_at=0')
    f.service.tick()
    assert len(f.sent) == 1
    f.store.record('alice', 'two', 'done', 'Task finished', 'a'*32)
    f.store.revoke_session(f.sessions.reference('alice-cookie'))
    f.service.tick()
    assert len(f.sent) == 1
    assert f.store.list('alice')['unread'] == 2


def test_same_owner_login_rotation_preserves_consent_and_account_change_removes_it(setup):
    f = setup; enroll(f)
    f.store.rebind_session(f.sessions.reference('alice-cookie'), 'next-reference', 'alice')
    assert f.store.device('alice', 'c'*32, 'next-reference')
    f.store.rebind_session('next-reference', 'bob-reference', 'bob')
    assert not f.store.device('alice', 'c'*32, 'next-reference')


def test_concurrent_delivery_claim_is_unique_and_transient_failure_retries_alert_only(setup):
    f = setup; enroll(f); nid = f.store.record('alice', 'fixture', 'done', 'Finished', 'a'*32)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: f.store.claim(), range(2)))
    assert sum(c is not None for c in claims) == 1
    row = next(c for c in claims if c)
    f.store.finish(row, 'Temporary delivery failure')
    assert f.store.claim() is None
    with f.store.db() as db:
        db.execute('UPDATE deliveries SET due=0')
    n.deliver_one(f.store, f.sessions.owner_for_reference, lambda *args: 201)
    with f.store.db() as db:
        assert db.execute('SELECT state FROM deliveries WHERE notification=?', (nid,)).fetchone()[0] == 'sent'
    assert f.tasks.notification_cursor() == 0, 'Delivery must never submit a task'


@pytest.mark.parametrize('status', [404, 410])
def test_gone_subscription_is_removed_without_replaying_work(setup, status):
    f = setup; enroll(f); f.store.record('alice', 'fixture', 'done', 'Finished', 'a'*32)
    n.deliver_one(f.store, f.sessions.owner_for_reference, lambda *args: status)
    assert not f.store.device('alice', 'c'*32, f.sessions.reference('alice-cookie'))
    assert len(f.store.list('alice')['notifications']) == 1


def test_real_webpush_encrypts_payload_and_disables_redirects(setup, monkeypatch):
    import requests
    import http_ece
    f = setup; receiver = ec.generate_private_key(ec.SECP256R1())
    value = subscription(private=receiver); private, _ = f.store.vapid(); calls = []
    def transport(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = requests.Response(); response.status_code = 201; response._content = b''
        return response
    monkeypatch.setattr(requests.Session, 'request', transport)
    assert n.send_push(value, 'e'*32, private, 'https://chat.example.test') == 201
    assert len(calls) == 1
    method, url, options = calls[0]
    assert method == 'POST' and url == value['endpoint']
    assert options['allow_redirects'] is False and options['timeout'] == 8
    assert options['headers']['content-encoding'] == 'aes128gcm'
    assert 'e'*32 not in str(options['data']) and 'vapid ' in options['headers']['Authorization']
    decrypted = http_ece.decrypt(options['data'], private_key=receiver,
                                 auth_secret=n.unbase64(value['keys']['auth']), version='aes128gcm')
    assert json.loads(decrypted) == {'id': 'e'*32}


def test_retention_and_malformed_mutations(setup):
    f = setup; nid = f.store.record('alice', 'old', 'done', 'Finished')
    with f.store.db() as db: db.execute('UPDATE inbox SET created=0')
    f.store.prune(); assert f.store.get('alice', nid) is None
    assert f.client.post('/api/notifications/read', json={'ids': ['bad']}).status_code == 400
    assert f.client.post('/api/notifications/read', content=b'x'*16001).status_code == 413
    assert f.client.post('/api/notifications/read', json={'ids': []}, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_real_browser_session_requirement_and_logout_hook_revoke_consent(setup, monkeypatch):
    import main
    f = setup; enroll(f)
    monkeypatch.setattr(main, 'sessions', f.sessions)
    monkeypatch.setattr(api, 'browser_session', REAL_BROWSER_SESSION)
    assert f.client.get('/api/notifications/devices/' + 'c'*32).json()['enabled']
    f.client.cookies.clear()
    monkeypatch.setattr(api, 'owner', lambda request: 'alice')  # bearer alone cannot enroll a browser
    assert f.client.post('/api/notifications/devices/' + 'd'*32, json={'subscription': subscription('other')}).status_code == 401
    app = FastAPI(); app.add_api_route('/auth/logout', main.logout)
    client = TestClient(app, cookies={'session': 'alice-cookie'})
    client.get('/auth/logout', follow_redirects=False)
    assert f.sessions.get('alice-cookie') is None
    assert not f.store.device('alice', 'c'*32, f.sessions.reference('alice-cookie'))


def test_delegation_opens_verified_parent_and_rejects_changed_relationship(setup):
    f = setup; target = 'd'*32
    f.chat._conv_path('alice', target).write_text(json.dumps({'conversation_id': target, 'bot_id': 'worker'}))
    f.registry['worker'] = {'id': 'worker', 'owner': 'alice', 'name': 'Worker'}
    tid = f.task(cid=target, bot='worker')
    did = f.tasks.create_delegation(owner='alice', coordinator_bot_id='bot-a', target_bot_id='worker',
        task_text='Inspect result', parent_conversation_id='a'*32, task_id=tid)
    f.tasks.set_status(tid, 'done', parent_delegation_id=did)
    f.service.tick()
    assert f.store.list('alice')['notifications'][0]['conversation'] == 'a'*32
    other = f.task(cid=target, bot='worker')
    f.tasks.set_status(other, 'done', parent_delegation_id=did)
    f.service.tick()
    assert len(f.store.list('alice')['notifications']) == 1


def test_saved_trainer_alerts_notify_once_without_reading_or_sending(setup):
    from level6_trainer_store import TrainerStore, conversation_id
    f = setup; cid = conversation_id('alice')
    f.chat._conv_path('alice', cid).write_text(json.dumps({'conversation_id': cid, 'automation': 'level6-trainer-monitor'}))
    with TrainerStore().db() as db:
        db.execute('''INSERT INTO changes(id,owner,day,version,old_trainer_id,trainer_id,trainer_name,message,observed_at,chat_visible)
            VALUES ('fixture','alice','2026-10-25',1,'old','new','Private name','Private message',?,1)''', (time.time(),))
    f.service.tick(); f.service.tick()
    assert f.store.list('alice')['notifications'][0]['conversation'] == cid
    assert len(f.store.list('alice')['notifications']) == 1
    assert f.tasks.notification_cursor() == 0
    assert b'Private message' not in f.store.path.read_bytes()


def test_delivery_crash_recovery_fences_late_completion(setup):
    f = setup; enroll(f); f.store.record('alice', 'fixture', 'done', 'Finished')
    previous = f.store.claim()
    assert f.store.claim() is None
    with f.store.db() as db: db.execute('UPDATE deliveries SET lease=0')
    current = n.NotificationStore(f.store.path).claim()
    assert current['claim'] != previous['claim']
    f.store.finish(previous)
    with f.store.db() as db:
        assert db.execute('SELECT state FROM deliveries').fetchone()[0] == 'sending'
    f.store.finish(current)
    assert f.store.claim() is None


def test_lifespan_preserves_existing_services_and_stops_notification_observer(setup, monkeypatch):
    from contextlib import asynccontextmanager
    events = []
    @asynccontextmanager
    async def previous(app):
        events.append('existing-start'); yield; events.append('existing-stop')
    app = FastAPI(lifespan=previous)
    monkeypatch.setattr(api.NotificationService, 'start', lambda self: events.append('notifications-start'))
    monkeypatch.setattr(api.NotificationService, 'stop', lambda self: events.append('notifications-stop'))
    api.install(app, setup.tasks)
    with TestClient(app):
        assert events == ['existing-start', 'notifications-start']
    assert events == ['existing-start', 'notifications-start', 'notifications-stop', 'existing-stop']


def test_older_unread_alerts_remain_accessible_after_first_hundred_are_read(setup):
    f = setup
    for i in range(110):
        f.store.record('alice', 'alert-' + str(i), 'done', 'Task finished')
    first = f.store.list('alice')
    assert first['unread'] == 110 and len(first['notifications']) == 100
    f.store.mark_read('alice', [row['id'] for row in first['notifications']])
    remaining = f.store.list('alice')
    assert remaining['unread'] == 10
    assert all(row['read'] is None for row in remaining['notifications'][:10])
    f.store.mark_read('alice', [row['id'] for row in remaining['notifications'] if row['read'] is None])
    assert f.store.list('alice')['unread'] == 0


def test_persistent_device_cookie_survives_login_cookie_expiry_and_still_revokes_on_logout(setup, monkeypatch):
    import main
    f = setup; device = 'c'*32
    monkeypatch.setattr(main, 'sessions', f.sessions)
    enrolled = f.client.post('/api/notifications/devices/' + device, json={'subscription': subscription()})
    assert enrolled.status_code == 200
    assert 'HttpOnly' in enrolled.headers['set-cookie'] and 'Secure' in enrolled.headers['set-cookie']
    sealed = enrolled.cookies[api.DEVICE_COOKIE]
    assert device not in sealed
    f.sessions.pop('alice-cookie')
    f.sessions['new-cookie'] = 'alice'
    # The expired login cookie is absent at the OAuth callback. The separate
    # sealed device cookie restores only SAME-account push consent.
    api.rebind_browser(None, 'new-cookie', 'alice', sealed)
    assert f.store.device('alice', device, f.sessions.reference('new-cookie'))
    api.logout_browser(None, sealed)
    assert not f.store.device('alice', device, f.sessions.reference('new-cookie'))
    assert api.cookie_device('forged-' + device) is None


def test_device_cookie_cannot_transfer_consent_to_another_account(setup, monkeypatch):
    import main
    f = setup; monkeypatch.setattr(main, 'sessions', f.sessions)
    enrolled = f.client.post('/api/notifications/devices/' + 'c'*32, json={'subscription': subscription()})
    api.rebind_browser(None, 'bob-cookie', 'bob', enrolled.cookies[api.DEVICE_COOKIE])
    assert not f.store.device('alice', 'c'*32, f.sessions.reference('alice-cookie'))
    assert not f.store.device('bob', 'c'*32, f.sessions.reference('bob-cookie'))
