"""Booked-class reads and Calendar imports; never a live booking/provider call."""
import asyncio
import copy
import json
import queue
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


@pytest.mark.parametrize('code', [
    'signed_out', 'session_unverified', 'unsupported_portal', 'refresh_blocked',
    'missing_read', 'private-token-or-member-value', None,
])
def test_host_failure_reaches_chat_as_a_fixed_explanation(monkeypatch, code):
    import booking_chat
    import browser_host_bridge
    monkeypatch.setattr(booking_chat, 'browser', lambda owner:
        ({'id': 'browser-bot', 'browser_allowlist': ['*']}, SimpleNamespace(host_id='test-host')))
    failure = {'code': code, 'message': 'private-token-or-member-value'}
    result = {'mode': schema.ACTION, 'status': 'error',
              'member_bookings_failure': failure,
              'final_response': 'private-token-or-member-value'}
    monkeypatch.setattr(browser_host_bridge, 'request_browser_dispatch', lambda **kwargs: (result, None))
    with pytest.raises(flow.FlowError) as error:
        flow.read('alice', WINDOW, 'test-task', None)
    assert str(error.value) == schema.failure_message(failure)
    assert 'private-token-or-member-value' not in str(error.value)


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
    connection = SimpleNamespace(scope_granted=lambda owner, scope: scope == connectors.GOOGLE_CALENDAR_READ_SCOPE,
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


@pytest.fixture
def connected_rig(rig, tmp_path, monkeypatch):
    """Use the production Store/CalendarRead/CalendarWrite; stub only HTTP."""
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'fixture-client')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'fixture-client-secret')
    monkeypatch.setenv('GOOGLE_REDIRECT_URI', 'https://example.test/oauth/callback')
    connection = connectors.ConnectorStore(path=tmp_path / 'real-connectors.json')

    def connect(scopes=None, owner='alice'):
        scopes = scopes if scopes is not None else [
            connectors.GOOGLE_CALENDAR_READ_SCOPE, connectors.GOOGLE_CALENDAR_WRITE_SCOPE]
        begun = connection.begin_oauth(owner, scopes=scopes)
        return connection.complete_oauth(owner, begun['state'], 'fixture-code',
            exchange=lambda *args: {'access_token': 'fixture-access-only',
                'expires_in': 3600, 'scope': ' '.join(scopes)})

    connect()
    rig['http'] = []

    def google_event(event):
        value = copy.deepcopy(event)
        value.setdefault('status', 'confirmed')
        for key in ('start', 'end'):
            when = value.get(key, {})
            if 'dateTime' in when:
                parsed = datetime.fromisoformat(when['dateTime'].replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=schema.TZ)
                value[key] = {'dateTime': parsed.isoformat(), 'timeZone': 'America/New_York'}
        return value

    def transport(method, url, token, params=None, body=None, headers=None):
        rig['http'].append((method, url))
        if url == 'https://oauth2.googleapis.com/tokeninfo':
            assert method == 'GET' and params == {'access_token': 'fixture-access-only'}
            return {'email': rig['account']}
        assert url == 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
        assert token == 'fixture-access-only'
        if method == 'GET':
            assert params['singleEvents'] == 'true'
            return {'items': [google_event(event) for event in rig['existing']]}
        assert method == 'POST'
        assert rig['reviews'] and rig['approved'] or rig.get('native_approved')
        rig['created'].append(copy.deepcopy(body))
        rig['existing'].append(google_event(body))
        if rig.get('uncertain'):
            raise TimeoutError('private provider response')
        return google_event(body)

    monkeypatch.setattr(connectors, 'default_transport', transport)
    monkeypatch.setattr(connectors, 'default_store', lambda: connection)
    rig.update(connection=connection, connect=connect)
    return rig


def test_real_calendar_connector_import_and_retry_with_google_timezone_offsets(connected_rig):
    rig = connected_rig
    source = rig['run']()
    first = rig['run']('calendar', source['task_id'])
    assert first['status'] == 'done', first['result']
    assert len(rig['created']) == 2 and len(rig['reviews']) == 1
    assert rig['reviews'][0]['account'] == 'alice@example.test'
    second = rig['run']('calendar', source['task_id'])
    assert second['status'] == 'done', second['result']
    assert len(rig['created']) == 2 and len(rig['reviews']) == 1
    assert 'already on your calendar' in second['result']['final_response']


def test_real_calendar_connector_skips_matching_events_created_elsewhere(connected_rig):
    rig = connected_rig
    rig['existing'] = [{**event, 'id': 'existing-' + str(i)}
        for i, event in enumerate(flow.calendar_events(ROWS, 'alice', {}))]
    result = rig['run']('calendar')
    assert result['status'] == 'done', result['result']
    assert not rig['reviews'] and not rig['created']


