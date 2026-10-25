"""Workout continuations use the existing durable Calendar task and review."""
import asyncio
import json

import pytest

import bots
import chat_api
import chat_service as chat
import dev_bot
import serve
from task_store import CloudTaskStore, TaskWorker
from test_level6_calendar_batch import LINES


REQUEST = 'Can you add these to my calendar'


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'calendar-handoff-tests')
    registry = {
        'chief': {'id': 'chief', 'owner': 'alice', 'status': 'running',
                  'policy': serve.COORDINATOR_PRESET, 'rift': str(tmp_path)},
        'calendar': {'id': 'calendar', 'owner': 'alice', 'status': 'running',
                     'policy': serve.calendar_preset_policy(), 'rift': str(tmp_path)},
    }
    monkeypatch.setattr(bots, 'load_bots', lambda: registry)
    store = CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(chat, '_task_store', lambda: store)
    monkeypatch.setattr(chat, '_resolve_provider', lambda *a, **k: pytest.fail('Reached LLM'))
    monkeypatch.setattr(chat.bot_provider, 'resolve_bot_provider', lambda *a, **k: pytest.fail('Reached Bot LLM'))
    monkeypatch.setattr(chat, 'EngineSession', lambda *a, **k: pytest.fail('Started engine'))
    conv = chat.create_conversation('alice', bot_id='chief')
    # Same shape as the screenshot: fenced message body, separate trainer rows.
    body = '\n'.join(line.replace(' — trainer: ', '\nTrainer: ') for line in LINES)
    chat._append_message('alice', conv, 'assistant',
        'Preview only — nothing sent.\n\n```text\n#L6Workout\n'
        '🏋️ Level 6 — Workout Week\n' + body + '\n```')
    conv['gmail_selected'] = {'id': 'unrelated-email', 'facts': {}}
    chat._write('alice', conv)
    return store, conv['conversation_id'], registry


