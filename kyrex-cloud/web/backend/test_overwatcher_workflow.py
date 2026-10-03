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
    session._browser_follow_remaining = 0
    assert flow.follow_browser_read(chat, dev_bot, session, frame, submitted) == submitted
    session._browser_follow_remaining = 100
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


def test_browser_research_poll_keeps_evidence_off_assistant_transcript(journey, monkeypatch):
    store, session, frame, submitted, tid, _ = journey
    body = 'Celebrate Fuquay-Varina — Oct 3, 10 AM–4 PM\n' + 'Contact us footer ' * 100
    store.complete(tid, {'status': 'no_changes', 'final_response': body})
    monkeypatch.setattr(chat, '_append_message',
                        lambda *a, **k: pytest.fail('Browser evidence became an assistant reply'))
    synced = chat.sync_delegated_work('alice', 'c1')
    assert synced['relayed'] == []
    assert synced['delegations'][0]['relayed'] is True
    assert body.strip() in synced['delegations'][0]['result_summary']
    assert store.get_delegation(submitted['delegation_id'])['relayed_at']
    # Polling can win the race; the model still receives the full evidence.
    evidence = flow.follow_browser_read(chat, dev_bot, session, frame, submitted)
    assert body.strip() in evidence['result_summary']
    assert chat.sync_delegated_work('alice', 'c1')['relayed'] == []


def test_legacy_browser_read_keeps_event_heading_instead_of_footer(journey):
    store, _, _, _, tid, _ = journey
    store.complete(tid, {'status': 'no_changes', 'final_response':
        'Celebrate Fuquay-Varina — October 3, 2026, 10 AM–4 PM\n' + 'Footer ' * 400})
    status, summary = chat._safe_result_summary(store, tid)
    assert status == 'done'
    assert 'Celebrate Fuquay-Varina — October 3, 2026, 10 AM–4 PM' in summary


@pytest.mark.parametrize('text,expected', [
    ('{"actions":[{"action":"navigate","url":"https://example.com"},{"action":"read"}]}', True),
    ('{"actions":[{"action":"click","selector":"button"},{"action":"read"}]}', False),
    ('{"actions":[{"action":"screenshot"}]}', False),
    ('not json', False),
])
def test_only_read_research_suppresses_receipt(text, expected):
    assert flow.is_browser_read_task({'executor_prefix': 'browser', 'task_text': text}) is expected
    assert not flow.is_browser_read_task({'executor_prefix': 'repo', 'task_text': text})


def test_reasoning_between_reads_does_not_spend_wait_budget(journey, monkeypatch):
    store, session, frame, submitted, tid, _ = journey
    clock = [0.0]
    monkeypatch.setattr(flow.time, 'monotonic', lambda: clock[0])
    store.complete(tid, {'status': 'no_changes', 'final_response': 'Town event listing'})
    assert flow.follow_browser_read(chat, dev_bot, session, frame, submitted)['status'] == 'done'
    assert session._browser_follow_remaining == flow.TURN_WAIT_SECONDS
    # The model spends two minutes reasoning before a second page read.
    clock[0] = 120.0
    did = store.create_delegation(owner='alice', coordinator_bot_id='chief', target_bot_id='browser',
        task_text=frame['task'], parent_conversation_id='c1', executor_prefix='browser')
    second = store.submit('browser', frame['task'], executor_prefix='browser', bot_id='browser',
        chat_id='alice', parent_delegation_id=did, conversation_id='c1')
    store.set_delegation_status(did, 'queued', task_id=second)
    view = chat.delegation.public_view(store.get_delegation(did))
    def finish(_seconds):
        clock[0] += 0.5
        store.complete(second, {'status': 'no_changes', 'final_response': 'Event detail: 10 AM–4 PM'})
    monkeypatch.setattr(flow.time, 'sleep', finish)
    answer = flow.follow_browser_read(chat, dev_bot, session, frame, view)
    assert answer['status'] == 'done'
    assert '10 AM–4 PM' in answer['result_summary']
    assert session._browser_follow_remaining == flow.TURN_WAIT_SECONDS - 0.5


def test_status_query_waits_for_existing_browser_read(journey, monkeypatch):
    store, session, _, submitted, tid, _ = journey
    clock = [0.0]
    monkeypatch.setattr(flow.time, 'monotonic', lambda: clock[0])
    def finish(_seconds):
        clock[0] += 1
        store.complete(tid, {'status': 'no_changes', 'final_response': 'Verified event details'})
    monkeypatch.setattr(flow.time, 'sleep', finish)
    views = flow.follow_browser_statuses(chat, dev_bot, session, [submitted])
    assert views[0]['status'] == 'done'
    assert 'Verified event details' in views[0]['result_summary']
    assert views[0]['task_id'] == tid
    assert session._browser_follow_remaining == flow.TURN_WAIT_SECONDS - 1


def test_status_query_does_not_wait_on_foreign_conversation(journey, monkeypatch):
    _, session, _, submitted, _, _ = journey
    session.delegation_ctx['conversation_id'] = 'other'
    monkeypatch.setattr(flow.time, 'sleep', lambda _: pytest.fail('Waited on foreign conversation'))
    assert flow.follow_browser_statuses(chat, dev_bot, session, [submitted]) == [submitted]


def test_legacy_read_returns_clickable_source_input_with_honest_provenance(journey):
    store, _, _, _, tid, _ = journey
    store.complete(tid, {'status': 'no_changes', 'final_response': 'Event: 10 AM–4 PM'})
    _, summary = chat._safe_result_summary(store, tid)
    assert 'Requested page link: https://example.com/farm' in summary
    assert 'Redirect destination not recorded' in summary
    assert 'Event: 10 AM–4 PM' in summary


def test_requested_read_links_reject_secrets_and_unread_navigation():
    task = {'executor_prefix': 'browser', 'task_text': json.dumps({'actions': [
        {'action': 'navigate', 'url': 'https://user:password@example.com'}, {'action': 'read'},
        {'action': 'navigate', 'url': 'https://example.com/?token=SECRET'}, {'action': 'read'},
        {'action': 'navigate', 'url': 'https://example.com/calendar.aspx?EID=123'}, {'action': 'read'},
        {'action': 'navigate', 'url': 'https://example.com/unread'},
    ]})}
    assert flow.requested_read_links(task) == ['https://example.com/calendar.aspx?EID=123']
