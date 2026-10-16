"""Booked-class reads and Calendar imports; never a live booking/provider call."""
import asyncio
import copy
import json
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import bots
import chat_service as chat
import connectors
import member_bookings as flow
import member_bookings_schema as schema
import serve
import task_store
import workout_booking_chat
from task_store import CloudTaskStore, TaskWorker

DAY = (datetime.now(schema.TZ) + timedelta(days=2)).date()
WINDOW = {'url': schema.URL, 'start': DAY.isoformat(), 'end': (DAY + timedelta(days=7)).isoformat()}
START = int(datetime.combine(DAY, datetime.min.time(), schema.TZ).replace(hour=8, minute=30).timestamp())
ROWS = [{'reference': 'a' * 24, 'event_id': 'c' * 24, 'title': 'Group Fitness Class', 'start': START, 'end': START + 2700},
        {'reference': 'b' * 24, 'event_id': 'd' * 24, 'title': 'Group Fitness Class', 'start': START + 86400, 'end': START + 89100}]
BINDING = {'bot': 'browser-bot', 'host': 'test-host', 'allowlist': ['*']}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'member-bookings-fixture-only')
    monkeypatch.setattr(bots, 'BOTS_FILE', str(tmp_path / 'bots.json'))
    monkeypatch.setattr(chat, '_data_dir', lambda: tmp_path)
    (tmp_path / 'rift').mkdir()
    bots.add_bot('assistant', 'Assistant', 'openai:fixture', str(tmp_path / 'rift'), owner='alice',
                 status='running', policy=serve.coordinator_preset_policy())
    store = CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(task_store, 'CloudTaskStore', lambda *a, **kw: store)
    monkeypatch.setattr(chat, '_task_store', lambda: store)
    state = {'bookings': copy.deepcopy(ROWS), 'identity': 'f' * 64, 'existing': [], 'created': [],
             'reads': [], 'approved': True, 'reviews': [], 'write': True, 'calendar': 'primary',
             'account': 'alice@example.test', 'connected_at': 'fixture-revision', 'bindings': []}

    def read(owner, window, task_id, task_store, *, expected_binding=None):
        assert owner == 'alice' and task_store is store
        if expected_binding is not None: assert expected_binding == BINDING
        state['reads'].append(task_id)
        return {**schema.result(copy.deepcopy(state['bookings']), state['identity'], window), 'browser_binding': BINDING}
    monkeypatch.setattr(flow, 'read', read)
    def approve(*args, **kw):
        assert not state['created']
        state['reviews'].append(json.loads(kw['detail']))
        if state.get('during_review'): state['during_review']()
        return state['approved']
    state['native_approval'] = serve._request_in_process_approval
    monkeypatch.setattr(serve, '_request_in_process_approval', approve)
    def events(**kwargs):
        assert kwargs['require_complete'] is True and kwargs['calendar_id'] == state['calendar']
        return copy.deepcopy(state['existing'])
    def create(event, *, expected_binding):
        assert expected_binding['calendar'] == state['calendar']
        assert expected_binding['connected_at'] == state['connected_at']
        assert state['approved'] and state['write']
        state['created'].append(copy.deepcopy(event))
        state['existing'].append({**event, 'summary': event['summary']})
        if state.get('uncertain'): raise TimeoutError('private provider body must not leak')
        return {'id': event['id']}
    connection = SimpleNamespace(calendar_read_available=lambda owner: True,
        calendar_write_available=lambda owner: state['write'],
        status=lambda owner: {'connected_at': state['connected_at']},
        account_view=lambda owner: {'email': state['account']},
        preferred_calendar=lambda owner: state['calendar'],
        calendar=lambda owner: SimpleNamespace(events=events),
        calendar_writer=lambda owner: SimpleNamespace(create_event=create))
    monkeypatch.setattr(connectors, 'default_store', lambda: connection)
    cid = chat.create_conversation('alice', bot_id='assistant')['conversation_id']
    def run(operation='read', source=None, owner='alice', window=None):
        payload = {'operation': operation, 'window': window or WINDOW, 'source_task': source}
        ident = flow.submit(owner, bots.get_bot('assistant'), payload, store, cid)
        worker = TaskWorker(store)
        assert worker.claim_and_execute_once() is True
        return store.get(ident)
    state.update(store=store, run=run, cid=cid)
    yield state
    chat.close_all_engine_sessions(); store.close()


