"""Offline regressions for conversational creates, status reads and replays."""
import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import bots
import cal_writer
import calendar_writer_executor
import chat_service
import connectors
import delegation
import dev_bot
import serve
import shared_connected_tools as shared
from job_contracts import JobContractError, validate_request_route
from task_store import CloudTaskStore, DuplicateTaskId


NOW = datetime(2026, 10, 26, 10, tzinfo=ZoneInfo('America/New_York'))
REQUESTS = [
    'add a calender event for tomorrow at 8:30am to 9:30am for Milo at Eastside Animal Clinic for a vitamin injection',
    'this is a new task. Can you add this to my calender Milo at 8:30am to 9:30am tomorrow at Eastside Animal Clinic for a vitamin injection',
    'Please add Milo tomorrow from 8:30am to 9:30am at Eastside Animal Clinic for a vitamin injection',
]


@pytest.fixture(autouse=True)
def connected(monkeypatch):
    shared.install(chat_service, dev_bot)
    monkeypatch.setattr(connectors, 'default_store', lambda: SimpleNamespace(
        calendar_read_available=lambda owner: True,
        calendar_write_available=lambda owner: True))


@pytest.mark.parametrize('text', REQUESTS)
def test_complete_owner_request_keeps_all_facts_and_cannot_become_read(text):
    intent = cal_writer.parse_create_request(text, now=NOW)
    assert intent == {
        'title': 'Milo', 'start': '2026-10-27T08:30:00',
        'end': '2026-10-27T09:30:00', 'all_day': False,
        'location': 'Eastside Animal Clinic', 'description': 'a vitamin injection',
    }
    assert cal_writer.create_shaped(text)
    assert shared._calendar_create_request(text)
    assert serve.natural_calendar_command(text) is None
    assert serve.natural_calendar_search_command(text) is None


def test_unrelated_appointment_and_quoted_title():
    intent = cal_writer.parse_create_request(
        'Could you please schedule Design review on November 3, 2026 from 2pm to 3pm at Room 4 for prototype feedback')
    assert intent['title'] == 'Design review'
    assert intent['location'] == 'Room 4'
    assert intent['description'] == 'prototype feedback'
    quoted = cal_writer.parse_create_request(
        'add "Breakfast at Tiffany\'s" tomorrow from 8am to 9am', now=NOW)
    assert quoted['title'] == "Breakfast at Tiffany's"
    assert 'location' not in quoted
    assert not shared._calendar_create_request('Can you create a parser file')


@pytest.mark.parametrize('extra', [
    'and invite Bob', 'every week', 'on November 4, 2026',
])
def test_extra_instructions_and_dates_cannot_disappear(extra):
    with pytest.raises(cal_writer.CalendarWriterError):
        cal_writer.parse_create_request(REQUESTS[0] + ' ' + extra, now=NOW)


@pytest.mark.parametrize('field,value', [
    ('location', ''), ('description', None), ('location', 'x' * 501),
    ('description', 'x' * 2001), ('location', 'Room\n4'),
])
def test_metadata_is_bounded(field, value):
    intent = cal_writer.parse_create_request(REQUESTS[0], now=NOW)
    with pytest.raises(cal_writer.CalendarWriterError):
        cal_writer.validate_intent({**intent, field: value})


@pytest.mark.parametrize('decision,count', [('DENIED', 0), ('APPROVED', 1)])
def test_exact_metadata_is_reviewed_before_provider_write(monkeypatch, decision, count):
    intent = cal_writer.parse_create_request(REQUESTS[0], now=NOW)
    calls = []
    def create(body):
        calls.append(body)
        return {'id': 'synthetic-event-id'}
    monkeypatch.setattr(connectors, 'default_store', lambda: SimpleNamespace(
        calendar_writer=lambda owner: SimpleNamespace(create_event=create)))
    monkeypatch.setenv('KYREX_BOT_OWNER', 'alice')
    monkeypatch.setattr('sys.argv', ['executor', '--task', json.dumps(intent)])
    monkeypatch.setattr('sys.stdin', io.StringIO('ALLOW\n' + decision + '\n'))
    output = io.StringIO()
    monkeypatch.setattr('sys.stdout', output)
    calendar_writer_executor.main()
    frames = dict((kind, json.loads(body)) for kind, body in
                  (line.split(':', 1) for line in output.getvalue().splitlines()))
    preview = frames['KYREX_APPROVAL']['detail']
    assert 'location: Eastside Animal Clinic' in preview
    assert 'description: a vitamin injection' in preview
    assert len(calls) == count
    if count:
        assert calls == [cal_writer.to_google_event(intent)]
        receipt = frames['KYREX_RESULT_JSON']['final_response']
        assert 'Location: Eastside Animal Clinic' in receipt
        assert 'Description: a vitamin injection' in receipt


def test_write_route_cannot_be_replaced_with_read_or_lossy_payload():
    request = REQUESTS[0]
    intent = cal_writer.parse_create_request(request)
    with pytest.raises(JobContractError, match='writer'):
        validate_request_route(request, 'calendar', 'calendar: tomorrow')
    with pytest.raises(JobContractError, match="owner's request"):
        validate_request_route(request, 'cal_write', json.dumps({
            key: value for key, value in intent.items() if key != 'description'}))
    validate_request_route(request, 'cal_write', json.dumps(intent))