def test_real_calendar_connector_decline_does_not_create(connected_rig):
    rig = connected_rig
    rig['approved'] = False
    assert rig['run']('calendar')['status'] == 'done'
    assert len(rig['reviews']) == 1
    assert not any(method == 'POST' for method, _ in rig['http'])


@pytest.mark.parametrize('grant', ['read_only', 'write_only', 'disconnected', 'other_owner'])
def test_real_calendar_connector_requires_this_owners_read_and_write_grants(connected_rig, grant):
    rig = connected_rig
    if grant == 'read_only': rig['connect']([connectors.GOOGLE_CALENDAR_READ_SCOPE])
    elif grant == 'write_only': rig['connect']([connectors.GOOGLE_CALENDAR_WRITE_SCOPE])
    else:
        rig['connection'].disconnect('alice')
        if grant == 'other_owner': rig['connect'](owner='bob')
    result = rig['run']('calendar')
    assert result['status'] == 'failed'
    assert 'Connect Google Calendar read and write access' in result['result']['final_response']
    assert result['result']['final_response'].endswith('No calendar events were added.')
    assert not rig['reviews'] and not rig['created'] and not rig['http']


def test_real_calendar_connector_rechecks_scope_after_review(connected_rig):
    rig = connected_rig
    rig['during_review'] = lambda: rig['connect']([connectors.GOOGLE_CALENDAR_WRITE_SCOPE])
    result = rig['run']('calendar')
    assert result['status'] == 'failed' and len(rig['reviews']) == 1
    assert not any(method == 'POST' for method, _ in rig['http'])


def test_real_calendar_connector_uncertain_write_is_not_replayed(connected_rig):
    rig = connected_rig
    rig['uncertain'] = True
    result = rig['run']('calendar')
    assert result['status'] == 'failed' and len(rig['created']) == 1
    assert 'Check Calendar before trying again' in result['result']['final_response']
    assert 'private provider response' not in str(result['result'])
    rig['uncertain'] = False
    rig['created'].clear()
    retry = rig['run']('calendar')
    assert retry['status'] == 'done', retry['result']
    assert len(rig['created']) == 1 and len(rig['existing']) == 2


@pytest.mark.parametrize('day,offset', [('2026-10-19', '-04:00'), ('2026-12-14', '-05:00')])
def test_duplicate_times_compare_explicit_zone_to_google_offset(day, offset):
    event = {'id': 'imported', 'summary': 'Level 6 · Group Fitness Class',
        'start': {'dateTime': day + 'T08:30:00', 'timeZone': 'America/New_York'},
        'end': {'dateTime': day + 'T09:15:00', 'timeZone': 'America/New_York'}}
    existing = {**event, 'start': {'dateTime': day + 'T08:30:00' + offset},
                'end': {'dateTime': day + 'T09:15:00' + offset}}
    assert flow.missing([event], [existing]) == []
    changed = {**existing, 'start': {'dateTime': day + 'T08:35:00' + offset}}
    with pytest.raises(flow.FlowError, match='edited'):
        flow.missing([event], [changed])


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


def test_calendar_grant_without_optional_google_email_still_uses_bound_connection(rig):
    rig['account'] = None
    task = rig['run']('calendar')
    assert task['status'] == 'done' and len(rig['created']) == 2
    assert rig['reviews'][0]['account'] == 'Your connected Google account'


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


@pytest.mark.parametrize('phrase,first,last', [
    ('Wednesday, October 21', '2026-10-21', '2026-10-22'),
    ('next Wednesday, October 21', '2026-10-21', '2026-10-22'),
    ('October 21,2026', '2026-10-21', '2026-10-22'),
    ('Oct. 21st', '2026-10-21', '2026-10-22'),
    ('2026-10-21', '2026-10-21', '2026-10-22'),
    ('Wednesday', '2026-10-21', '2026-10-22'),
    ('next Tuesday', '2026-10-27', '2026-10-28'),
    ('Tuesday', '2026-10-20', '2026-10-21'),
    ('today', '2026-10-20', '2026-10-21'),
    ('tomorrow', '2026-10-21', '2026-10-22'),
    ('this week', '2026-10-20', '2026-10-26'),
    ('next week', '2026-10-26', '2026-11-02'),
    ('upcoming', '2026-10-20', '2026-11-19'),
])
def test_owner_supplied_read_dates_are_exact_local_windows(rig, phrase, first, last):
    now = datetime(2026, 10, 20, 6, 58, tzinfo=schema.TZ)
    result = flow.request('Show my Level 6 bookings for ' + phrase,
                          chat.get_conversation('alice', rig['cid']), rig['store'], 'alice', now=now)
    assert result == {'operation': 'read', 'source_task': None,
                      'window': {'url': schema.URL, 'start': first, 'end': last}}
    assert not rig['reads'] and not rig['created'] and not rig['reviews']


