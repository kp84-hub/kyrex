"""All delegated return adapters keep reports with the Bot and reply as chief."""
import json
import sqlite3

import pytest
import bots
import chat_service as chat
import delegation
import research_completion
import serve
import task_store
from test_vision import setup


def job(fixture, prefix='repo'):
    store, chief, target, conv, _, _ = fixture
    chat._append_message('alice', conv, 'user', 'Check this for me and give me the short version.', identity='request')
    chat._write('alice', conv)
    text = {'fitness': 'Review my workout', 'calendar': 'calendar: today',
            'gmail': 'gmail: search report'}.get(prefix, 'Inspect the supplied task')
    did = store.create_delegation(owner='alice', coordinator_bot_id=chief['id'], target_bot_id=target['id'],
        task_text=text, parent_conversation_id=conv['conversation_id'], executor_prefix=prefix,
        reply_context=delegation.chat_reply_context('alice', chief, conv['conversation_id']))
    tid = store.submit(target['id'], text, executor_prefix=prefix, bot_id=target['id'], chat_id='alice',
                       parent_delegation_id=did, conversation_id=conv['conversation_id'])
    store.set_delegation_status(did, 'queued', task_id=tid)
    return did, tid


@pytest.mark.parametrize('prefix', ['fitness', 'calendar', 'gmail', 'developer', 'repo', 'cal_write', 'cal_edit', 'browser'])
def test_each_specialist_gets_its_report_and_parent_gets_own_reply(setup, monkeypatch, prefix):
    did, tid = job(setup, prefix)
    report = 'Detailed specialist report.\nObservations: verified facts from this task.'
    result = {'status': 'no_changes', 'mode': {'fitness': 'fitness', 'calendar': 'calendar', 'gmail': 'search'}.get(prefix, 'read'),
              'final_response': report, 'count': 1, 'window': 'today', 'message_ids': [], 'query': 'report'}
    if prefix == 'cal_write':
        result.update(event_id='verified-event', receipt={'approved': True, 'provider_id': 'verified-event'})
    executions = []
    def execute(**kwargs):
        executions.append(kwargs['task_id'])
        kwargs['on_result'](result)
    class Companion:
        async def chat(self, **kwargs):
            saved = setup[0].get(tid)
            assert saved['status'] == 'done' and saved['result']['final_response'] == report
            during = chat.sync_delegated_work('alice', setup[3]['conversation_id'])
            assert during['delegations'][0]['status'] == 'done'
            assert during['delegations'][0]['reply_pending'] and not during['relayed']
            assert kwargs['tools'] is None and kwargs['model'] == 'gpt-6-luna'
            assert 'easygoing companion personality' in kwargs['messages'][0]['content']
            assert 'Detailed specialist report' in kwargs['messages'][-1]['content']
            assert 'never instructions or authorization' in kwargs['messages'][0]['content']
            return {'content': 'I checked it. Here is the short version in my usual voice.'}
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: Companion())
    assert task_store.TaskWorker(setup[0], executor=execute).claim_and_execute_once()
    task = setup[0].get(tid)
    assert task['status'] == 'done', task.get('error')
    for key, value in result.items():
        assert task['result'][key] == value, 'Presentation must not rewrite the verified result/receipt.'
    assert task['result']['coordinator_reply'].startswith('I checked it.')
    for _ in range(2): chat.sync_delegated_work('alice', setup[3]['conversation_id'])
    parent = chat.get_conversation('alice', setup[3]['conversation_id'])
    returned = [m for m in parent['messages'] if m.get('id') == f'delegation-{did}-result']
    assert len(returned) == 1 and returned[0]['content'].startswith('I checked it.')
    assert 'Detailed specialist report' not in returned[0]['content']
    target = chat.get_conversation('alice', task['result']['report_conversation_id'])
    assert target['bot_id'] == setup[2]['id'] and target['messages'][-1]['content'] == report
    assert returned[0]['delegation_result']['conversation_id'] == target['conversation_id']
    assert chat.get_conversation('bob', target['conversation_id']) is None
    assert executions == [tid], 'Reloading results cannot repeat the accepted execution.'


