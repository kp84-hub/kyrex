"""Manual retries keep the scheduled week contract and never send messages."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
import chat_service as chat
import level6_message_schedule as schedule
import serve
from task_store import CloudTaskStore


@pytest.fixture
def retry_setup(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(schedule, 'select_bot', lambda owner: {'id': 'calendar', 'rift': '/tmp/calendar'})
    store = CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(chat, '_task_store', lambda: store)
    cid = chat.ensure_level6_preview_conversation('alice', 'L6 Besties')
    return store, cid


def at(day=11, hour=20):
    return datetime(2026, 10, day, hour, tzinfo=ZoneInfo('America/New_York'))


def test_failed_scheduled_preview_retries_and_delivers_reviewable_draft(retry_setup):
    store, cid = retry_setup
    old = schedule.preview_task_id('alice', '2026-10-12')
    store.submit(session_key='calendar', task_text=serve.LEVEL6_MESSAGE_PREVIEW_REQUEST,
                 executor_prefix='level6', chat_id='alice', conversation_id=cid,
                 task_id=old, resolve_bot=False)
    store.fail(old, 'Chrome startup failed')
    job = schedule.submit_now(store, owner='alice', conversation_id=cid, now=at())
    assert job['task_id'] != old and job['week'] == '2026-10-12'
    assert schedule.expected_week(job['task_id'], 'alice') == '2026-10-12'
    assert schedule.expected_week(job['task_id'], 'bob') is None
    task = store.get(job['task_id'])
    assert task['task_text'] == serve.LEVEL6_MESSAGE_PREVIEW_REQUEST
    assert task['job_contract']['mode'] == 'level6_preview'
    again = schedule.submit_now(store, owner='alice', conversation_id=cid, now=at())
    assert again['task_id'] == job['task_id'] and again['reused']
    assert len(store.tasks_for_conversation(cid, 'alice')) == 2
    body = '#L6Workout\nVerified week'
    store.complete(job['task_id'], {'mode': 'level6_preview', 'status': 'no_changes',
        'count': 6, 'lines': ['Workout'] * 6, 'message_text': body,
        'final_response': 'Preview only — nothing sent.\n' + body})
    messages = chat.get_conversation('alice', cid)['messages']
    assert len(messages) == 2 and 'Chrome startup failed' in messages[0]['content']
    assert messages[-1]['message_draft'] == {'recipient': 'L6 Besties', 'text': body}
    assert all('message_send' not in message for message in messages)
    assert store.get(old)['status'] == 'failed'
    assert chat.list_conversations('alice')[0]['automation'] == 'level6-weekly-preview'


@pytest.mark.parametrize('day,week', [(11, '2026-10-12'), (12, '2026-10-12'),
                                    (17, '2026-10-12'), (18, '2026-10-19')])
def test_manual_preview_week_selection(retry_setup, day, week):
    store, cid = retry_setup
    job = schedule.submit_now(store, owner='alice', conversation_id=cid, now=at(day))
    assert job['week'] == week
    assert schedule.expected_week(job['task_id'], 'alice') == week


def test_parallel_clicks_queue_only_one_attempt(retry_setup):
    store, cid = retry_setup
    barrier = Barrier(2)
    def click():
        connection = CloudTaskStore(store.db_path)
        raw = connection._conn
        class ConcurrentInsert:
            synchronized = False
            def execute(self, sql, *args):
                cursor = raw.execute(sql, *args)
                if sql == 'SELECT 1 FROM tasks WHERE task_id = ?' and not self.synchronized:
                    self.synchronized = True
                    barrier.wait(timeout=5)
                return cursor
            def __getattr__(self, name):
                return getattr(raw, name)
        connection._conn = ConcurrentInsert()
        read = connection.tasks_for_conversation
        def synchronized_read(*args):
            rows = read(*args)
            barrier.wait(timeout=5)
            return rows
        connection.tasks_for_conversation = synchronized_read
        result = schedule.submit_now(connection, owner='alice', conversation_id=cid, now=at())
        assert not raw.in_transaction, 'duplicate submissions must release the failed write transaction'
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: click(), range(2)))
    assert results[0]['task_id'] == results[1]['task_id']
    assert len(store.tasks_for_conversation(cid, 'alice')) == 1


def test_separate_preview_threads_do_not_collide(retry_setup):
    store, cid = retry_setup
    other = chat.ensure_level6_preview_conversation('alice', 'Other group')
    first = schedule.submit_now(store, owner='alice', conversation_id=cid, now=at())
    second = schedule.submit_now(store, owner='alice', conversation_id=other, now=at())
    assert first['task_id'] != second['task_id']


def test_retry_after_failure_and_invalid_bindings(retry_setup, monkeypatch):
    store, cid = retry_setup
    first = schedule.submit_now(store, owner='alice', conversation_id=cid, now=at())
    store.fail(first['task_id'], 'Post unavailable')
    second = schedule.submit_now(store, owner='alice', conversation_id=cid, now=at())
    assert second['task_id'] != first['task_id']
    store.fail(second['task_id'], 'Post unavailable')
    monkeypatch.setattr(schedule, 'select_bot', lambda owner: None)
    with pytest.raises(ValueError, match='same host'):
        schedule.submit_now(store, owner='alice', conversation_id=cid, now=at())
    assert len(store.tasks_for_conversation(cid, 'alice')) == 2


def test_endpoint_auth_scope_and_fixed_preview(retry_setup, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import chat_api
    store, cid = retry_setup
    def owner(request):
        if not request.headers.get('x-owner'):
            raise HTTPException(401)
        return request.headers['x-owner']
    monkeypatch.setattr(chat_api, '_require_user', owner)
    app = FastAPI(); app.include_router(chat_api.router)
    client = TestClient(app)
    url = f'/api/conversations/{cid}/level6-preview'
    assert client.post(url).status_code == 401
    assert client.post(url, headers={'x-owner': 'bob'}).status_code == 404
    ordinary = chat.create_conversation('alice')['conversation_id']
    assert client.post(f'/api/conversations/{ordinary}/level6-preview', headers={'x-owner': 'alice'}).status_code == 400
    result = client.post(url, headers={'x-owner': 'alice'}, json={
        'owner': 'bob', 'task_text': 'send-facebook-weekly', 'recipient': 'Someone else', 'week': '2000-01-03'})
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    task = store.get(result.json()['task_id'])
    assert task['chat_id'] == 'alice' and task['conversation_id'] == cid
    assert task['task_text'] == serve.LEVEL6_MESSAGE_PREVIEW_REQUEST
    assert len(store.tasks_for_conversation(cid, 'alice')) == 1


@pytest.mark.parametrize('suffix', ['-retry-x-1', '-retry-abcdef123456-0', '-retry-abcdef123456-1-send'])
def test_malformed_retry_identity_fails_closed(suffix):
    with pytest.raises(ValueError, match='Invalid scheduled'):
        schedule.expected_week(schedule.preview_task_id('alice', '2026-10-12') + suffix, 'alice')