def test_read_does_not_create_or_call_workout_submission(rig, monkeypatch):
    monkeypatch.setattr(workout_booking_chat, 'prepare', lambda *a: pytest.fail('Read cannot prepare a booking'))
    task = rig['run']()
    assert task['status'] == 'done', task
    assert task['result']['bookings'] == ROWS
    assert task['result']['verified_bookings'] is True
    assert rig['created'] == [] and rig['reviews'] == []
    assert '2 booked' in task['result']['final_response']


def test_verified_read_followup_imports_exact_classes_and_repeated_import_skips(rig):
    source = rig['run']()
    result = rig['run']('calendar', source['task_id'])
    assert result['status'] == 'done', result
    assert len(rig['created']) == 2
    assert len(rig['reviews']) == 1
    assert rig['reviews'][0]['events'] == [{k: v for k, v in event.items() if k != 'id'} for event in rig['created']]
    assert len(rig['reads']) == 3  # read, import preflight, post-approval recheck
    again = rig['run']('calendar', source['task_id'])
    assert again['status'] == 'done'
    assert len(rig['created']) == 2 and len(rig['reviews']) == 1
    assert 'already on your calendar' in again['result']['final_response']


@pytest.mark.parametrize('change', ['decline', 'cancelled_class', 'session', 'destination', 'google_account', 'revoked_scope', 'task_cancel'])
def test_changes_during_review_cannot_create_events(rig, change):
    def mutate():
        if change == 'decline': rig['approved'] = False
        elif change == 'cancelled_class': rig['bookings'].pop()
        elif change == 'session': rig['identity'] = 'e' * 64
        elif change == 'destination': rig['calendar'] = 'other'
        elif change == 'google_account': rig['account'] = 'other@example.test'
        elif change == 'revoked_scope': rig['write'] = False
        else:
            active = rig['store'].latest_active_task_for_conversation(rig['cid'], 'alice')
            rig['store'].request_cancel(active['task_id'])
    rig['during_review'] = mutate
    rig['run']('calendar')
    assert rig['created'] == []


def test_cancelled_selection_stops_before_review(rig):
    source = rig['run'](); rig['bookings'].pop()
    result = rig['run']('calendar', source['task_id'])
    assert result['status'] == 'failed'
    assert rig['reviews'] == [] and rig['created'] == []


def test_followup_does_not_import_additional_bookings_discovered_after_the_list(rig):
    source = rig['run']()
    rig['bookings'].append({**ROWS[0], 'reference': '9' * 24, 'event_id': '8' * 24,
                           'start': START + 172800, 'end': START + 175500})
    result = rig['run']('calendar', source['task_id'])
    assert result['status'] == 'done'
    assert len(rig['created']) == 2 and result['result']['bookings'] == ROWS


def test_unknown_provider_outcome_is_not_replayed_and_next_request_reconciles(rig):
    rig['uncertain'] = True
    first = rig['run']('calendar')
    assert first['status'] == 'failed'
    assert len(rig['created']) == 1
    assert 'private provider body' not in str(first['result'])
    rig['uncertain'] = False
    # Only the second class is now missing; a new exact review is required.
    rig['created'].clear()
    again = rig['run']('calendar')
    assert again['status'] == 'done' and len(rig['created']) == 1
    assert len(rig['existing']) == 2


