"""Durable, isolated final synthesis with real SQLite tasks and outbox recovery."""
import copy
import json
from types import SimpleNamespace

import pytest
import chat_service as production
import research_completion as completion
import serve
from task_store import CloudTaskStore


@pytest.fixture
def journey(tmp_path):
    tasks = CloudTaskStore(tmp_path / 'tasks.sqlite3')
    outbox = completion.CompletionStore(tmp_path / 'outbox.sqlite3')
    bot = dict(id='chief', owner='alice', status='running',
               policy=serve.COORDINATOR_PRESET, model='selected-model', provider_profile_id='p1')
    target = dict(id='browser', owner='alice')
    anchor = dict(id='u1', role='user', content='Find local events today',
                  created_at='2026-10-03T14:00:00+00:00')
    conv = dict(conversation_id='c1', bot_id='chief', messages=[anchor])
    task_text = json.dumps({'actions': [{'action': 'navigate', 'url': 'https://example.com/events'}, {'action': 'read'}]})
    did = tasks.create_delegation(owner='alice', coordinator_bot_id='chief', target_bot_id='browser',
        task_text=task_text, parent_conversation_id='c1', executor_prefix='browser')
    tid = tasks.submit('browser', task_text, executor_prefix='browser', bot_id='browser',
        chat_id='alice', parent_delegation_id=did, conversation_id='c1')
    tasks.set_delegation_status(did, 'queued', task_id=tid)
    calls = []
    state = {'response': {'content': '**Local festival**\n- Time: 10 AM–4 PM\n- Source: [Town](https://example.com/events)'}, 'after': None}

    class Provider:
        async def chat(self, **kw):
            calls.append(kw)
            if state['after']:
                state['after']()
            return state['response']

    def factory(name, key, **kw):
        assert (name, key, kw['base_url'], kw['extra_headers']) == (
            'openai', 'owner-secret', 'https://selected.example/v1', {'X-Owner': 'alice'})
        return Provider()

    chat = SimpleNamespace(
        get_conversation=lambda owner, cid: copy.deepcopy(conv) if (owner, cid) == ('alice', 'c1') else None,
        resolve_bot_for_user=lambda owner, bid: bot,
        bots=SimpleNamespace(load_bots=lambda: {'browser': target}),
        serve=serve, overwatcher_workflow=production.overwatcher_workflow,
        _task_store=lambda: tasks, _safe_result_summary=production._safe_result_summary,
        bot_provider=SimpleNamespace(resolve_bot_provider=lambda owner, b: dict(
            provider='openai', api_key='owner-secret', base_url='https://selected.example/v1',
            headers={'X-Owner': 'alice'}, model='selected-model')),
        get_provider=factory, build_coordinator_context=lambda owner, b: 'Answer concisely with sources.',
        sanitize_assistant_text=production.sanitize_assistant_text,
        _provider_error_content=production._provider_error_content)
    identity = outbox.register('alice', 'c1', bot, anchor, [did], '2026-10-03')
    return SimpleNamespace(tasks=tasks, outbox=outbox, bot=bot, target=target, conv=conv,
        anchor=anchor, did=did, tid=tid, calls=calls, state=state, chat=chat, identity=identity)


def finish(j):
    j.tasks.complete(j.tid, {'status': 'no_changes', 'mode': 'browser_read',
        'final_response': 'Local festival, October 3, 10 AM–4 PM',
        'browser_sources': ['https://example.com/events'], 'api_key': 'PRIVATE'})


def test_pending_then_restart_publishes_once(journey):
    j = journey
    completion.process_once(j.chat, j.outbox)
    assert j.calls == []
    finish(j)
    reopened = completion.CompletionStore(j.outbox.path)
    completion.process_once(j.chat, reopened)
    completion.process_once(j.chat, j.outbox)
    assert len(j.calls) == 1 and j.calls[0]['tools'] is None
    assert j.calls[0]['model'] == 'selected-model'
    payload = json.loads(j.calls[0]['messages'][1]['content'])
    assert payload['original_request'] == j.anchor['content']
    assert 'PRIVATE' not in str(payload) and 'owner-secret' not in str(payload)
    assert '2026-10-03' in j.calls[0]['messages'][0]['content']
    conv = completion.project_answers(reopened, 'alice', copy.deepcopy(j.conv))
    completion.project_answers(reopened, 'alice', conv)
    assert len(conv['messages']) == 2
    assert conv['messages'][-1]['id'] == 'research-' + j.identity + '-answer'
    assert not reopened.answers('bob', 'c1') and not reopened.answers('alice', 'c2')


