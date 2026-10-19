"""Actual owner HTTP -> durable dispatch -> host-channel diagnostics."""
import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient

for key, value in {'GITHUB_CLIENT_ID': 'fixture-client', 'GITHUB_CLIENT_SECRET': 'fixture-client-secret',
                   'WEB_ALLOWED_GITHUB_USERNAME': 'alice', 'WEB_SESSION_SECRET': 'diagnostic-test-only'}.items():
    os.environ.setdefault(key, value)

import main
import bots
import browser_diagnostics as diag
import browser_diagnostics_api as api
import browser_host_bridge as bridge
import browser_host_channel as channels
import browser_hosts as hosts
import serve
from task_store import CloudTaskStore


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'diagnostic-fixture-only')
    monkeypatch.setenv('RAILWAY_GIT_COMMIT_SHA', 'a' * 40)
    monkeypatch.setattr(bots, 'BOTS_FILE', str(tmp_path / 'bots.json'))
    store = CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(main, 'store', store)
    manager = channels.HostManager(task_timeout=1)
    monkeypatch.setattr(channels, '_default_manager', manager)
    for user in ['alice', 'bob']:
        main.sessions['diag-' + user] = user
        bots.add_bot('browser-' + user, 'Browser', 'openai:fixture', str(tmp_path / user),
                     owner=user, status='running', policy=serve.browser_preset_policy(), browser_allowlist=['example.com'])
        enrollment = hosts.enroll_host(user, 'host-' + user)
        hosts.bind_bot(user, 'browser-' + user, 'host-' + user)
        channel = manager.attach(lambda f: None)
        nonce = 'fixture-nonce'
        proof = hmac.new(enrollment['secret'].encode(), ('host-' + user + '.' + nonce).encode(), hashlib.sha256).hexdigest()
        channel.handle(channels.frame('hello', {'host_id': 'host-' + user, 'nonce': nonce, 'proof': proof,
                                               'browser_diagnostics': True, 'host_build': 'b' * 40}))
    yield store, manager
    store.close()
    for user in ['alice', 'bob']: main.sessions.pop('diag-' + user, None)


def client(user='alice'):
    return TestClient(main.app, cookies={'session': 'diag-' + user})


def start(c=None, **values):
    body = {'bot_id': 'browser-alice', 'url': 'https://example.com', 'request_id': 'c' * 32}
    body.update(values)
    return (c or client()).post('/api/browser-diagnostics', json=body)


def test_saved_probe_is_dispatched_once_and_reopens_without_execution(rig):
    store, manager = rig
    r = start(); assert r.status_code == 202, r.text
    ref = r.json()['reference']; assert r.json()['status'] == 'pending'
    assert len(store.list_browser_dispatches('alice')) == 1
    channel = manager.channel_for('host-alice'); sent = []
    def send(frame):
        if frame['type'] == 'task':
            sent.append(frame)
            result = diag.host_result(stages={'dispatch': 'passed', 'browser_launch': 'passed', 'site_access': 'passed'})
            channel.handle(channels.frame('result', {'result': result}))
    channel._send_raw = send
    poller = bridge.BrowserDispatchPoller(store, manager)
    assert poller.tick() == 1
    assert poller.tick() == 0
    assert len(sent) == 1
    data = client().get('/api/browser-diagnostics/' + ref).json()
    assert data['status'] == 'done' and data['cloud_build'] == 'a' * 12
    assert data['host_build'] == 'b' * 12
    assert next(s['state'] for s in data['stages'] if s['id'] == 'sign_in') == 'not_checked'
    assert start().json()['reference'] == ref
    assert len(store.list_browser_dispatches('alice')) == 1
    assert len(sent) == 1
    assert client().get('/api/browser-diagnostics').json()['reports'][0]['reference'] == ref


def test_owner_auth_and_guessed_references_are_isolated(rig):
    r = start(); ref = r.json()['reference']
    assert TestClient(main.app).get('/api/browser-diagnostics').status_code == 401
    assert start(client('bob')).status_code == 404
    assert client('bob').get('/api/browser-diagnostics/' + ref).status_code == 404
    assert client('bob').get('/api/browser-diagnostics').json()['reports'] == []
    assert [b['id'] for b in client().get('/api/browser-diagnostics').json()['bots']] == ['browser-alice']


