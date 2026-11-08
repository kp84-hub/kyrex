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
import chat_api
import chat_service
import connectors
import delegation
import dev_bot
import serve
import shared_connected_tools as shared
from job_contracts import JobContractError, validate_request_route
from task_store import CloudTaskStore, DuplicateTaskId, TaskWorker


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


ADVENT_TITLE = '12 DAYS OF NESTLÉ® TOLL HOUSE® ADVENT CALENDAR'
ADVENT_REQUEST = f'Add {ADVENT_TITLE} to Nov. 6th. Do all day'


@pytest.mark.parametrize('preposition', ['on','for','to',''])
@pytest.mark.parametrize('ending', [' all day','. Do all day','; make it all-day'])
def test_natural_all_day_keeps_numbered_unicode_title(preposition,ending):
    request=f'Add {ADVENT_TITLE} {preposition} Nov. 6th{ending}'
    intent=cal_writer.parse_create_request(request,now=NOW)
    assert intent=={'title':ADVENT_TITLE,'start':'2026-11-06','end':'2026-11-07','all_day':True}
    assert cal_writer.to_google_event(intent)=={'summary':ADVENT_TITLE,'start':{'date':'2026-11-06'},'end':{'date':'2026-11-07'}}


def test_screenshot_all_day_uses_request_year_and_preserves_title_words():
    intent=cal_writer.parse_create_request(ADVENT_REQUEST,now=NOW)
    assert intent['title']==ADVENT_TITLE and intent['start']=='2026-11-06' and intent['end']=='2026-11-07'
    assert cal_writer.parse_create_request(ADVENT_REQUEST,now=datetime(2028,11,1))['start']=='2028-11-06'
    assert cal_writer.parse_create_request('Add "All Day Long on Ice" to November 6, 2027. Do all day',now=NOW)['title']=='All Day Long on Ice'
    with pytest.raises(cal_writer.CalendarClarification) as exc:
        cal_writer.parse_create_request(f'Add {ADVENT_TITLE}',now=NOW)
    assert exc.value.draft['title']==ADVENT_TITLE and exc.value.draft['missing']=='date'


@pytest.mark.parametrize('owner_text', [
    'Add Fair on November 6 and November 7. Do all day',
    'Add Fair on tomorrow to Nov. 6th. Do all day',
    'Add Fair every year to Nov. 6th. Do all day',
    'Add Fair and invite Bob to Nov. 6th. Do all day',
    'Add Fair at 3pm to Nov. 6th. Do all day',
    'Add Fair at 08:30 to Nov. 6th. Do all day',
    'Add Fair to Nov. 6th. Do all day and invite Bob',
    'Add Fair to Nov. 6th. Do all day for 12 days',
    'Add Fair to February 30, 2026. Do all day',
    'Add Fair to Saturday, November 6, 2026. Do all day',
])
def test_natural_all_day_rejects_ambiguous_or_extra_effects(owner_text):
    with pytest.raises(cal_writer.CalendarWriterError):cal_writer.parse_create_request(owner_text,now=NOW)


def test_screenshot_all_day_is_shared_create_and_exact_task_contract():
    intent=cal_writer.parse_create_request(ADVENT_REQUEST)
    assert shared._calendar_create_request(ADVENT_REQUEST)
    validate_request_route(ADVENT_REQUEST,'cal_write',json.dumps(intent))
    with pytest.raises(JobContractError):validate_request_route(ADVENT_REQUEST,'calendar','calendar: week')
    with pytest.raises(JobContractError):validate_request_route(ADVENT_REQUEST,'cal_write',json.dumps({**intent,'title':'Different title'}))


