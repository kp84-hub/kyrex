"""Real inbox/task persistence, source bindings, and coordinator projection."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time

import pytest

import coordinator_notifications as relay
import notification_api as api
from test_notifications import setup


@pytest.fixture
def f(setup):
    f = setup
    f.coordinator = 'd' * 32
    f.registry['coordinator'] = {'id': 'coordinator', 'owner': 'alice',
        'name': 'The Overwatcher', 'status': 'running', 'policy': {'bot:delegate': 0}}
    f.chat._conv_path('alice', f.coordinator).write_text(json.dumps({
        'conversation_id': f.coordinator, 'bot_id': 'coordinator',
        'messages': [], 'updated_at': '2020-01-01T00:00:00+00:00'}))
    f.opened = []
    def open_bot(owner, bot):
        assert owner == 'alice' and bot == 'coordinator'
        f.opened.append((owner, bot))
        return json.loads(f.chat._conv_path(owner, f.coordinator).read_text())
    f.chat.open_bot_conversation = open_bot
    f.project = lambda: relay.project_alerts('alice', open_bot('alice', 'coordinator'),
        store=f.store, registry=f.registry, chat=f.chat)
    return f


def test_direct_bot_review_reaches_existing_coordinator_without_moving_or_approving(f):
    tid = f.task()
    f.tasks.set_status(tid, 'running')
    aid = f.tasks.persist_approval_request(tid, 'alice-session', 'review', 2,
        'private-review-token', 'Private email body', 'Private payload')
    f.service.tick()
    alert = f.store.list('alice')['notifications'][0]
    assert alert['conversation'] == f.coordinator and alert['source_conversation'] == 'a' * 32
    messages = f.project()['messages']
    assert len(messages) == 1 and 'needs your review' in messages[0]['content']
    assert messages[0]['delegation_result']['conversation_id'] == 'a' * 32
    assert messages[0]['delegation_result']['bot_name'] == 'Alice Bot'
    assert messages[0]['coordinator_notification']['id'] == alert['id']
    assert f.tasks.get(tid)['conversation_id'] == 'a' * 32
    assert f.tasks.get_pending_approval(tid)['approval_id'] == aid
    assert f.tasks.get_pending_approval(tid)['decision'] == 'pending'
    assert f.tasks.status(tid) == 'awaiting_approval'
    output = json.dumps(messages) + f.store.path.read_bytes().decode('latin1')
    assert 'private-review-token' not in output and 'Private email body' not in output
    assert 'Private payload' not in output
    assert len(f.tasks.list_tasks()) == 1


def test_saved_change_and_calendar_review_relay_without_new_reads_or_model_calls(f):
    change = f.store.record('alice', 'trainer:change-one', 'automation',
        'Calendar Bot · Trainer schedule updated', 'a' * 32)
    cid = 'e' * 32
    f.chat._conv_path('alice', cid).write_text(json.dumps({'conversation_id': cid, 'title': 'Calendar suggestions'}))
    suggestion = 'f' * 32
    f.store.record('alice', 'calendar-suggestion:' + suggestion, 'automation', 'Calendar suggestion ready', cid)
    f.service.tick()
    messages = f.project()['messages']
    assert len(messages) == 2
    assert any(m.get('calendar_suggestion') == {'id': suggestion} for m in messages)
    assert f.store.get('alice', change)['conversation'] == f.coordinator
    assert f.tasks.list_tasks() == []
    assert json.loads(f.chat._conv_path('alice', f.coordinator).read_text())['messages'] == []


def test_restarts_concurrent_observers_and_projection_keep_one_identity(f):
    nid = f.store.record('alice', 'saved-change', 'automation', 'Changed', 'a' * 32)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: relay.route_alerts(f.chat, f.store), range(2)))
    restarted = api.NotificationService(f.tasks, f.chat, store=f.store)
    restarted.tick(); restarted.tick()
    assert len(f.store.list('alice')['notifications']) == 1
    conv = f.project()
    again = relay.project_alerts('alice', conv, store=f.store, registry=f.registry, chat=f.chat)
    assert len(again['messages']) == 1 and again['messages'][0]['id'] == 'bot-alert-' + nid
    f.store.mark_read('alice', [nid])
    assert f.store.list('alice')['unread'] == 0 and len(f.project()['messages']) == 1
    f.store.resolve('alice', source='saved-change')
    assert not relay.project_alerts('alice', conv, store=f.store, registry=f.registry, chat=f.chat)['messages']


@pytest.mark.parametrize('problem', ['stopped', 'foreign', 'restricted', 'ambiguous', 'deleted_source', 'reassigned_source'])
def test_invalid_destinations_keep_original_alert_and_never_cross_owners(f, problem):
    nid = f.store.record('alice', 'change', 'automation', 'Changed', 'a' * 32)
    if problem == 'stopped': f.registry['coordinator']['status'] = 'stopped'
    elif problem == 'foreign': f.registry['coordinator']['owner'] = 'bob'
    elif problem == 'restricted': f.registry['coordinator']['policy'] = {'bot:delegate': 'deny'}
    elif problem == 'ambiguous':
        f.registry['another'] = {**f.registry['coordinator'], 'id': 'another'}
    elif problem == 'deleted_source': f.chat._conv_path('alice', 'a' * 32).unlink()
    else: f.registry['bot-a']['owner'] = 'bob'
    f.service.tick()
    assert f.store.get('alice', nid)['conversation'] == 'a' * 32
    assert not f.opened and not f.store.list('bob')['notifications']
    assert not f.tasks.list_tasks()


def test_current_owner_and_bot_binding_are_rechecked_after_routing(f):
    nid = f.store.record('alice', 'change', 'automation', 'Changed', 'a' * 32)
    f.service.tick(); assert f.project()['messages']
    f.registry['bot-a']['owner'] = 'bob'
    assert not f.project()['messages']
    f.service.tick()
    assert f.store.get('alice', nid)['conversation'] == 'a' * 32


def test_superseded_review_disappears_from_overwatcher_and_notification_inbox(f):
    tid = f.task(); f.tasks.set_status(tid, 'running')
    f.tasks.persist_approval_request(tid, 'alice-session', 'review', 2, 'token', 'Review', '')
    f.service.tick(); assert len(f.project()['messages']) == 1
    f.tasks.set_status(tid, 'done'); f.service.tick()
    rows = f.store.list('alice')['notifications']
    assert len(rows) == 1 and rows[0]['kind'] == 'done'
    assert len(f.project()['messages']) == 1
    assert f.project()['messages'][0]['coordinator_notification']['kind'] == 'done'


def test_read_history_tests_and_already_coordinator_results_do_not_create_chat_noise(f):
    old = f.store.record('alice', 'history', 'done', 'Old result', 'a' * 32)
    f.store.mark_read('alice', [old])
    f.store.record('alice', 'test', 'test', 'Test notification', 'a' * 32)
    f.store.record('alice', 'native-parent', 'done', 'Already in parent', f.coordinator)
    f.service.tick()
    assert not f.project()['messages']
    assert f.store.get('alice', old)['conversation'] == 'a' * 32


def test_projection_is_not_written_into_transcript(f, monkeypatch):
    import chat_service
    f.store.record('alice', 'change', 'automation', 'Changed', 'a' * 32)
    f.service.tick()
    conv = f.project()
    monkeypatch.setattr(chat_service, '_conv_path', f.chat._conv_path)
    chat_service._write('alice', conv)
    assert json.loads(f.chat._conv_path('alice', f.coordinator).read_text())['messages'] == []


@pytest.mark.parametrize('kind,filename,table,state,clock,ttl', [
    ('email', 'email_drafts.sqlite3', 'drafts', 'draft', 'created', 86400),
    ('booking', 'bookings.sqlite3', 'bookings', 'proposed', 'updated', 600),
])
def test_native_reviews_are_announced_and_retired_without_reading_payloads_or_deciding(
        f, kind, filename, table, state, clock, ttl):
    import native_review_notifications as native
    from paths import data_dir
    path = data_dir() / filename
    identity, revision = '1' * 32, '2' * 32
    now = time.time()
    with sqlite3.connect(path) as db:
        db.execute(f'CREATE TABLE {table} (id TEXT,owner TEXT,conversation TEXT,revision TEXT,state TEXT,{clock} REAL,sealed TEXT)')
        db.execute(f'INSERT INTO {table} VALUES (?,?,?,?,?,?,?)',
            (identity, 'alice', 'a' * 32, revision, state, now, 'PAYLOAD MUST NOT BE DECRYPTED'))
    native.observe(f.chat, f.store, now=now)
    native.observe(f.chat, f.store, now=now)
    f.service.tick()
    rows = f.store.list('alice')['notifications']
    assert len(rows) == 1 and rows[0]['kind'] == 'awaiting_review'
    assert rows[0]['conversation'] == f.coordinator
    assert 'needs your review' in f.project()['messages'][0]['content']
    assert b'PAYLOAD MUST NOT BE DECRYPTED' not in f.store.path.read_bytes()
    with sqlite3.connect(path) as db:
        assert db.execute(f'SELECT state FROM {table}').fetchone()[0] == state
        db.execute(f'UPDATE {table} SET state=?', ('cancelled',))
    native.observe(f.chat, f.store, now=now)
    assert f.store.list('alice')['notifications'] == []
    assert f.tasks.list_tasks() == []


def test_phone_preview_uses_owned_chat_reference_and_expiration_without_phone_command(f):
    import native_review_notifications as native
    from paths import data_dir
    path = data_dir() / 'device_messages.sqlite3'
    identity = '3' * 32
    now = time.time()
    source = f.chat._conv_path('alice', 'a' * 32)
    conv = json.loads(source.read_text())
    conv['messages'] = [{'id': 'preview', 'message_send': {'id': identity}}]
    source.write_text(json.dumps(conv))
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE message_sends (id TEXT,owner TEXT,state TEXT,expires REAL,data TEXT)')
        db.execute('INSERT INTO message_sends VALUES (?,?,?,?,?)',
            (identity, 'alice', 'ready', now + 30, 'PRIVATE MESSAGE PAYLOAD'))
        db.execute('INSERT INTO message_sends VALUES (?,?,?,?,?)',
            ('4' * 32, 'bob', 'ready', now + 30, 'OTHER OWNER'))
    native.observe(f.chat, f.store, now=now)
    f.service.tick()
    assert len(f.project()['messages']) == 1
    assert not f.store.list('bob')['notifications']
    assert b'PRIVATE MESSAGE PAYLOAD' not in f.store.path.read_bytes()
    native.observe(f.chat, f.store, now=now + 31)
    assert not f.store.list('alice')['notifications']
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT state FROM message_sends WHERE id=?', (identity,)).fetchone()[0] == 'ready'


def test_actual_chat_and_sidebar_project_alerts_without_a_new_owner_turn(f, monkeypatch):
    import chat_service
    import bots
    monkeypatch.setattr(bots, 'load_bots', lambda: f.registry)
    monkeypatch.setattr(chat_service, '_conv_path', f.chat._conv_path)
    monkeypatch.setattr(chat_service, '_user_dir', lambda owner: f.chat._conv_path(owner, '0' * 32).parent)
    monkeypatch.setattr(chat_service, '_task_store', lambda: f.tasks)
    f.store.record('alice', 'saved-automation', 'automation', 'Calendar Bot · Schedule changed', 'a' * 32)
    f.service.tick()
    conv = chat_service.get_conversation('alice', f.coordinator)
    assert len(conv['messages']) == 1
    assert 'Schedule changed' in conv['messages'][0]['content']
    sidebar = next(row for row in chat_service.list_conversations('alice') if row['conversation_id'] == f.coordinator)
    assert 'Schedule changed' in sidebar['latest_update'] and sidebar['message_count'] == 1
    assert json.loads(f.chat._conv_path('alice', f.coordinator).read_text())['messages'] == []


def test_notification_settings_explain_destination_and_ambiguity_without_foreign_metadata(f, monkeypatch):
    monkeypatch.setattr(relay.bots, 'load_bots', lambda: f.registry)
    f.registry['foreign'] = {**f.registry['coordinator'], 'id': 'foreign', 'owner': 'bob', 'name': 'PRIVATE FOREIGN BOT'}
    selected = f.client.get('/api/notifications/settings').json()
    assert selected['coordinator'] == {'state': 'selected', 'bot_name': 'The Overwatcher'}
    assert 'PRIVATE FOREIGN BOT' not in json.dumps(selected)
    f.registry['another'] = {**f.registry['coordinator'], 'id': 'another'}
    assert f.client.get('/api/notifications/settings').json()['coordinator']['state'] == 'ambiguous'
    f.registry['another']['status'] = 'stopped'
    f.registry['coordinator']['status'] = 'stopped'
    assert f.client.get('/api/notifications/settings').json()['coordinator']['state'] == 'missing'