def test_malformed_json_does_not_create_a_dispatch(rig):
    assert client().post('/api/browser-diagnostics', content='{', headers={'Content-Type': 'application/json'}).status_code == 400
    assert not rig[0].list_browser_dispatches('alice')


@pytest.mark.parametrize('url', ['http://example.com', 'https://127.0.0.1', 'https://localhost',
                                 'https://example.com?token=private', 'https://user:pass@example.com',
                                 'https://example.com/bookings', 'https://example.com/#token'])
def test_unsafe_targets_are_rejected_without_queueing(rig, url):
    assert start(url=url).status_code == 400
    assert not rig[0].list_browser_dispatches('alice')


def test_preflight_block_and_old_host_are_saved_but_never_queued(rig):
    store, manager = rig
    response = start(url='https://other.example').json()
    assert response['status'] == 'failed' and response['code'] == 'site_permissions'
    assert not store.claim_browser_dispatches('test', host_ids=['host-alice'])
    manager.channel_for('host-alice').browser_diagnostics = False
    response = start(request_id='d' * 32).json()
    assert response['status'] == 'failed' and response['code'] == 'host_update_required'


def test_saved_offline_failure_and_unchanged_request_survive_reconnect(rig):
    store, manager = rig
    manager.detach(manager.channel_for('host-alice'))
    response = start().json()
    assert response['code'] == 'host_offline'
    hosts.mark_online('host-alice')
    assert start().json()['reference'] == response['reference']
    assert start().json()['status'] == 'failed'
    assert start(url='https://example.com/different').status_code == 409
    assert len(store.list_browser_dispatches('alice')) == 1


def test_expired_orphan_is_visible_without_restarting_or_dispatching(rig):
    store, manager = rig
    ref = start().json()['reference']
    store._conn.execute("UPDATE browser_dispatches SET deadline_at='2000-01-01T00:00:00+00:00' WHERE dispatch_id=?", (ref,))
    store._conn.commit()
    response = client().get('/api/browser-diagnostics/' + ref).json()
    assert response['code'] == 'dispatch_timeout' and response['status'] == 'failed'
    assert store.get_browser_dispatch(ref)['status'] == 'pending'  # GET is read-only.
    assert bridge.BrowserDispatchPoller(store, manager).tick() == 0
    assert store.get_browser_dispatch(ref)['status'] == 'failed'


def test_booking_failure_report_has_reference_but_no_private_content(rig):
    store, _ = rig
    private = 'private-account-and-page-content'
    ref = store.submit_browser_dispatch(owner='alice', bot_id='browser-alice', host_id='host-alice',
        task_id='workout-fixture', task_text=json.dumps({'workout_browser': {'private': private}}))
    store.fail_browser_dispatch(ref, 'HostUnavailable: browser dispatch deadline expired ' + private)
    response = client().get('/api/browser-diagnostics')
    assert response.json()['reports'][0]['code'] == 'dispatch_timeout'
    assert response.json()['reports'][0]['reference'] == ref
    assert private not in response.text and 'sealed' not in response.text


def test_pausing_after_admission_blocks_delivery_without_replay(rig):
    store, manager = rig
    ref = start().json()['reference']
    bots.set_status('browser-alice', 'paused')
    channel = manager.channel_for('host-alice'); sent = []
    channel._send_raw = sent.append
    bridge.BrowserDispatchPoller(store, manager).tick()
    report = client().get('/api/browser-diagnostics/' + ref).json()
    assert report['code'] == 'bot_inactive'
    assert not any(f['type'] == 'task' for f in sent)