@pytest.mark.parametrize('state', ['queued', 'awaiting_approval', 'done', 'failed', 'cancelled'])
def test_repeat_create_follows_original_task_even_after_uncertain_outcome(tmp_path, state):
    store = CloudTaskStore(db_path=tmp_path / 'tasks.db')
    intent = cal_writer.parse_create_request(REQUESTS[0], now=NOW)
    def submit(owner='alice', cid='conversation', bot='calendar', **kwargs):
        return store.submit(session_key=bot, bot_id=bot, chat_id=owner, rift='',
            task_text=json.dumps(intent), executor_prefix='cal_write',
            conversation_id=cid, resolve_bot=False, **kwargs)
    original = submit()
    store.set_status(original, state)
    assert submit(bot='other-calendar-bot') == original
    assert len(store.list_tasks()) == 1
    assert submit(owner='bob') != original
    assert submit(cid='other-conversation') != original
    explicit = submit(task_id='explicit-id')
    with pytest.raises(DuplicateTaskId):
        submit(task_id=explicit)


def test_concurrent_create_replays_across_store_connections_are_atomic(tmp_path):
    path = tmp_path / 'tasks.db'
    first, second = CloudTaskStore(db_path=path), CloudTaskStore(db_path=path)
    intent = cal_writer.parse_create_request(REQUESTS[0], now=NOW)
    def submit(store):
        return store.submit(session_key='calendar', bot_id='calendar', chat_id='alice',
            task_text=json.dumps(intent), executor_prefix='cal_write', rift='',
            conversation_id='conversation', resolve_bot=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [first, second]))
    assert results[0] == results[1]
    assert len(first.list_tasks()) == 1
    assert len(first.get_events(results[0])) == 1


def test_upgrade_recognizes_existing_json_create_and_changed_details_get_new_task(tmp_path):
    store = CloudTaskStore(db_path=tmp_path / 'tasks.db')
    intent = cal_writer.parse_create_request(REQUESTS[0], now=NOW)
    def submit(payload, **kwargs):
        return store.submit(session_key='calendar', bot_id='calendar', chat_id='alice',
            task_text=json.dumps(payload), executor_prefix='cal_write', rift='',
            conversation_id='conversation', resolve_bot=False, **kwargs)
    old = submit(intent, task_id='old-random-task-id')
    store.complete(old, {'status': 'ok', 'event_id': 'synthetic-event-id'})
    assert submit(intent) == old
    assert submit({**intent, 'location': 'Room 7'}) != old
    assert len(store.list_tasks()) == 2


@pytest.mark.parametrize('explicit', [False, True])
def test_status_turn_cannot_resubmit_stale_model_create(monkeypatch, tmp_path, explicit):
    chief = {'id': 'chief', 'owner': 'alice', 'status': 'running', 'policy': serve.COORDINATOR_PRESET}
    target = {'id': 'calendar', 'owner': 'alice', 'status': 'running', 'policy': serve.CALENDAR_WRITER_PRESET}
    monkeypatch.setattr(bots, 'load_bots', lambda: {'chief': chief, 'calendar': target})
    store = CloudTaskStore(db_path=tmp_path / 'tasks.db')
    status = 'I thought we added my workouts to my calendar'
    assert serve.natural_calendar_search_command(status) == 'calendar: search workout'
    token = shared._request_text.set(status)
    try:
        with pytest.raises(delegation.DelegationError, match='current message'):
            delegation.submit_delegation('alice', chief, 'calendar',
                'create a calendar event called Checkup on 2026-11-03 from 8am to 9am',
                store=store, parent_conversation_id='conversation',
                executor_prefix='cal_write' if explicit else 'repo')
    finally:
        shared._request_text.reset(token)
    assert store.list_tasks() == []


def test_delegation_preserves_current_owner_facts_over_lossy_model_task(monkeypatch, tmp_path):
    chief = {'id': 'chief', 'owner': 'alice', 'status': 'running', 'policy': serve.COORDINATOR_PRESET}
    target = {'id': 'calendar', 'owner': 'alice', 'status': 'running', 'policy': {}}
    monkeypatch.setattr(bots, 'load_bots', lambda: {'chief': chief, 'calendar': target})
    store = CloudTaskStore(db_path=tmp_path / 'tasks.db')
    token = shared._request_text.set(REQUESTS[0])
    try:
        result = delegation.submit_delegation('alice', chief, 'calendar',
            'create a calendar event called Milo on tomorrow from 8:30am to 9:30am',
            store=store, parent_conversation_id='conversation')
    finally:
        shared._request_text.reset(token)
    intent = json.loads(store.get(result['task_id'])['task_text'])
    assert intent == cal_writer.parse_create_request(REQUESTS[0])