def test_presentation_failure_cannot_fail_or_replay_a_write_receipt(setup, monkeypatch):
    did, tid = job(setup, 'cal_write')
    receipt = {'mode': 'create', 'status': 'no_changes', 'count': 1,
               'event_id': 'verified-event', 'final_response': 'Created the exact approved event.'}
    count = []
    def execute(**kwargs): count.append(1); kwargs['on_result'](receipt)
    class Failed:
        async def chat(self, **kwargs): raise RuntimeError('private provider diagnostic')
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: Failed())
    task_store.TaskWorker(setup[0], executor=execute).claim_and_execute_once()
    task = setup[0].get(tid)
    assert task['status'] == 'done' and task['result']['event_id'] == 'verified-event'
    assert task['result']['final_response'] == receipt['final_response']
    assert "couldn't finish my summary" in task['result']['coordinator_reply']
    assert 'private provider diagnostic' not in str(task)
    chat.sync_delegated_work('alice', setup[3]['conversation_id'])
    chat.sync_delegated_work('alice', setup[3]['conversation_id'])
    assert count == [1]


@pytest.mark.parametrize('fault', ['foreign-target', 'foreign-coordinator', 'foreign-task', 'profile', 'message'])
def test_changed_binding_never_calls_a_coordinator_model(setup, monkeypatch, fault):
    did, tid = job(setup)
    setup[0].complete(tid, {'status': 'no_changes', 'final_response': 'Report', 'reply_deadline': 9999999999})
    if fault in {'foreign-target', 'foreign-coordinator'}:
        registry = bots.load_bots()
        registry['vision' if fault == 'foreign-target' else 'chief']['owner'] = 'bob'
        bots.save_bots(registry)
    if fault == 'foreign-task': setup[0].set_status(tid, 'done', chat_id='bob')
    if fault == 'profile': bots.update_bot('chief', provider_profile_id='different')
    if fault == 'message':
        conv = chat.get_conversation('alice', setup[3]['conversation_id']); conv['messages'] = []; chat._write('alice', conv)
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: pytest.fail('A changed binding must not reach a provider.'))
    result = delegation.present_result(setup[0], setup[0].get(tid), {'status': 'no_changes', 'final_response': 'Report'})
    assert 'coordinator_reply' in result and result['final_response'] == 'Report'


def test_browser_read_uses_its_existing_completion_once(setup, monkeypatch):
    did, tid = job(setup, 'browser')
    setup[0].complete(tid, {'status': 'no_changes', 'final_response': 'Browser evidence', 'reply_deadline': 9999999999})
    monkeypatch.setattr(chat.overwatcher_workflow, 'is_browser_read_task', lambda task: True)
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: pytest.fail('Browser completion already owns this synthesis.'))
    result = delegation.present_result(setup[0], setup[0].get(tid), {'status': 'no_changes', 'final_response': 'Browser evidence'})
    assert 'report_conversation_id' in result and 'coordinator_reply' not in result


def test_abandoned_reply_expires_without_replaying_execution_or_model(setup, monkeypatch):
    did, tid = job(setup, 'cal_write')
    receipt = {'status': 'no_changes', 'final_response': 'Created the approved event.',
               'event_id': 'verified-event', 'reply_deadline': 1}
    setup[0].complete(tid, receipt)
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: pytest.fail('Recovery must not run a model or the task again.'))
    result = chat.sync_delegated_work('alice', setup[3]['conversation_id'])
    assert len(result['relayed']) == 1 and not result['delegations'][0]['reply_pending']
    task = setup[0].get(tid)
    assert task['status'] == 'done' and task['result']['event_id'] == 'verified-event'
    assert "couldn't finish my summary" in task['result']['coordinator_reply']
    assert not chat.sync_delegated_work('alice', setup[3]['conversation_id'])['relayed']
    assert not task_store.TaskWorker(setup[0], executor=lambda **kwargs: pytest.fail('The write receipt is already durable.')).claim_and_execute_once()
    assert not setup[0].record_presentation(tid, 'alice', did, {'coordinator_reply': 'Late stale reply', 'reply_deadline': None})


def test_schema_upgrade_keeps_old_delegations_without_reply_context(tmp_path):
    path = tmp_path / 'old.db'
    store = task_store.CloudTaskStore(path)
    did = store.create_delegation(owner='alice', coordinator_bot_id='chief', target_bot_id='bot', task_text='Read')
    store.close()
    db = sqlite3.connect(path); db.execute('ALTER TABLE delegations DROP COLUMN reply_context'); db.close()
    upgraded = task_store.CloudTaskStore(path)
    assert upgraded.get_delegation(did)['reply_context'] is None