def test_atomic_claim_and_expired_worker_cannot_finish(journey):
    j = journey
    old = j.outbox.claim(j.identity)
    other = completion.CompletionStore(j.outbox.path)
    assert other.claim(j.identity) is None
    with other.db() as db:
        db.execute('UPDATE research_completions SET lease=0 WHERE id=?', (j.identity,))
    other.waiting()
    new = other.claim(j.identity)
    assert not j.outbox.finish(old, 'stale answer')
    assert other.finish(new, 'current answer')
    assert j.outbox.answers('alice', 'c1')[0]['answer'] == 'current answer'


@pytest.mark.parametrize('change', ['new-request', 'paused', 'model', 'profile', 'owner', 'task-owner', 'target-owner', 'cancelled'])
def test_superseded_or_unowned_research_is_cancelled(journey, change):
    j = journey
    finish(j)
    if change == 'new-request': j.conv['messages'].append(dict(id='u2', role='user', content='Different request'))
    if change == 'paused': j.bot['status'] = 'paused'
    if change == 'model': j.bot['model'] = 'different-model'
    if change == 'profile': j.bot['provider_profile_id'] = 'p2'
    if change == 'owner': j.bot['owner'] = 'bob'
    if change == 'task-owner': j.tasks.set_status(j.tid, 'done', chat_id='bob')
    if change == 'target-owner': j.target['owner'] = 'bob'
    if change in {'cancelled', 'rejected'}: j.tasks.set_status(j.tid, change)
    completion.process_once(j.chat, j.outbox)
    assert not j.calls and not j.outbox.answers('alice', 'c1')
    assert j.outbox.waiting() == []


def test_approval_is_never_executed(journey):
    j = journey
    j.tasks.set_status(j.tid, 'awaiting_approval')
    completion.process_once(j.chat, j.outbox)
    assert not j.calls and len(j.outbox.waiting()) == 1
    assert j.tasks.get(j.tid)['status'] == 'awaiting_approval'


def test_new_request_during_provider_call_discards_answer(journey):
    j = journey
    finish(j)
    j.state['after'] = lambda: j.conv['messages'].append(dict(id='u2', role='user', content='Another request'))
    completion.process_once(j.chat, j.outbox)
    assert len(j.calls) == 1 and not j.outbox.answers('alice', 'c1')


def test_provider_failure_is_bounded_and_has_safe_final_message(journey):
    j = journey
    finish(j)
    j.state['response'] = {'error': 'secret traceback', 'content': ''}
    for _ in range(4): completion.process_once(j.chat, j.outbox)
    assert len(j.calls) == 2
    answer = j.outbox.answers('alice', 'c1')[0]['answer']
    assert 'could not generate' in answer and 'secret' not in answer


def test_only_pending_turn_is_registered(journey, monkeypatch):
    j = journey
    fresh = completion.CompletionStore(j.outbox.path.parent / 'registration.sqlite3')
    monkeypatch.setattr(completion, 'default_store', lambda: fresh)
    session = SimpleNamespace(_browser_research_ids=[j.did, j.did], delegation_ctx={'bot': j.bot})
    completion.register_pending(j.chat, 'alice', 'c1', session, 'wrong-anchor')
    assert not fresh.waiting()
    completion.register_pending(j.chat, 'alice', 'c1', session, 'u1')
    assert len(fresh.waiting()) == 1
    assert json.loads(fresh.waiting()[0]['delegations']) == [j.did]
    finish(j)
    another = completion.CompletionStore(j.outbox.path.parent / 'completed.sqlite3')
    monkeypatch.setattr(completion, 'default_store', lambda: another)
    completion.register_pending(j.chat, 'alice', 'c1', session, 'u1')
    assert not another.waiting()


def test_real_conversation_reads_and_sidebar_project_durable_answer(journey, monkeypatch, tmp_path):
    j = journey
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path / 'chat-data'))
    monkeypatch.setattr(completion, 'default_store', lambda: j.outbox)
    monkeypatch.setattr(production, '_task_store', lambda: j.tasks)
    production._write('alice', j.conv)
    finish(j)
    completion.process_once(j.chat, j.outbox)
    first = production.get_conversation('alice', 'c1')
    second = production.get_conversation('alice', 'c1')
    assert first['messages'] == second['messages']
    assert first['messages'][-1]['id'].startswith('research-')
    listed = production.list_conversations('alice')
    assert listed[0]['message_count'] == 2
    assert 'Local festival' in listed[0]['latest_update']
    assert production.get_conversation('bob', 'c1') is None