@pytest.mark.parametrize('phrase', [
    'Tuesday, October 21', 'February 30, 2026', '2026-02-30', '2026-2-3',
    'October 21, 26', 'October 21, 20264', 'October', '10/21',
    'October 21 and October 22', 'October 21-23', 'October 21/22',
    'October 21 through 23', 'Wednesday or Thursday', 'tomorrow October 22',
    'this week next week', 'next Wednesday, October 28',
    'last Wednesday', 'before October 21', 'after October 21',
    'not Wednesday', 'except Wednesday', 'day after tomorrow',
])
def test_ambiguous_or_invalid_date_never_broadens_into_upcoming_bookings(rig, phrase):
    with pytest.raises(flow.FlowError):
        flow.request('Show my Level 6 bookings for ' + phrase,
                     chat.get_conversation('alice', rig['cid']), rig['store'], 'alice',
                     now=datetime(2026, 10, 20, tzinfo=schema.TZ))
    assert not rig['reads'] and not rig['created'] and not rig['reviews']


@pytest.mark.parametrize('when,phrase,expected', [
    ('2026-10-21T01:00:00+00:00', 'Tuesday', '2026-10-20'),
    ('2026-12-31T23:00:00-05:00', 'Friday', '2027-01-01'),
    ('2026-12-31T23:00:00-05:00', 'January 1', '2026-01-01'),
])
def test_date_resolution_uses_studio_local_day_and_explicit_year_policy(rig, when, phrase, expected):
    result = flow.request('Show my Level 6 bookings for ' + phrase,
                          chat.get_conversation('alice', rig['cid']), rig['store'], 'alice',
                          now=datetime.fromisoformat(when))
    assert result['window']['start'] == expected


@pytest.mark.parametrize('day,hours', [('2026-03-08', 23), ('2026-11-01', 25)])
def test_exact_day_windows_preserve_dst_midnight_boundaries(day, hours):
    window = flow.read_window(day, datetime(2026, 10, 20, tzinfo=schema.TZ))
    low, high = schema.bounds(window)
    assert high - low == hours * 3600


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


@pytest.mark.parametrize('text,expected_day', [
    ('What is booked at Level 6?', None),
    ('Show my Level 6 bookings for Wednesday, October 21, 2026', '2026-10-21'),
])
def test_actual_chat_routes_before_email_and_booking_and_keeps_submission_untouched(rig, monkeypatch, text, expected_day):
    calls = []
    async def stream(*args, **kwargs):
        calls.append(kwargs)
        yield {'type': 'status', 'status': 'complete', 'content': 'fixture'}
    monkeypatch.setattr(chat, '_stream_writable_bot_task', stream)
    async def run():
        return [f async for f in chat.stream_chat('alice', rig['cid'], text)]
    frames = asyncio.run(run())
    assert frames[-1]['content'] == 'fixture'
    assert calls[0]['mode'] == 'member_bookings'
    assert calls[0]['member_bookings_request']['operation'] == 'read'
    if expected_day:
        assert calls[0]['member_bookings_request']['window'] == {
            'url': schema.URL, 'start': expected_day, 'end': '2026-10-22'}
    assert workout_booking_chat.action('Book my Level 6 workout tomorrow at 8:30 AM', {'messages': []}) == 'start'


@pytest.mark.parametrize('empty_poll', [False, True])
def test_actual_chat_worker_and_reopened_followup_use_same_verified_selection(rig, monkeypatch, empty_poll):
    to_thread = asyncio.to_thread
    forced_empty = []
    async def with_empty_poll(fn, *args, **kwargs):
        if (empty_poll and not forced_empty and getattr(fn, '__name__', '') == 'get'
                and isinstance(getattr(fn, '__self__', None), queue.Queue)
                and args == (True, chat.BOT_TASK_POLL_SECONDS)):
            forced_empty.append(True)
            raise queue.Empty
        return await to_thread(fn, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', with_empty_poll)
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
        assert bool(forced_empty) == empty_poll
    finally:
        stop.set(); thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize('decision', ['y', 'n'])
@pytest.mark.parametrize('connector_fixture', ['rig', 'connected_rig'])
def test_import_parks_at_real_durable_approval_before_any_calendar_write(request, monkeypatch, decision, connector_fixture):
    rig = request.getfixturevalue(connector_fixture)
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
        rig['native_approved'] = decision == 'y'
        assert rig['store'].deliver_operator_replies() == 1
    finally:
        thread.join(timeout=6)
        assert not thread.is_alive()
    assert len(rig['created']) == (2 if decision == 'y' else 0)