@pytest.mark.parametrize('decision,count',[('DENIED',0),('APPROVED',1)])
def test_screenshot_all_day_preserves_exact_review_and_requires_approval(monkeypatch,decision,count):
    intent=cal_writer.parse_create_request(ADVENT_REQUEST,now=NOW)
    calls=[]
    monkeypatch.setattr(connectors,'default_store',lambda:SimpleNamespace(calendar_writer=lambda owner:SimpleNamespace(
        create_event=lambda event:calls.append(event) or {'id':'fixture-all-day-event'})))
    monkeypatch.setenv('KYREX_BOT_OWNER','alice')
    monkeypatch.setattr('sys.argv',['executor','--task',json.dumps(intent)])
    monkeypatch.setattr('sys.stdin',io.StringIO('ALLOW\n'+decision+'\n'))
    output=io.StringIO();monkeypatch.setattr('sys.stdout',output)
    calendar_writer_executor.main()
    frames=dict((kind,json.loads(body)) for kind,body in (line.split(':',1) for line in output.getvalue().splitlines()))
    preview=frames['KYREX_APPROVAL']['detail']
    assert ADVENT_TITLE in preview and '2026-11-06' in preview and 'all day: yes' in preview
    assert len(calls)==count
    if count:assert calls==[cal_writer.to_google_event(intent)]


@pytest.fixture
def calendar_handoff(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_service, '_chat_root', lambda: tmp_path / 'chat')
    bot = {'id': 'helper', 'owner': 'alice', 'status': 'running',
           'policy': serve.CALENDAR_PRESET, 'rift': str(tmp_path), 'model': 'fixture'}
    monkeypatch.setattr(bots, 'load_bots', lambda: {'helper': bot})
    monkeypatch.setattr(chat_service, 'resolve_bot_for_user', lambda *a: bot)
    monkeypatch.setattr(chat_service.bot_provider, 'resolve_bot_provider', lambda *a: {
        'provider': 'openai', 'profile': 'fixture', 'model': 'fixture',
        'api_key': 'offline', 'base_url': None, 'headers': {}})
    monkeypatch.setattr(chat_service, '_get_engine_session',
                        lambda *a, **k: pytest.fail('Reached model'))
    store = CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(chat_service, '_task_store', lambda: store)
    conv = chat_service.create_conversation('alice')
    conv.update(bot_id='helper',
                calendar_create_draft={'title': ADVENT_REQUEST[4:], 'missing': 'date'},
                messages=[{'role': 'assistant', 'content': 'What date should I use for this event?'}])
    chat_service._write('alice', conv)
    return store, conv, bot


REMINDER_REQUEST = 'Can you set up a calendar reminder for that?'
DROP_TITLE = 'final release of the 12 Days of Nestlé Toll House Advent Calendar'
DROP_ANSWER = (
    f'The next Nestlé calendar drop is the {DROP_TITLE} on Friday, December 4, 2026 '
    'at 2:00 PM ET (National Cookie Day). The October 9 and November 6, 2026 drops have already passed. '
    'Offered to add a calendar reminder for the December 4 drop if the owner wants one.'
)


def test_reminder_followup_reuses_the_prior_event_and_asks_only_for_duration(calendar_handoff, monkeypatch):
    store, conv, bot = calendar_handoff
    bot['policy'] = serve.COORDINATOR_PRESET
    conv['messages'] = [{'role': 'user', 'content': 'When is the next Nestlé drop?'},
                        {'role': 'assistant', 'content': DROP_ANSWER}]
    chat_service._write('alice', conv)
    submitted = []

    async def submit(user, saved, target, text, cid, cancel, **kwargs):
        submitted.append(kwargs['calendar_intent'])
        yield {'type': 'status', 'status': 'complete', 'content': 'Review ready.'}

    monkeypatch.setattr(chat_service, '_stream_writable_bot_task', submit)

    async def turn(text):
        return [frame async for frame in chat_service.stream_chat('alice', conv['conversation_id'], text)]

    first = asyncio.run(turn(REMINDER_REQUEST))
    assert first[-1]['content'] == f'How long should “{DROP_TITLE}” last?'
    assert not store.list_tasks() and not submitted
    saved = chat_service.get_conversation('alice', conv['conversation_id'])
    assert saved['calendar_create_draft'] == {'title': DROP_TITLE, 'date': '2026-12-04',
                                               'time': '14:00', 'duration': None, 'missing': 'duration'}
    assert asyncio.run(turn('15 minutes'))[-1]['content'] == 'Review ready.'
    assert submitted == [{'title': DROP_TITLE, 'start': '2026-12-04T14:00:00',
                          'end': '2026-12-04T14:15:00', 'all_day': False}]


