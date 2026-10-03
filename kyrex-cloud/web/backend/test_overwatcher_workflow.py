"""Real durable Browser task results return to the coordinator safely."""
import json
import time
from types import SimpleNamespace

import pytest
import chat_service as chat
import dev_bot
import overwatcher_workflow as flow
import serve
from task_store import CloudTaskStore


@pytest.fixture
def journey(tmp_path, monkeypatch):
    store = CloudTaskStore(tmp_path / 'tasks.sqlite3')
    target = {'id': 'browser', 'owner': 'alice', 'status': 'running', 'policy': serve.browser_preset_policy()}
    monkeypatch.setattr(chat.bots, 'load_bots', lambda: {'browser': target})
    monkeypatch.setattr(chat, '_task_store', lambda: store)
    frame = {'target_bot_id': 'browser', 'task': json.dumps({'actions': [
        {'action': 'navigate', 'url': 'https://example.com/farm'}, {'action': 'read'}]})}
    did = store.create_delegation(owner='alice', coordinator_bot_id='chief', target_bot_id='browser',
        task_text=frame['task'], parent_conversation_id='c1', executor_prefix='browser')
    tid = store.submit('browser', frame['task'], executor_prefix='browser', bot_id='browser',
        chat_id='alice', parent_delegation_id=did, conversation_id='c1')
    store.set_delegation_status(did, 'queued', task_id=tid)
    session = SimpleNamespace(delegation_ctx={'owner': 'alice', 'bot': {'id': 'chief'}, 'conversation_id': 'c1'})
    submitted = chat.delegation.public_view(store.get_delegation(did))
    return store, session, frame, submitted, tid, target


def test_completed_browser_read_returns_evidence_without_duplicate_relay(journey):
    store, session, frame, submitted, tid, _ = journey
    store.complete(tid, {'status': 'no_changes', 'mode': 'browser_read',
        'final_response': 'Farm address: 4400 Mid Pines Rd\n' + 'Footer ' * 200,
        'browser_sources': ['https://example.com/farm-final'], 'api_key': 'NOT-FOR-MODEL',
        'browser_ref': 'INTERNAL-BROWSER-REF'})
    result = flow.follow_browser_read(chat, dev_bot, session, frame, submitted)
    assert result['status'] == 'done'
    assert '4400 Mid Pines Rd' in result['result_summary']
    assert 'https://example.com/farm-final' in result['result_summary']
    assert 'NOT-FOR-MODEL' not in str(result) and 'INTERNAL-BROWSER-REF' not in str(result)
    assert store.get_delegation(submitted['delegation_id'])['relayed_at']


@pytest.mark.parametrize('state', ['failed', 'cancelled', 'awaiting_approval'])
def test_failure_or_approval_is_reported_without_claiming_completion(journey, state):
    store, session, frame, submitted, tid, _ = journey
    store.set_status(tid, state)
    result = flow.follow_browser_read(chat, dev_bot, session, frame, submitted)
    assert result['status'] == state
    assert not result.get('approval_token')
    assert store.get(tid)['status'] == state
    if state == 'awaiting_approval': assert not store.get_delegation(submitted['delegation_id'])['relayed_at']


@pytest.mark.parametrize('change', ['owner', 'conversation', 'task-owner', 'target-owner'])
def test_foreign_or_stale_relationship_never_becomes_evidence(journey, change):
    store, session, frame, submitted, tid, target = journey
    store.complete(tid, {'status': 'no_changes', 'final_response': 'PRIVATE RESULT'})
    if change == 'owner': session.delegation_ctx['owner'] = 'bob'
    if change == 'conversation': session.delegation_ctx['conversation_id'] = 'other'
    if change == 'task-owner': store.set_status(tid, 'done', chat_id='bob')
    if change == 'target-owner': target['owner'] = 'bob'
    assert flow.follow_browser_read(chat, dev_bot, session, frame, submitted) == submitted
    assert not store.get_delegation(submitted['delegation_id'])['relayed_at']


def test_slow_work_and_stop_preserve_the_existing_task(journey):
    store, session, frame, submitted, tid, _ = journey
    session._browser_follow_deadline = time.monotonic() - 1
    assert flow.follow_browser_read(chat, dev_bot, session, frame, submitted) == submitted
    session._browser_follow_deadline = time.monotonic() + 100
    session._browser_follow_cancel = lambda: True
    assert flow.follow_browser_read(chat, dev_bot, session, frame, submitted) == submitted
    assert store.get(tid)['status'] == 'queued'
    assert not store.get(tid)['cancel_requested']


def test_non_read_plan_and_repo_task_are_not_followed(journey):
    store, session, frame, submitted, tid, _ = journey
    frame['task'] = '{"actions":[{"action":"screenshot","path":"image.png"}]}'
    assert flow.follow_browser_read(chat, dev_bot, session, frame, submitted) == submitted
    frame['task'] = '{"actions":[{"action":"read"}]}'
    store.set_status(tid, 'queued', executor_prefix='repo')
    assert flow.follow_browser_read(chat, dev_bot, session, frame, submitted) == submitted