def test_duplicate_probe_ids_are_atomic_across_store_connections(rig, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    store, _ = rig
    other = CloudTaskStore(store.db_path)
    barrier = Barrier(2)
    def submit(db):
        barrier.wait(timeout=3)
        return db.submit_browser_dispatch(owner='alice', bot_id='browser-alice', host_id='host-alice',
            task_id='probe-race', task_text=diag.wire('https://example.com'), dispatch_id='bd-' + 'f' * 16)
    try:
        with ThreadPoolExecutor(2) as pool:
            assert list(pool.map(submit, [store, other])) == ['bd-' + 'f' * 16] * 2
        assert len(store.list_browser_dispatches('alice')) == 1
    finally: other.close()


@pytest.mark.parametrize('reason,code', [
    ('HostUnavailable: browser dispatch state unreadable', 'dispatch_state_unreadable'),
    ('HostUnavailable: browser host task cancelled', 'cancelled'),
    ('HostUnavailable: browser host connection lost', 'connection_lost'),
    ('HostUnavailable: browser host dispatch deadline expired', 'dispatch_timeout'),
])
def test_distinct_dispatch_failures_do_not_claim_host_offline(reason, code):
    import workout_booking_schema
    assert diag.failure(reason) == code
    answer = workout_booking_schema.browser_problem(reason + ' Diagnostic: bd-' + 'a' * 16)
    assert 'bd-' + 'a' * 16 in answer
    assert 'agent is running' not in answer


@pytest.mark.parametrize('code', ['session_unverified', 'session_verification_blocked'])
def test_unverified_session_reports_are_preserved_without_login_instructions(code):
    report = diag.report({'result': diag.host_result(code), 'status': 'failed',
                          'dispatch_id': 'bd-' + 'c' * 16})
    assert report['code'] == code
    assert 'Sign in through' not in report['message']
    assert next(s['state'] for s in report['stages'] if s['id'] == 'sign_in') == 'failed'


def reconnect_body(**changes):
    return {'bot_id': 'browser-alice', 'url': 'https://example.com/login',
            'action': 'start', 'lease': '', 'request_id': 'a' * 32, **changes}


def enable_reconnect(rig):
    channel = rig[1].channel_for('host-alice')
    channel.browser_reconnect = True
    calls = []
    def send(f):
        if f['type'] != 'browser_reconnect': return
        calls.append(f['payload'])
        channel.handle(channels.frame('browser_reconnect_result', {
            'request_id': f['payload']['request_id'], 'state': 'starting', 'lease': 'e' * 32,
            'viewer_url': 'https://fixture.ts.net/vnc.html', 'cookies': 'private-fixture'}))
    channel._send_raw = send
    return calls


def test_private_reconnect_auth_owner_isolation_and_safe_host_roundtrip(rig):
    calls = enable_reconnect(rig)
    assert TestClient(main.app).post('/api/browser-diagnostics/reconnect', json=reconnect_body()).status_code == 401
    assert client('bob').post('/api/browser-diagnostics/reconnect', json=reconnect_body()).status_code == 404
    assert client('bob').get('/api/browser-diagnostics/reconnect/browser-alice').status_code == 404
    assert not calls
    result = client().post('/api/browser-diagnostics/reconnect', json=reconnect_body())
    assert result.status_code == 200, result.text
    assert result.json()['state'] == 'starting' and result.json()['lease'] == 'e' * 32
    assert calls[0]['owner'] == 'alice' and calls[0]['bot_id'] == 'browser-alice'
    assert 'private-fixture' not in result.text
    assert not rig[0].list_browser_dispatches('alice')  # No browser task or booking submission.


def test_reconnect_ack_retry_uses_one_command_identity_and_status_is_nonstarting(rig):
    calls = enable_reconnect(rig)
    for _ in range(2):
        assert client().post('/api/browser-diagnostics/reconnect', json=reconnect_body()).status_code == 200
    assert calls[0] == calls[1]
    response = client().get('/api/browser-diagnostics/reconnect/browser-alice')
    assert response.status_code == 200 and calls[-1]['action'] == 'status' and calls[-1]['url'] == ''
    assert calls[-1]['lease'] == ''
    # Same acknowledgement identity cannot select a different host-side command.
    client().post('/api/browser-diagnostics/reconnect', json=reconnect_body(url='https://example.com/other'))
    assert calls[-1]['request_id'] == calls[0]['request_id'] and calls[-1]['url'] != calls[0]['url']


@pytest.mark.parametrize('changes', [
    {'owner': 'bob'}, {'bot_id': []}, {'url': 'http://example.com'},
    {'url': 'https://example.com?token=private'}, {'lease': 'not-an-id'}, {'action': 'reset'},
])
def test_invalid_private_reconnect_does_not_contact_host(rig, changes):
    calls = enable_reconnect(rig)
    assert client().post('/api/browser-diagnostics/reconnect', json=reconnect_body(**changes)).status_code == 400
    assert not calls


def test_private_reconnect_rechecks_bot_policy_and_site_permissions(rig):
    calls = enable_reconnect(rig)
    assert client().post('/api/browser-diagnostics/reconnect', json=reconnect_body(url='https://other.example/login')).status_code == 403
    bots.set_status('browser-alice', 'paused')
    assert client().post('/api/browser-diagnostics/reconnect', json=reconnect_body()).status_code == 409
    assert not calls