@pytest.mark.parametrize('followup, opener, reply, expected', [
    (' and are you able to buy that for me when it drops?', 'So 2 questions',
     'Go ahead, Keith — what are your two questions?', 'draft'),
    ('? Can you purchase it for me?', 'I have a quick question', 'Sure, ask away.', 'draft'),
    (' and are you able to buy that for me when it drops?', 'How is the weather?',
     'Looks sunny. What are your two questions?', 'clarify'),
    (' and buy that for me when it drops', 'So 2 questions',
     'Go ahead, Keith — what are your two questions?', 'refuse'),
])
def test_calendar_and_purchase_followup(calendar_handoff, monkeypatch, followup, opener, reply, expected):
    store, conv, bot = calendar_handoff
    bot['policy'] = serve.COORDINATOR_PRESET
    if expected != 'clarify':
        conv.pop('calendar_create_draft', None)
    title = 'The last drop of the Nestlé Toll House advent calendar'
    conv['messages'] = [
        {'role': 'user', 'content': 'When is the next Nestlé drop?'},
        {'role': 'assistant', 'content': f'{title} is Friday, December 4, 2026 at 2 p.m. ET — '
                                         'the final of three online drops (Oct 9, Nov 6, and Dec 4). '
                                         'It sells for $29.99 exclusively online.'},
        {'role': 'user', 'content': opener},
        {'role': 'assistant', 'content': reply},
    ]
    chat_service._write('alice', conv)
    submitted = []

    async def submit(user, saved, target, text, cid, cancel, **kwargs):
        assert set(kwargs) == {'calendar_intent'}
        submitted.append(kwargs['calendar_intent'])
        yield {'type': 'status', 'status': 'complete', 'content': 'Review ready.'}

    monkeypatch.setattr(chat_service, '_stream_writable_bot_task', submit)

    async def turn(text):
        return [frame async for frame in chat_service.stream_chat('alice', conv['conversation_id'], text)]

    frames = asyncio.run(turn(f'Can you add that to my calendar{followup}'))
    content = frames[-1]['content']
    assert not store.list_tasks() and not submitted
    saved = chat_service.get_conversation('alice', conv['conversation_id'])
    if expected == 'draft':
        assert content.startswith(f'How long should “{title}” last?')
        assert 'place the order yourself' in content
        assert saved['calendar_create_draft'] == {'title': title, 'date': '2026-12-04',
                                                   'time': '14:00', 'duration': None, 'missing': 'duration'}
        assert asyncio.run(turn('15 minutes'))[-1]['content'] == 'Review ready.'
        assert submitted == [{'title': title, 'start': '2026-12-04T14:00:00',
                              'end': '2026-12-04T14:15:00', 'all_day': False}]
    else:
        assert 'calendar_create_draft' not in saved
        if expected == 'clarify':
            assert 'Which event should I use?' in content and 'place the order yourself' in content
        else:
            assert 'make the calendar request separately' in content


@pytest.mark.parametrize('previous_answer', [
    DROP_ANSWER + ' The second event is on Saturday, December 5, 2026 at 3 PM ET.',
    DROP_ANSWER.replace('PM ET', 'PM Pacific Time'),
    DROP_ANSWER.replace('PM ET', 'p.m. PT'),
    DROP_ANSWER.replace('December 4, 2026 at', 'December 4 at'),
    'I can set up a calendar reminder for Friday, December 4, 2026 at 2 PM ET.',
    'The release time is still unknown.',
])
def test_reminder_followup_keeps_ambiguous_or_incomplete_context_out_of_the_draft(previous_answer):
    with pytest.raises(cal_writer.CalendarWriterError) as error:
        cal_writer.parse_create_request(REMINDER_REQUEST, previous_answer=previous_answer)
    assert not isinstance(error.value, cal_writer.CalendarClarification)