def test_all_day_labels_are_not_timed_booked_class_duplicates(rig):
    rig['existing'] = [{'id': 'old-tag', 'summary': 'Level 6 Workout: STRENGTH',
                        'start': {'date': DAY.isoformat()}, 'end': {'date': (DAY + timedelta(days=1)).isoformat()}}]
    assert rig['run']('calendar')['status'] == 'done'
    assert len(rig['created']) == 2


def test_foreign_owner_cannot_submit_or_select_another_conversation(rig):
    with pytest.raises(ValueError): rig['run'](owner='bob')
    source = rig['run']()
    with rig['store']._conn:
        rig['store']._conn.execute('UPDATE tasks SET conversation_id=? WHERE task_id=?', ('foreign-conversation', source['task_id']))
    task = rig['run']('calendar', source['task_id'])
    assert task['status'] == 'failed' and not rig['reviews'] and not rig['created']


@pytest.mark.parametrize('text', [
    'Book my Level 6 workout on the website for October 19, 2026 at 8:30 AM',
    'Yes', 'Book it', 'Cancel my booking', 'Delete that calendar event',
    'Show my calendar', 'Search my email for a booking', 'Fix the Glofox booking code',
    'Book a table at a restaurant', 'Show my hotel reservations',
    "Don't check my Level 6 bookings", "Don't add my booked Level 6 classes to my calendar",
])
def test_booking_approvals_and_unrelated_tools_do_not_enter_read_flow(rig, text):
    conv = chat.get_conversation('alice', rig['cid'])
    assert flow.request(text, conv, rig['store'], 'alice') is None


@pytest.mark.parametrize('text', [
    'What is booked?', 'Show my booked classes', 'What do I have booked?',
    'What have I booked at Level 6?', "I've booked 2 classes on l6 website can you check those",
])
def test_natural_booking_read_requests(rig, text):
    conv = chat.get_conversation('alice', rig['cid'])
    conv['messages'].append({'role': 'user', 'content': 'I booked my Level 6 workouts'})
    result = flow.request(text, conv, rig['store'], 'alice')
    assert result['operation'] == 'read' and result['source_task'] is None


def test_new_conversation_does_not_assume_a_studio_for_an_ambiguous_booking_question(rig):
    with pytest.raises(ValueError, match='Which service'):
        flow.request('What is booked?', chat.get_conversation('alice', rig['cid']), rig['store'], 'alice')


def test_pronoun_binds_native_result_and_not_assistant_invented_dates(rig):
    source = rig['run']()
    conv = chat.get_conversation('alice', rig['cid'])
    conv['messages'].append({'role': 'assistant', 'id': 'task-' + source['task_id'] + '-result', 'content': source['result']['final_response']})
    payload = flow.request('Add those to my calendar', conv, rig['store'], 'alice')
    assert payload['source_task'] == source['task_id'] and payload['window'] == WINDOW
    assert flow.request('Add Dentist tomorrow to my calendar', conv, rig['store'], 'alice') is None
    conv['messages'].append({'role': 'assistant', 'id': 'unrelated', 'content': 'Some email result'})
    assert flow.request('Add those to my calendar', conv, rig['store'], 'alice') is None


def test_screenshot_verify_then_add_uses_website_context(rig):
    conv = chat.get_conversation('alice', rig['cid'])
    conv['messages'].append({'role': 'user', 'content': "I've booked 2 classes on l6 website can you check those"})
    assert flow.request('Verify booking then add to my calendar', conv, rig['store'], 'alice')['operation'] == 'calendar'


def test_explicit_bot_denies_are_not_overridden(rig):
    bot = bots.get_bot('assistant'); bot['policy']['cal:create'] = 'deny'
    with pytest.raises(ValueError): flow.permitted(bot, 'cal:create')


def test_negated_calendar_clause_keeps_request_read_only(rig):
    conv = chat.get_conversation('alice', rig['cid'])
    assert flow.request("Show my Level 6 bookings but don't add them to my calendar", conv, rig['store'], 'alice')['operation'] == 'read'