@pytest.mark.parametrize('explicit', [False, True])
def test_repeat_delegation_returns_original_approval_and_receipt_identity(monkeypatch, tmp_path, explicit):
    chief = {'id': 'chief', 'owner': 'alice', 'status': 'running', 'policy': serve.COORDINATOR_PRESET}
    target = {'id': 'calendar', 'owner': 'alice', 'status': 'running', 'policy': serve.CALENDAR_WRITER_PRESET}
    monkeypatch.setattr(bots, 'load_bots', lambda: {'chief': chief, 'calendar': target})
    if explicit:
        monkeypatch.setattr(delegation, '_bot_rift_resolves', lambda bot: True)
        monkeypatch.setattr(serve, '_bot_llm_config', lambda bot: {'provider': 'offline'})
    store = CloudTaskStore(db_path=tmp_path / 'tasks.db')
    def submit():
        return delegation.submit_delegation('alice', chief, 'calendar', REQUESTS[0],
            store=store, parent_conversation_id='conversation',
            executor_prefix='cal_write' if explicit else 'repo')
    first = submit()
    store.set_status(first['task_id'], 'awaiting_approval')
    store.set_delegation_status(first['delegation_id'], 'awaiting_approval')
    assert submit() == delegation.public_view(store.get_delegation(first['delegation_id']))
    store.complete(first['task_id'], {'status': 'ok', 'event_id': 'synthetic-event-id', 'final_response': 'Created.'})
    store.set_delegation_result(first['delegation_id'], 'Created.')
    repeated = submit()
    assert repeated['delegation_id'] == first['delegation_id']
    assert repeated['status'] == 'done' and repeated['result_summary'] == 'Created.'
    assert len(store.list_tasks()) == 1 and len(store.list_delegations()) == 1


@pytest.mark.parametrize('policy', [serve.CALENDAR_PRESET, serve.DEVELOPER_PRESET])
def test_chat_create_and_workout_status_bypass_model_and_old_context(monkeypatch, tmp_path, policy):
    monkeypatch.setattr(chat_service, '_chat_root', lambda: tmp_path / 'chat')
    bot = {'id': 'helper', 'owner': 'alice', 'status': 'running', 'policy': policy,
           'rift': str(tmp_path), 'model': 'test:model'}
    monkeypatch.setattr(chat_service, 'resolve_bot_for_user', lambda *a: bot)
    monkeypatch.setattr(chat_service.bot_provider, 'resolve_bot_provider', lambda *a: {
        'provider': 'openai', 'profile': 'test', 'model': 'mock',
        'api_key': 'offline', 'base_url': None, 'headers': {}})
    monkeypatch.setattr(chat_service, '_get_engine_session', lambda *a, **k: pytest.fail('Reached model'))
    conv = chat_service.create_conversation('alice')
    conv.update(bot_id='helper', messages=[{'role': 'assistant', 'content': 'Previous repository discussion.'}])
    chat_service._write('alice', conv)
    submitted = []
    async def submit(user, saved, target, text, cid, cancel, **kwargs):
        submitted.append((text, kwargs))
        yield {'type': 'status', 'status': 'complete', 'content': 'Prepared.'}
    monkeypatch.setattr(chat_service, '_stream_writable_bot_task', submit)
    async def turn(text):
        return [frame async for frame in chat_service.stream_chat('alice', conv['conversation_id'], text)]
    asyncio.run(turn(REQUESTS[1]))
    assert submitted[-1][1]['calendar_intent'] == cal_writer.parse_create_request(REQUESTS[1])
    asyncio.run(turn('I though we added my workouts to my calendar'))
    assert submitted[-1] == ('calendar: search workout', {'mode': 'calendar'})
    assert len(submitted) == 2


def test_current_owner_turn_survives_chat_worker_thread(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_service, '_chat_root', lambda: tmp_path / 'chat')
    monkeypatch.setattr(chat_service, '_resolve_provider', lambda *a, **k: {
        'provider': 'openai', 'model': 'mock', 'api_key': 'offline', 'base_url': None})
    observed = []
    class Provider:
        async def chat(self, **kwargs):
            observed.append(shared._request_text.get())
            return {'role': 'assistant', 'content': 'Acknowledged.'}
    monkeypatch.setattr(chat_service, 'get_provider', lambda *a, **k: Provider())
    async def turn():
        return [frame async for frame in chat_service.stream_chat('alice', '', 'This is my current request')]
    asyncio.run(turn())
    assert observed == ['This is my current request']
    assert shared._request_text.get() == ''


def test_stale_workout_error_explains_dates_and_preserves_diagnostic(monkeypatch):
    replies, audits = [], []
    reason = ('Level6Error: stale weekly post: the newest post is for the week of '
              '2026-10-19, before the current week (2026-10-26)')
    monkeypatch.setattr(serve.audit, 'log', lambda **kw: audits.append(kw))
    serve._level6_calendar_fail_closed(SimpleNamespace(bot_id='calendar'),
        'messages.send_level6', reason, 'alice', lambda owner, message: replies.append(message))
    assert '2026-10-19' in replies[0] and '2026-10-26' in replies[0]
    assert "couldn't verify this week's workouts" in replies[0]
    assert 'Previously saved calendar events are separate' in replies[0]
    assert 'Level6Error' not in replies[0]
    assert audits[0]['detail']['reason'] == reason