@pytest.mark.parametrize('policy', [serve.CALENDAR_PRESET, serve.COORDINATOR_PRESET])
@pytest.mark.parametrize('owner_text', [ADVENT_REQUEST, REQUESTS[1]])
def test_chat_create_reaches_real_queue_review_and_replay(calendar_handoff, policy, owner_text):
    store, conv, bot = calendar_handoff
    bot['policy'] = policy
    cid = conv['conversation_id']
    intent = cal_writer.parse_create_request(owner_text)
    executions = []

    def execute(**kwargs):
        assert kwargs['executor_prefix'] == 'cal_write'
        assert kwargs['chat_id'] == 'alice' and kwargs['session_key'] == 'helper'
        assert json.loads(kwargs['task_text']) == intent
        executions.append(kwargs['task_id'])
        kwargs['on_approval']('create-review', 1, 'private-review-token',
                              'Create event', json.dumps(intent))
        assert store.get(kwargs['task_id'])['status'] == 'awaiting_approval'
        kwargs['on_approval_resolved']('create-review', 'approved')
        kwargs['on_result']({'status': 'ok', 'event_id': 'fixture-event',
                              'final_response': 'Created the reviewed event.'})

    worker = TaskWorker(store, worker_id='create-handoff', executor=execute,
                        idle_sleep=0.01, heartbeat_interval=0.01)

    async def turn():
        source = chat_service.stream_chat('alice', cid, owner_text)
        return [json.loads(frame.split('data: ', 1)[1])
                async for frame in chat_api._drive_stream(source, 'create-request', cid)]

    worker.start()
    try:
        frames = asyncio.run(turn())
    finally:
        worker.stop()
    assert frames[-1]['type'] == 'done'
    assert frames[-1]['content'].endswith('Created the reviewed event.')
    approvals = [frame for frame in frames if frame['type'] == 'approval_request']
    assert len(approvals) == 1 and approvals[0]['task_id'] == executions[0]
    assert 'private-review-token' not in json.dumps(frames)
    assert len(store.tasks_for_conversation(cid, 'alice')) == 1
    assert store.tasks_for_conversation(cid, 'bob') == []
    saved = chat_service.get_conversation('alice', cid)
    assert 'calendar_create_draft' not in saved
    assert saved['messages'][-1]['id'] == f'task-{executions[0]}-result'
    # A repeated owner message observes the same accepted task and result.
    assert asyncio.run(turn())[-1] == frames[-1]
    assert len(executions) == len(store.tasks_for_conversation(cid, 'alice')) == 1


@pytest.mark.parametrize('mismatch', ['title', 'date', 'read', 'owner'])
def test_chat_create_handoff_keeps_payload_route_and_owner_guards(calendar_handoff, mismatch):
    store, conv, bot = calendar_handoff
    intent = cal_writer.parse_create_request(ADVENT_REQUEST)
    kwargs = {'calendar_intent': intent}
    if mismatch == 'title':
        intent['title'] = 'Changed title'
    elif mismatch == 'date':
        intent.update(start='2026-11-07', end='2026-11-08')
    elif mismatch == 'read':
        kwargs['mode'] = 'calendar'
    else:
        bot['owner'] = 'bob'

    async def turn():
        return [frame async for frame in chat_service._stream_writable_bot_task(
            'alice', conv, bot, ADVENT_REQUEST, conv['conversation_id'], None, **kwargs)]

    with pytest.raises(chat_service.ChatUnavailable):
        asyncio.run(turn())
    assert store.list_tasks() == []