def test_bound_calendar_connector_refuses_a_changed_destination_before_transport():
    calls = []
    store = SimpleNamespace(route_capability=lambda *a: {'bot_role': 'calendar_writer'},
        status=lambda *a: {'scopes': [connectors.GOOGLE_CALENDAR_WRITE_SCOPE], 'connected_at': 'revision'},
        access_token=lambda *a: 'fixture-only', preferred_calendar=lambda *a: 'other-calendar')
    writer = connectors.CalendarWrite(store, 'alice', transport=lambda *args: calls.append(args))
    with pytest.raises(connectors.ConnectorUnavailable):
        writer.create_event({'summary': 'Class'}, expected_binding={'calendar': 'primary', 'connected_at': 'revision'})
    assert calls == []


def test_actual_chat_routes_before_email_and_booking_and_keeps_submission_untouched(rig, monkeypatch):
    calls = []
    async def stream(*args, **kwargs):
        calls.append(kwargs)
        yield {'type': 'status', 'status': 'complete', 'content': 'fixture'}
    monkeypatch.setattr(chat, '_stream_writable_bot_task', stream)
    async def run():
        return [f async for f in chat.stream_chat('alice', rig['cid'], 'What is booked at Level 6?')]
    frames = asyncio.run(run())
    assert frames[-1]['content'] == 'fixture'
    assert calls[0]['mode'] == 'member_bookings'
    assert calls[0]['member_bookings_request']['operation'] == 'read'
    assert workout_booking_chat.action('Book my Level 6 workout tomorrow at 8:30 AM', {'messages': []}) == 'start'


def test_actual_chat_worker_and_reopened_followup_use_same_verified_selection(rig):
    stop = threading.Event()
    worker = TaskWorker(rig['store'])
    def drain():
        while not stop.is_set():
            worker.claim_and_execute_once(timeout=.1)
            stop.wait(.01)
    thread = threading.Thread(target=drain, daemon=True); thread.start()
    async def collect(text):
        return [f async for f in chat.stream_chat('alice', rig['cid'], text)]
    try:
        first = asyncio.run(collect('Show my Level 6 bookings'))
        assert first[-1]['status'] == 'complete', first
        reopened = chat.get_conversation('alice', rig['cid'])
        assert flow.latest_selection(rig['store'], 'alice', reopened) is not None
        second = asyncio.run(collect('Add those to my calendar'))
        assert second[-1]['status'] == 'complete', second
        assert len(rig['created']) == 2 and len(rig['reviews']) == 1
    finally:
        stop.set(); thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize('decision', ['y', 'n'])
def test_import_parks_at_real_durable_approval_before_any_calendar_write(rig, monkeypatch, decision):
    monkeypatch.setattr(serve, '_request_in_process_approval', rig['native_approval'])
    monkeypatch.setattr(serve, 'APPROVAL_TIMEOUT', 5)
    ident = flow.submit('alice', bots.get_bot('assistant'),
        {'operation': 'calendar', 'window': WINDOW, 'source_task': None}, rig['store'], rig['cid'])
    worker = TaskWorker(rig['store'])
    thread = threading.Thread(target=worker.claim_and_execute_once, daemon=True); thread.start()
    try:
        deadline = time.monotonic() + 3
        approval = None
        while time.monotonic() < deadline:
            approval = rig['store'].get_pending_approval(ident)
            if approval: break
            time.sleep(.02)
        assert approval, rig['store'].get(ident)
        assert rig['store'].get(ident)['status'] == 'awaiting_approval'
        assert not rig['created']
        assert len(json.loads(approval['detail'])['events']) == 2
        assert rig['store'].record_operator_reply(ident, decision)
        assert rig['store'].deliver_operator_replies() == 1
    finally:
        thread.join(timeout=6)
        assert not thread.is_alive()
    assert len(rig['created']) == (2 if decision == 'y' else 0)