def test_screenshot_request_streams_existing_review_and_persists_result(handoff):
    store, cid, _ = handoff
    executions = []

    def execute(**kwargs):
        assert kwargs['executor_prefix'] == 'level6'
        assert kwargs['task_text'] == serve.LEVEL6_CALENDAR_BATCH_REQUEST
        assert kwargs['chat_id'] == 'alice' and kwargs['session_key'] == 'calendar'
        executions.append(kwargs['task_id'])
        kwargs['on_approval']('review-1', 1, 'private-review-token',
                              'Add 6 workouts', 'Six exact event titles and dates')
        assert store.get(kwargs['task_id'])['status'] == 'awaiting_approval'
        kwargs['on_approval_resolved']('review-1', 'approved')
        kwargs['on_result']({'status': 'no_changes', 'mode': 'level6_calendar_batch',
                              'count': 6, 'final_response': 'Added 6 Level 6 workouts to your calendar.'})

    worker = TaskWorker(store, worker_id='calendar-handoff', executor=execute,
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()

    async def run():
        source = chat.stream_chat('alice', cid, REQUEST, request_id='calendar-request')
        return [json.loads(frame.split('data: ', 1)[1])
                async for frame in chat_api._drive_stream(source, 'calendar-request', cid)]

    try:
        frames = asyncio.run(run())
    finally:
        worker.stop()
    assert frames[-1]['type'] == 'done'
    assert frames[-1]['content'] == 'Added 6 Level 6 workouts to your calendar.'
    approvals = [f for f in frames if f['type'] == 'approval_request']
    assert len(approvals) == 1 and approvals[0]['task_id'] == executions[0]
    assert 'private-review-token' not in json.dumps(frames)
    saved = chat.get_conversation('alice', cid)
    assert saved['messages'][-1]['content'] == frames[-1]['content']
    assert saved['messages'][-1]['id'] == f'task-{executions[0]}-result'
    assert [m['content'] for m in saved['messages'] if m['role'] == 'user'] == [REQUEST]
    assert len(store.tasks_for_conversation(cid, 'alice')) == 1
    assert store.tasks_for_conversation(cid, 'bob') == []
    # A replay after the completed result replaced the preview still resolves
    # to the accepted task, without a model, worker or new Calendar execution.
    replay = asyncio.run(run())
    assert replay[-1] == frames[-1]
    assert len(store.tasks_for_conversation(cid, 'alice')) == 1
    assert len(executions) == 1
    async def changed_request():
        return [f async for f in chat.stream_chat('alice', cid,
            'Add it to a different calendar', request_id='calendar-request')]
    with pytest.raises(chat.ChatUnavailable, match='already used'):
        asyncio.run(changed_request())


def test_retries_reattach_to_pending_or_same_completed_request(handoff):
    store, cid, registry = handoff
    calendar = registry['calendar']
    first = dev_bot.submit_level6_calendar_batch_task(
        'alice', calendar, store, cid, request_id='one')
    assert store.claim_next('worker')['task_id'] == first
    store.persist_approval_request(first, 'calendar', 'review-1', 1, 'secret', 'Review', 'Exact dates')
    # A new HTTP request from Retry still observes the pending review.
    again = dev_bot.submit_level6_calendar_batch_task(
        'alice', calendar, store, cid, request_id='two')
    assert again == first and store.get(first)['status'] == 'awaiting_approval'
    store.resolve_approval_request(first, 'review-1', 'approved')
    store.complete(first, {'final_response': 'Added approved workouts.'})
    # A transport replay of the accepted request cannot execute it again.
    assert dev_bot.submit_level6_calendar_batch_task(
        'alice', calendar, store, cid, request_id='one') == first
    assert len(store.tasks_for_conversation(cid, 'alice')) == 1
    assert store.claim_next('worker') is None
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_level6_calendar_batch_task('bob', calendar, store, cid, request_id='one')


def test_legacy_delegated_calendar_work_cannot_be_duplicated(handoff):
    store, cid, registry = handoff
    did = store.create_delegation(owner='alice', coordinator_bot_id='chief',
        target_bot_id='calendar', task_text=serve.LEVEL6_CALENDAR_BATCH_REQUEST,
        parent_conversation_id=cid, executor_prefix='level6')
    tid = store.submit('calendar', serve.LEVEL6_CALENDAR_BATCH_REQUEST,
        executor_prefix='level6', bot_id='calendar', chat_id='alice',
        conversation_id='specialist-chat', parent_delegation_id=did, resolve_bot=False)
    store.set_delegation_status(did, 'queued', task_id=tid)
    with pytest.raises(dev_bot.DevBotError, match='already in progress'):
        dev_bot.submit_level6_calendar_batch_task('alice', registry['calendar'],
            store, cid, request_id='retry-old-handoff')
    assert len(store.list_tasks()) == 1 and store.get(tid)['status'] == 'queued'


@pytest.mark.parametrize('problem', ['missing', 'foreign', 'stopped', 'restricted', 'ambiguous', 'workspace', 'cancelled'])
def test_unavailable_targets_and_cancelled_requests_cannot_submit(handoff, problem):
    store, cid, registry = handoff
    kwargs = {}
    if problem == 'missing':
        del registry['calendar']
    elif problem == 'foreign':
        registry['calendar']['owner'] = 'bob'
    elif problem == 'stopped':
        registry['calendar']['status'] = 'stopped'
    elif problem == 'restricted':
        registry['calendar']['policy'] = {'cal:list': 0, 'cal:create': 'deny'}
    elif problem == 'ambiguous':
        registry['second'] = {**registry['calendar'], 'id': 'second'}
    elif problem == 'workspace':
        kwargs['workspace_id'] = 'another-workspace'
    else:
        kwargs['cancel_event'] = asyncio.Event()
        kwargs['cancel_event'].set()

    async def run():
        return [f async for f in chat.stream_chat('alice', cid, REQUEST, **kwargs)]

    if problem == 'cancelled':
        assert asyncio.run(run())[-1]['status'] == 'cancelled'
    else:
        with pytest.raises(chat.ChatUnavailable):
            asyncio.run(run())
    assert store.tasks_for_conversation(cid, 'alice') == []
