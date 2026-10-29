"""Opening a Bot is navigation, with owner isolation and process-safe reuse."""
import multiprocessing

import pytest
from fastapi.testclient import TestClient

import bots
import chat_service as chat
import main
from task_store import CloudTaskStore
from test_developer_delegation_conversations import setup


def test_reopens_latest_bot_thread_without_new_work_or_lost_history(setup):
    store,_,parent=setup
    old=chat.create_conversation('alice',bot_id='dev')
    chat._append_message('alice',old,'assistant','An older saved result.'); chat._write('alice',old)
    latest=chat.create_conversation('alice',bot_id='dev')
    chat._append_message('alice',latest,'assistant','The latest saved result.'); chat._write('alice',latest)
    for _ in range(3):
        opened=chat.open_bot_conversation('alice','dev')
        assert opened['conversation_id']==latest['conversation_id']
        assert opened['messages'][-1]['content']=='The latest saved result.'
    assert chat.get_conversation('alice',old['conversation_id'])['messages'][-1]['content']=='An older saved result.'
    assert len([row for row in chat.list_conversations('alice') if row.get('bot_id')=='dev'])==2
    assert not store.list_tasks() and not store.list_delegations(owner='alice')
    assert chat.get_conversation('alice',parent['conversation_id'])['bot_id']=='chief'


def test_endpoint_auth_ownership_and_lifecycle_are_rechecked(setup,monkeypatch):
    store,_,_=setup
    monkeypatch.setitem(main.sessions,'navigation-session','alice')
    client=TestClient(main.app)
    assert client.post('/api/bots/dev/conversation',json={}).status_code==401
    client.cookies.set('session','navigation-session')
    first=client.post('/api/bots/dev/conversation',json={})
    assert first.status_code==200
    identity=first.json()['conversation_id']
    for _ in range(2):
        result=client.post('/api/bots/dev/conversation',json={'owner':'bob','conversation_id':'invented'})
        assert result.status_code==200 and result.json()['conversation_id']==identity
    assert client.post('/api/bots/foreign/conversation',json={}).status_code==400
    bots.set_status('dev','paused')
    assert client.post('/api/bots/dev/conversation',json={}).status_code==400
    assert len([row for row in chat.list_conversations('alice') if row.get('bot_id')=='dev'])==1
    assert not store.list_tasks()


def test_same_bot_for_different_owners_keeps_separate_histories(setup):
    _,_,_=setup
    alice=chat.open_bot_conversation('alice','dev')
    bob=chat.open_bot_conversation('bob','foreign')
    assert alice['conversation_id']!=bob['conversation_id']
    assert chat.get_conversation('bob',alice['conversation_id']) is None
    assert chat.get_conversation('alice',bob['conversation_id']) is None
    with pytest.raises(chat.BotUnavailable): chat.open_bot_conversation('bob','dev')


def test_simultaneous_processes_create_one_bot_thread(setup,monkeypatch):
    store,_,_=setup
    # Each process uses its own SQLite connection, as the real web/worker do.
    monkeypatch.setattr(chat,'_task_store',lambda:CloudTaskStore(store.db_path))
    context=multiprocessing.get_context('fork')
    gate=context.Barrier(4); results=context.Queue()
    def open_thread():
        try:
            gate.wait(timeout=8)
            results.put(chat.open_bot_conversation('alice','dev')['conversation_id'])
        except Exception as exc: results.put('error:'+str(exc))
    processes=[context.Process(target=open_thread) for _ in range(4)]
    try:
        for process in processes: process.start()
        ids=[results.get(timeout=10) for _ in processes]
        for process in processes: process.join(timeout=5)
        assert all(process.exitcode==0 for process in processes)
        assert len(set(ids))==1 and not ids[0].startswith('error:')
        assert len([row for row in chat.list_conversations('alice') if row.get('bot_id')=='dev'])==1
    finally:
        for process in processes:
            if process.is_alive(): process.terminate(); process.join(timeout=2)


def test_legacy_new_thread_cannot_displace_the_ongoing_bot_chat(setup):
    _,_,_=setup
    first=chat.open_bot_conversation('alice','dev')
    second=chat.create_conversation('alice',bot_id='dev')
    assert first['conversation_id']!=second['conversation_id']
    assert chat.open_bot_conversation('alice','dev')['conversation_id']==first['conversation_id']


def test_bot_creation_endpoint_reuses_chat_but_ordinary_new_chat_stays_independent(setup, monkeypatch):
    store, _, _ = setup
    monkeypatch.setitem(main.sessions, 'ongoing-session', 'alice')
    client = TestClient(main.app); client.cookies.set('session', 'ongoing-session')
    first = client.post('/api/conversations', json={'bot_id': 'dev'}).json()
    second = client.post('/api/conversations', json={'bot_id': 'dev', 'title': 'New copy'}).json()
    assert first['conversation_id'] == second['conversation_id']
    ordinary = [client.post('/api/conversations', json={}).json() for _ in range(2)]
    assert ordinary[0]['conversation_id'] != ordinary[1]['conversation_id']
    assert not store.list_tasks()


def test_legacy_bot_threads_roll_into_timeline_with_original_action_bindings(setup, monkeypatch):
    store, _, _ = setup
    first = chat.create_conversation('alice', bot_id='dev')
    second = chat.create_conversation('alice', bot_id='dev')
    foreign = chat.create_conversation('bob', bot_id='foreign')
    workspace = chat.create_conversation('alice', bot_id='dev'); workspace['workspace_id'] = 'isolated'
    for conv, content, date in [(first, 'Earlier task', '2026-10-28T10:00:00Z'),
                                (second, 'Later task', '2026-10-29T10:00:00Z'),
                                (foreign, 'Private other owner', '2026-10-29T11:00:00Z'),
                                (workspace, 'Separate workspace', '2026-10-29T12:00:00Z')]:
        message = chat._append_message('bob' if conv is foreign else 'alice', conv, 'assistant', content, identity='same-id')
        message['created_at'] = date
        if conv is first: message['message_draft'] = {'recipient': 'Test group'}
        chat._write('bob' if conv is foreign else 'alice', conv)
    before = {path: path.read_bytes() for path in chat._user_dir('alice').glob('*.json')}
    timeline = chat.get_conversation_timeline('alice', second['conversation_id'])
    messages = timeline['messages']
    assert [m['content'] for m in messages] == ['Earlier task', 'Later task']
    assert len({m['id'] for m in messages}) == 2
    assert messages[0]['source_conversation_id'] == first['conversation_id']
    assert messages[0]['source_message_id'] == 'same-id'
    assert messages[1]['id'] == 'same-id', 'Current streaming IDs must remain stable.'
    assert chat.get_conversation('alice', first['conversation_id'])['messages'][0]['id'] == 'same-id'
    assert before == {path: path.read_bytes() for path in before}
    assert chat.get_conversation_timeline('bob', second['conversation_id']) is None
    assert chat.get_conversation_timeline('alice', workspace['conversation_id'])['messages'][0]['content'] == 'Separate workspace'
    assert [m['content'] for m in chat.recent_conversation_messages(chat.bot_dialogue('alice', second))] == ['Earlier task', 'Later task']
    monkeypatch.setitem(main.sessions, 'timeline-session', 'alice')
    client = TestClient(main.app); client.cookies.set('session', 'timeline-session')
    assert client.get('/api/conversations/' + second['conversation_id']).json()['messages'] == messages
    assert not store.list_tasks(), 'Reading a rolled-up chat must never execute work.'
