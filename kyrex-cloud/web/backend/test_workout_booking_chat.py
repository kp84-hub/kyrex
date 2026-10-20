"""Actual native Chat stream: one owner review, exact class, no live bookings."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import booking_schema as common
import booking_store
import bots
import browser_host_bridge
import browser_hosts
import chat_service as chat
import workout_class_reader
import serve
import workout_booking_chat as flow
import workout_booking_schema as schema
from task_store import CloudTaskStore

DAY = (datetime.now(ZoneInfo('America/New_York')) + timedelta(days=1)).date().isoformat()
DETAILS = {'date': DAY, 'time': '08:30', 'event_id': 'c' * 24,
           'class_name': 'Group Fitness Class', 'trainer_name': 'Donna Albertone',
           'start': int(datetime.fromisoformat(DAY + 'T08:30').replace(tzinfo=ZoneInfo('America/New_York')).timestamp())}
PORTAL = 'https://app.glofox.com/portal/#/branch/' + schema.BRANCH + '/classes-list-view'
API = 'https://api.glofox.com/2.3/branches/' + schema.BRANCH + '/bookings'


def control():
    c = {'ref': 'c0', 'selector': 'css=#book', 'frame': PORTAL, 'label': 'Book',
         'tag': 'button', 'type': 'button', 'value': '', 'href': '', 'required': False,
         'disabled': False, 'form_action': '', 'form_method': '', 'options': []}
    c['fingerprint'] = common.fingerprint(c)
    return c


def day_control(day):
    wanted = datetime.fromisoformat(day)
    weekday = ('MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN')[wanted.weekday()]
    c = control()
    c.update(ref='c-day', selector='css=#requested-day', tag='li', label=f'{weekday}\n{wanted.day}')
    c['fingerprint'] = common.fingerprint(c)
    return c


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'workout-test-only')
    monkeypatch.setenv('KYREX_PROVIDER', 'openai')
    monkeypatch.setenv('KYREX_API_KEY', 'test-key')
    monkeypatch.setenv('KYREX_MODEL', 'test-model')
    monkeypatch.setattr(chat, '_data_dir', lambda: tmp_path)
    monkeypatch.setattr(bots, 'BOTS_FILE', str(tmp_path / 'bots.json'))
    rift = tmp_path / 'rift'; rift.mkdir()
    bots.add_bot('browser-bot', 'Browser Bot', 'openai:test-model', str(rift), owner='alice',
                 status='running', policy=serve.browser_preset_policy(), browser_allowlist=['*'])
    host = SimpleNamespace(host_id='test-host', is_available=lambda: True)
    monkeypatch.setattr(browser_hosts, 'host_for', lambda owner, bot: host if owner == 'alice' and bot == 'browser-bot' else None)
    state = {'calls': [], 'posts': 0, 'outcome': 'confirmed', 'changed_class': False, 'model': 0}
    task = CloudTaskStore(db_path=tmp_path / 'tasks.db'); monkeypatch.setattr(chat, '_task_store', lambda: task)
    def class_at(day, clock):
        d = dict(DETAILS); d.update(date=day, time=clock)
        d['start'] = int(datetime.fromisoformat(day + 'T' + clock).replace(tzinfo=ZoneInfo('America/New_York')).timestamp())
        if state['changed_class']: d['trainer_name'] = 'Different Trainer'
        return d
    monkeypatch.setattr(workout_class_reader, 'class_at', class_at)
    async def provider(**kw):
        state['model'] += 1
        data = json.loads(kw['messages'][1]['content'])
        if kw['messages'][0]['content'].startswith('Extract'):
            messages = '\n'.join(data['owner_messages'])
            values = dict(data['current_details']); evidence = {}
            if 'tomorrow' in messages: values['date'] = DAY; evidence['date'] = 'tomorrow'
            if '8:30 AM' in messages: values['time'] = '08:30'; evidence['time'] = '8:30 AM'
            if '9:30 AM' in messages: values['time'] = '09:30'; evidence['time'] = '9:30 AM'
            return {'content': json.dumps({'details': values, 'evidence': evidence})}
        if state.get('past_default') and 'Event passed' in data.get('snapshot', {}).get('text', ''):
            state['past_model_views'] = state.get('past_model_views', 0) + 1
            return {'content': json.dumps({'action': 'blocked', 'evidence': data['snapshot']['text']})}
        return {'content': json.dumps({'action': 'click', 'ref': 'c0'})}
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=provider))
    def browser(**kw):
        spec = json.loads(kw['task_text'])[schema.ACTION]
        store = booking_store.BookingStore(); store.authorize(kw['owner'], kw['bot_id'], kw['host_id'], kw['task_text'])
        assert store.active(kw['owner'], kw['bot_id'], kw['host_id'], kw['task_text'])
        state['calls'].append(spec)
        if state.get('login_required'):
            return {'mode': schema.ACTION, 'final_response': 'Sign in to your Glofox member account in the Browser Host manual viewer first.'}, None
        c = control()
        snap = {'url': PORTAL, 'title': 'Level 6', 'text': 'My bookings Group Fitness Class Included in your membership',
                'controls': [c], 'blocked': False, 'contexts': {'c0': 'Group Fitness Class Included in your membership'}, 'signed_in': True}
        selected = any(s['control']['selector'] == 'css=#requested-day' for s in spec['steps'])
        if state.get('past_default') and (not selected or state.get('still_past')):
            day = day_control(spec['details']['date'])
            snap.update(text='Group Fitness Class 8:30 am - 45 Minutes Event passed 5/18 Joined Austin Ordonez',
                        controls=[] if state.get('hide_day') or selected else [day])
            return {'mode': schema.ACTION, 'snapshot': snap}, None
        if spec['stage'] == 'submit':
            assert spec['expected'] == common.review_hash(snap)
            state['posts'] += 1
            return {'mode': schema.ACTION, 'outcome': state['outcome'], 'reference': 'd' * 24, 'url': PORTAL}, None
        if any(s['control']['selector'] == 'css=#book' for s in spec['steps']):
            template = schema.request_template(API, 'POST', {'model': 'event', 'model_id': 'c' * 24,
                'user_id': 'e' * 24, 'join_waiting_list': False}, 'f' * 64)
            if state.get('legacy_member'):
                template = schema.request_template('https://api.glofox.com/2.0/bookings', 'POST',
                    {'model': 'event', 'model_id': 'c' * 24, 'guest_bookings': 0, 'pay_gym': False}, 'f' * 64)
                if state.get('alter_adapter'): template['body']['guest_bookings'] = 1
            return {'mode': schema.ACTION, 'snapshot': snap, 'review': {'submit': c, 'request': template,
                'steps': [s for s in spec['steps'] if s['control']['selector'] != 'css=#book'],
                'terms': 'Group Fitness Class Included in your membership'}}, None
        return {'mode': schema.ACTION, 'snapshot': snap}, None
    monkeypatch.setattr(browser_host_bridge, 'request_browser_dispatch', browser)
    cid = chat.create_conversation('alice')['conversation_id']
    yield cid, state, tmp_path
    chat.close_all_engine_sessions()


def turn(rig, text, request_id=None, owner='alice', cancel=None):
    async def run():
        return [f async for f in chat.stream_chat(owner, rig[0], text, request_id=request_id, cancel_event=cancel)]
    return asyncio.run(run())


def review(rig):
    result = turn(rig, 'Book my workout tomorrow at 8:30 AM', 'prepare')[-1]
    assert 'Reply **yes**' in result['content'] and 'Donna Albertone' in result['content']
    assert 'Level 6' in result['content'] and not rig[1]['posts']
    return result


@pytest.fixture
def inline_class_lookup(rig, monkeypatch):
    """Keep a pure schedule fixture out of the executor in later-stage deadline tests.

    These tests deliberately pause dispatch/the model. Thread-pool scheduling
    must not consume their short deadline before that tested stage is reached.
    The separate class-lookup deadline tests retain the real asynchronous path.
    """
    to_thread = asyncio.to_thread
    calls = []
    async def lookup(fn, *args, **kwargs):
        if fn is workout_class_reader.class_at:
            calls.append(True)
            return fn(*args, **kwargs)
        return await to_thread(fn, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', lookup)
    return calls


def test_requested_date_is_selected_before_past_default_card_can_block_the_model(rig):
    rig[1]['past_default'] = True
    result = turn(rig, 'Book my Level 6 workout on the website for October 19, 2026 at 8:30 AM', 'dated-prepare')[-1]
    assert 'Reply **yes**' in result['content'] and 'October 19, 2026' in result['content']
    assert not rig[1]['posts'] and not rig[1].get('past_model_views')
    calls = rig[1]['calls']
    assert len(calls) == 3
    assert calls[0]['steps'] == []
    assert [s['control']['label'] for s in calls[1]['steps']] == ['MON\n19']
    assert [s['control']['label'] for s in calls[2]['steps']] == ['MON\n19', 'Book']
    assert all(c['details']['date'] == '2026-10-19' for c in calls)
    answer = turn(rig, 'yes', 'dated-submit')[-1]
    assert 'Glofox confirmed' in answer['content'] and rig[1]['posts'] == 1
    turn(rig, 'yes', 'dated-duplicate')
    assert rig[1]['posts'] == 1


@pytest.mark.parametrize('hide_day', [False, True])
def test_past_card_does_not_establish_that_requested_future_class_is_unavailable(rig, hide_day):
    rig[1].update(past_default=True, still_past=True, hide_day=hide_day)
    result = turn(rig, 'Book my workout tomorrow at 8:30 AM', 'unverified-date')[-1]
    assert 'could not verify your requested future class' in result['content']
    assert 'Nothing was booked' in result['content'] and not rig[1]['posts']
    assert 'The page says' not in result['content'] and '5/18 Joined' not in result['content']
    assert len(rig[1]['calls']) == (1 if hide_day else 2)


def test_requested_day_selection_requires_one_enabled_matching_portal_control():
    wanted = {'date': '2026-10-19'}
    c = day_control(wanted['date'])
    assert schema.requested_day_control({'controls': [c]}, wanted, []) == c
    assert schema.requested_day_control({'controls': [c]}, wanted, [{'op': 'click', 'control': c}]) is None
    for change in ({'disabled': True}, {'label': 'TUE\n19'}, {'label': 'MON\n26'},
                   {'frame': 'https://example.com'}, {'frame': PORTAL.replace(schema.BRANCH, 'a' * 24)}):
        assert schema.requested_day_control({'controls': [{**c, **change}]}, wanted, []) is None
    assert schema.requested_day_control({'controls': [c, {**c, 'ref': 'c-other'}]}, wanted, []) is None


def test_workout_native_flow_persists_review_and_submits_only_after_yes_once(rig):
    review(rig)
    model_calls = rig[1]['model']
    result = turn(rig, 'yes', 'confirm')[-1]
    assert 'Glofox confirmed' in result['content'] and rig[1]['posts'] == 1
    assert rig[1]['model'] == model_calls, 'Approval must never be model-generated.'
    assert turn(rig, 'yes', 'confirm')[-1] == result
    turn(rig, 'yes', 'duplicate')
    assert rig[1]['posts'] == 1
    raw = (rig[2] / 'bookings.sqlite3').read_bytes()
    assert b'Donna Albertone' not in raw and ('e' * 24).encode() not in raw


def test_legacy_member_adapter_survives_encrypted_review_and_one_owner_confirmation(rig):
    rig[1]['legacy_member'] = True
    review(rig)
    assert not rig[1]['posts']
    assert 'Glofox confirmed' in turn(rig, 'yes', 'confirm')[-1]['content']
    submit = rig[1]['calls'][-1]
    assert submit['stage'] == 'submit'
    assert schema.task(submit) == submit
    assert submit['request']['url'] == API
    assert submit['request']['body']['join_waiting_list'] is False
    assert submit['request']['native']['body']['pay_gym'] is False
    turn(rig, 'yes', 'duplicate')
    assert rig[1]['posts'] == 1
    raw = (rig[2] / 'bookings.sqlite3').read_bytes()
    assert b'pay_gym' not in raw and b'join_waiting_list' not in raw


def test_changed_version_adapter_cannot_be_saved_as_an_approvable_review(rig):
    rig[1].update(legacy_member=True, alter_adapter=True)
    result = turn(rig, 'Book my workout tomorrow at 8:30 AM', 'prepare')[-1]
    assert 'Reply **yes**' not in result['content']
    turn(rig, 'yes', 'confirm')
    assert not rig[1]['posts']


def test_missing_workout_date_and_time_are_collected_across_turns(rig):
    result = turn(rig, 'Book my workout', 'start')[-1]
    assert 'date and time' in result['content'] and not rig[1]['calls']
    assert 'Reply **yes**' in turn(rig, 'tomorrow at 8:30 AM', 'details')[-1]['content']


@pytest.mark.parametrize('prompt', [
    'Could you please book my workout tomorrow at 8:30 AM for Level 6?',
    "I'm looking to book my workout tomorrow at 8:30 AM.",
    'I want you to book my Level 6 class tomorrow at 8:30 AM.',
    'Can you help me book a Level 6 workout tomorrow at 8:30 AM?',
    'Go to the Level 6 website and book my workout tomorrow at 8:30 AM.',
    'Book my workout tomorrow at 8:30 AM for Level 6 on the website',
    'I need my Level 6 workout booked tomorrow at 8:30 AM.',
])
def test_natural_requests_prepare_native_class_instead_of_calendar(rig, prompt):
    result = turn(rig, prompt, 'natural')[-1]
    assert 'Reply **yes**' in result['content'] and not rig[1]['posts']
    assert rig[1]['calls'][-1]['url'] == schema.URL
    assert rig[1]['calls'][-1]['details']['date'] == DAY
    assert rig[1]['calls'][-1]['details']['time'] == '08:30'


@pytest.mark.parametrize('reply', [
    'Im asking you to go to the website and book the class',
    "I'm asking you to go to the website and book the class",
    "I mean book the class on the website, not on my calendar.",
    'Book the class on the website',
])
def test_owner_correction_recovers_details_from_misrouted_calendar_conversation(rig, reply):
    conv = chat.get_conversation('alice', rig[0])
    chat._append_message('alice', conv, 'user', 'Could you please book my workout tomorrow at 8:30 AM for Level 6?')
    chat._append_message('alice', conv, 'assistant', 'How long should the Level 6 Calendar event last?')
    conv['calendar_create_draft'] = {'title': 'Level 6 workout', 'date': DAY, 'time': '08:30', 'missing': 'duration'}
    chat._write('alice', conv)
    result = turn(rig, reply, 'correct-route')[-1]
    assert 'Reply **yes**' in result['content'] and 'Donna Albertone' in result['content']
    assert 'duration' not in result['content'] and 'studio name' not in result['content']
    assert not rig[1]['posts']
    assert 'calendar_create_draft' not in chat.get_conversation('alice', rig[0])
    # A correction prepares a review, never approves an earlier Calendar task.
    assert turn(rig, 'yes', 'confirmed-after-correction')[-1]['content'].startswith('Glofox confirmed')
    assert rig[1]['posts'] == 1


def test_recovery_uses_owner_details_without_assistant_guesses(rig):
    conv = chat.get_conversation('alice', rig[0])
    chat._append_message('alice', conv, 'user', 'Could you please book my workout for Level 6?')
    chat._append_message('alice', conv, 'assistant', 'I guessed October 15, 2026 at 8:30 AM.')
    chat._write('alice', conv)
    result = turn(rig, 'Im asking you to go to the website and book the class', 'no-guess')[-1]
    assert 'date and time' in result['content'] and not rig[1]['calls']


def test_month_day_reply_uses_local_year_without_duration_question(rig):
    turn(rig, 'Book my Level 6 workout at 8:30 AM', 'time')
    result = turn(rig, datetime.fromisoformat(DAY).strftime('%B %-d'), 'date')[-1]
    assert 'Reply **yes**' in result['content'] and DAY in rig[1]['calls'][-1]['details']['date']


@pytest.mark.parametrize('text', [
    "Don't book my workout on the website.",
    "I want to avoid booking a workout on the website.",
    "I want to know how to book my workout on the website.",
    'Explain how to book my workout.',
    'Put my Level 6 workout on my calendar tomorrow at 8:30 AM.',
    'Book my workout on my calendar tomorrow at 8:30 AM.',
    'Show my calendar today.',
])
def test_research_negation_and_explicit_calendar_requests_do_not_start_class_booking(text):
    assert flow.action(text, {}) is None


def test_unrelated_owner_request_breaks_implicit_class_context():
    conv = {'messages': [{'role': 'user', 'content': 'Book my Level 6 workout tomorrow at 8:30 AM'},
                         {'role': 'user', 'content': 'Show my calendar'},
                         {'role': 'assistant', 'content': 'Your calendar is empty'}]}
    assert flow.action('Book the class on the website', conv) is None
    assert flow.action('2026', conv) is None


def test_ambiguous_time_requires_a_choice_without_browser_preparation(rig):
    result = turn(rig, 'Book my workout tomorrow at 8:30 AM or 9:30 AM', 'ambiguous')[-1]
    assert 'Which one date and time' in result['content'] and not rig[1]['calls']


@pytest.mark.parametrize('studio', ['L6', 'l6', 'Level Six', 'level-six', 'Level6'])
def test_class_alias_enters_existing_glofox_review_not_restaurant(rig, studio):
    prompt = f'Can you book a class at {studio} tomorrow at 8:30 AM?'
    assert flow.restaurant.action(prompt, {}) is None
    result = turn(rig, prompt, 'class-alias')[-1]
    assert 'Reply **yes**' in result['content'] and 'Level 6' in result['content']
    assert 'party size' not in result['content'] and rig[1]['posts'] == 0
    assert rig[1]['calls'][-1]['details']['date'] == DAY
    assert rig[1]['calls'][-1]['details']['time'] == '08:30'
    assert turn(rig, 'yes', 'approve-alias')[-1]['content'].startswith('Glofox confirmed')
    turn(rig, 'yes', 'repeat-alias-approval')
    assert rig[1]['posts'] == 1


def test_explicit_rejection_of_restaurant_recovers_prior_owner_class_details(rig):
    conv = chat.get_conversation('alice', rig[0])
    chat._append_message('alice', conv, 'user', 'Can you book a class at L6 tomorrow at 8:30 AM?')
    chat._append_message('alice', conv, 'assistant', 'I need your party size, city or restaurant location.')
    chat._write('alice', conv)
    correction = "I don't want a reservation. I want to book a class for level six."
    assert flow.action(correction, conv) == 'start'
    result = turn(rig, correction, 'correct-class-domain')[-1]
    assert 'Reply **yes**' in result['content']
    assert 'party size' not in result['content'] and rig[1]['posts'] == 0
    assert rig[1]['calls'][-1]['details']['date'] == DAY
    assert rig[1]['calls'][-1]['details']['time'] == '08:30'


def test_weekday_date_conflict_stays_collecting_then_explicit_reply_prepares_review(rig):
    prompt = 'Can you book a class at L6 for Wednesday, October 20, 2026 at 8:30 AM?'
    result = turn(rig, prompt, 'weekday-conflict')[-1]
    assert 'is Tuesday' in result['content'] and 'you said Wednesday' in result['content']
    assert 'Which date' in result['content']
    assert not rig[1]['calls'] and rig[1]['model'] == 0
    conv = chat.get_conversation('alice', rig[0])
    entry = flow.restaurant.latest(conv)['workout_booking']
    row = booking_store.BookingStore().get('alice', entry['id'])
    assert row['state'] == 'collecting' and row['data']['kind'] == 'workout'
    assert row['data']['requested'] == {'time': '08:30'}
    result = turn(rig, 'Tuesday, October 20, 2026', 'resolve-weekday')[-1]
    assert 'Reply **yes**' in result['content'] and rig[1]['posts'] == 0
    assert rig[1]['calls'][-1]['details']['date'] == '2026-10-20'
    assert rig[1]['calls'][-1]['details']['time'] == '08:30'


def test_original_l6_request_with_bare_time_asks_about_conflicting_day(rig, monkeypatch):
    class ReportedDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 19, 12, tzinfo=tz)
    monkeypatch.setattr(flow, 'datetime', ReportedDay)
    result = turn(rig, 'Can you book a class at L6 for Wednesday, October 20th at 8.30?', 'original-l6')[-1]
    assert 'is Tuesday' in result['content'] and 'Which date' in result['content']
    assert not rig[1]['calls'] and rig[1]['model'] == 0 and rig[1]['posts'] == 0
    result = turn(rig, 'Tuesday, October 20, 2026 at 8:30 AM', 'explicit-date-time')[-1]
    assert 'Reply **yes**' in result['content'] and rig[1]['posts'] == 0
    assert rig[1]['calls'][-1]['details']['date'] == '2026-10-20'
    assert rig[1]['calls'][-1]['details']['time'] == '08:30'


def test_matching_weekday_and_numeric_date_are_one_date(rig):
    result = turn(rig, 'Book a class at L6 Tuesday, October 20, 2026 at 8:30 AM', 'redundant-date')[-1]
    assert 'Reply **yes**' in result['content'] and rig[1]['posts'] == 0
    assert rig[1]['calls'][-1]['details']['date'] == '2026-10-20'


def test_matching_tomorrow_and_month_day_are_one_date(rig):
    day = datetime.fromisoformat(DAY)
    prompt = 'Book a class at L6 tomorrow, ' + day.strftime('%B %d, %Y') + ' at 8:30 AM'
    result = turn(rig, prompt, 'relative-and-absolute')[-1]
    assert 'Reply **yes**' in result['content']
    assert rig[1]['calls'][-1]['details']['date'] == DAY and rig[1]['posts'] == 0


def test_conflicting_change_invalidates_review_and_yes_cannot_submit(rig):
    review(rig)
    result = turn(rig, 'Wednesday, October 20, 2026 at 8:30 AM', 'change-with-conflict')[-1]
    assert 'Which date' in result['content'] and rig[1]['posts'] == 0
    result = turn(rig, 'yes', 'not-an-approval')[-1]
    assert 'no current workout proposal' in result['content']
    assert rig[1]['posts'] == 0


@pytest.mark.parametrize('text', [
    "Don't book a class at L6 tomorrow at 8:30 AM.",
    'How do I book a class at L6?',
    'Add my L6 class to my calendar tomorrow at 8:30 AM.',
    'Book a table for dinner tomorrow at 8:30 PM.',
    'Book a class at Studio Elsewhere tomorrow at 8:30 AM.',
    'Book a table at L60 tomorrow at 8:30 PM.',
])
def test_class_alias_does_not_expand_booking_scope(text):
    assert flow.action(text, {}) is None


def test_active_review_does_not_capture_restaurant_or_research_requests(rig):
    review(rig)
    conv = chat.get_conversation('alice', rig[0])
    assert flow.action('Book a table tomorrow at 8:30 PM', conv) is None
    assert flow.action('Review my workout today', conv) is None
    assert flow.action('I want to know how to book on the website', conv) is None
    result = turn(rig, "Don't book the class", 'decline')[-1]
    assert 'Cancelled the workout proposal' in result['content'] and not rig[1]['posts']


def test_calendar_details_are_reused_only_after_explicit_website_correction(rig):
    conv = chat.get_conversation('alice', rig[0])
    chat._append_message('alice', conv, 'user', 'Put my Level 6 workout on my calendar tomorrow at 8:30 AM.')
    chat._append_message('alice', conv, 'assistant', 'How long should the event last?')
    chat._write('alice', conv)
    assert flow.action('2026', conv) is None
    result = turn(rig, 'Im asking you to go to the website and book the class', 'switch-to-site')[-1]
    assert 'Reply **yes**' in result['content'] and not rig[1]['posts']


def test_yes_with_changes_reprepares_the_class_without_submission(rig):
    review(rig)
    result = turn(rig, 'yes but 9:30 AM instead', 'edit')[-1]
    assert 'Reply **yes**' in result['content'] and not rig[1]['posts']
    assert rig[1]['calls'][-1]['details']['time'] == '09:30'


@pytest.mark.parametrize('reason', ['expired', 'trainer', 'stopped', 'cancel'])
def test_invalid_review_never_submits(rig, reason):
    review(rig)
    if reason == 'expired':
        with booking_store.BookingStore().db() as db: db.execute('UPDATE bookings SET updated=0')
    elif reason == 'trainer': rig[1]['changed_class'] = True
    elif reason == 'stopped': bots.set_status('browser-bot', 'stopped')
    else:
        assert 'Nothing was booked' in turn(rig, 'no', 'cancel')[-1]['content']
    turn(rig, 'yes', 'confirm')
    assert not rig[1]['posts']


def test_glofox_login_is_required_without_collecting_a_password(rig):
    rig[1]['login_required'] = True
    assert 'manual viewer' in turn(rig, 'Book my workout tomorrow at 8:30 AM', 'prepare')[-1]['content']
    assert not rig[1]['posts']


@pytest.mark.parametrize('reason, expected', [
    ('ChannelError: a browser host runs one task at a time', 'busy with another task'),
    ('BrowserChannelUnavailable: private-host-detail', 'Kyrex Cloud cannot reach'),
    ('HostUnavailable: browser host dispatch timed out waiting for a result', 'timeout'),
    ('ManualControlActive: browser profile is in manual control', 'manual control'),
    ('host level6training.com is not on this host\'s allowlist', 'site permissions'),
    ('browser tasks must be JSON of the form {"actions": []}', 'Rebuild its agent'),
    ('Exception: password=opaque-secret private@example.com', 'Check the Browser Host logs'),
])
@pytest.mark.parametrize('transport_error', [True, False])
def test_browser_failures_keep_actionable_reason_without_private_details(rig, monkeypatch, reason, expected, transport_error):
    def fail(**kw):
        rig[1]['calls'].append(kw)
        return (None, reason) if transport_error else ({'status': 'error', 'errors': [reason]}, None)
    monkeypatch.setattr(browser_host_bridge, 'request_browser_dispatch', fail)
    result = turn(rig, 'Book my workout tomorrow at 8:30 AM for level 6', 'failure')[-1]
    assert expected in result['content'] and 'Nothing was booked.' in result['content']
    assert not rig[1]['posts'] and len(rig[1]['calls']) == 1
    assert 'opaque-secret' not in result['content'] and 'private-host-detail' not in result['content']
    assert 'private@example.com' not in result['content']


def test_failed_submission_stays_unknown_and_cannot_retry(rig, monkeypatch):
    review(rig)
    calls = []
    def fail(**kw):
        calls.append(kw)
        return None, 'ChannelError: a browser host runs one task at a time'
    monkeypatch.setattr(browser_host_bridge, 'request_browser_dispatch', fail)
    result = turn(rig, 'yes', 'fail-submit')[-1]
    assert 'outcome is unknown' in result['content'] and 'Check Glofox My bookings' in result['content']
    assert 'Nothing was booked' not in result['content']
    turn(rig, 'yes', 'another-confirmation')
    assert len(calls) == 1


def test_unrelated_latest_reply_cannot_approve_old_workout(rig):
    review(rig)
    conv = chat.get_conversation('alice', rig[0])
    chat._append_message('alice', conv, 'assistant', 'Your calendar is empty.'); chat._write('alice', conv)
    assert flow.action('yes', conv) is None and not rig[1]['posts']


def test_cancelled_turn_and_foreign_bot_cannot_prepare(rig):
    cancel = asyncio.Event(); cancel.set()
    assert turn(rig, 'Book my workout tomorrow at 8:30 AM', 'cancel', cancel=cancel)[-1]['status'] == 'cancelled'
    assert not rig[1]['calls']
    other = bots.add_bot('other', 'Other', 'openai:test-model', str(rig[2]), owner='bob', status='running', policy={'fitness:read': 0})
    conv = chat.get_conversation('alice', rig[0]); conv['bot_id'] = other['id']; chat._write('alice', conv)
    with pytest.raises(Exception): turn(rig, 'Book my workout tomorrow at 8:30 AM', 'foreign')
    assert not rig[1]['calls']


def test_workout_request_routes_separately_from_restaurants_and_health_reads():
    import booking_chat
    assert flow.action('Book my workout tomorrow at 8:30 AM', {}) == 'start'
    assert booking_chat.action('Book my workout tomorrow at 8:30 AM', {}) is None
    assert flow.action('Review my workout today', {}) is None
    assert flow.action('Book a table tomorrow at 8:30 PM', {}) is None
    assert flow.action('Schedule my workout on my calendar tomorrow', {}) is None


def test_workout_wire_is_owner_bound_single_use_and_rejects_a_changed_class(rig):
    review(rig)
    store = booking_store.BookingStore(); conv = chat.get_conversation('alice', rig[0])
    entry = flow.restaurant.latest(conv)['workout_booking']
    row = store.claim('alice', entry['id'], entry['revision'], rig[0], 'turn-prepare-assistant')
    wire = store.wire(row, 'browser-bot', 'test-host', 'submit')
    tampered = json.loads(wire); tampered[schema.ACTION]['details']['event_id'] = 'b' * 24
    with pytest.raises(booking_store.BookingError): store.authorize('alice', 'browser-bot', 'test-host', json.dumps(tampered))
    with pytest.raises(booking_store.BookingError): store.authorize('bob', 'browser-bot', 'test-host', wire)
    store.authorize('alice', 'browser-bot', 'test-host', wire)
    assert store.active('alice', 'browser-bot', 'test-host', wire)
    with pytest.raises(booking_store.BookingError): store.authorize('alice', 'browser-bot', 'test-host', wire)
    store.request_cancel('alice', row['id'])
    assert not store.active('alice', 'browser-bot', 'test-host', wire)


@pytest.mark.parametrize('stage', ['prepare', 'submit'])
def test_disconnected_http_viewer_keeps_one_operation_and_recovers_saved_reply(rig, monkeypatch, stage):
    import chat_api
    if stage == 'submit': review(rig)
    real_dispatch = flow.dispatch

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def paused(store, row, mode, tasks, cancel):
            if mode == stage:
                started.set()
                await release.wait()
            return await real_dispatch(store, row, mode, tasks, cancel)
        monkeypatch.setattr(flow, 'dispatch', paused)
        monkeypatch.setattr(chat_api, '_require_user', lambda request: request.owner)
        async def body():
            return {'conversation_id': rig[0], 'message': 'yes' if stage == 'submit' else 'Book my workout tomorrow at 8:30 AM',
                    'request_id': 'sleep'}
        response = await chat_api.chat(SimpleNamespace(owner='alice', json=body))
        viewer = response.body_iterator
        assert 'conversation' in await anext(viewer)
        assert 'workout_booking' in await anext(viewer)
        await asyncio.wait_for(started.wait(), 2)
        producer = flow.running('alice', 'sleep')
        await viewer.aclose()
        assert not producer.done(), 'Closing SSE must not cancel the booking producer.'
        assert 'sleep' in chat_api._active_streams, 'Explicit Stop remains usable after losing the viewer.'
        saved = chat.get_conversation('alice', rig[0])
        assert flow.pending(saved) and 'do not resend' in saved['messages'][-1]['content']
        duplicate = [f async for f in chat.stream_chat('alice', rig[0], 'yes', request_id='duplicate-live')]
        assert 'still running' in duplicate[-1]['content'] and not rig[1]['posts']
        assert flow.running('bob', 'sleep') is None
        assert not flow.cancel_running('bob', 'sleep')
        release.set()
        await asyncio.wait_for(producer, 2)
        await asyncio.sleep(0)
        assert 'sleep' not in chat_api._active_streams
        saved = chat.get_conversation('alice', rig[0])
        assert not flow.pending(saved)
        result = saved['messages'][-1]
        assert result['id'] == 'turn-sleep-assistant'
        assert ('Reply **yes**' if stage == 'prepare' else 'Glofox confirmed') in result['content']
        assert rig[1]['posts'] == (stage == 'submit')
        if stage == 'prepare':
            result = [f async for f in chat.stream_chat('alice', rig[0], 'yes', request_id='real-confirm')]
            assert 'Glofox confirmed' in result[-1]['content']
        await asyncio.sleep(0)
        again = [f async for f in chat.stream_chat('alice', rig[0], 'yes', request_id='sleep')]
        assert again[-1]['content'] and rig[1]['posts'] == 1
        assert len([m for m in chat.get_conversation('alice', rig[0])['messages'] if m['id'] == 'turn-sleep-assistant']) == 1
    asyncio.run(run())


def test_explicit_stop_after_disconnect_still_stops_preparation(rig, monkeypatch):
    import chat_api
    async def run():
        started = asyncio.Event()
        cancel = asyncio.Event()
        async def paused(store, row, mode, tasks, stop):
            started.set()
            await stop.wait()
            raise booking_store.BookingError('Workout preparation stopped. Nothing was booked.')
        monkeypatch.setattr(flow, 'dispatch', paused)
        stream = chat.stream_chat('alice', rig[0], 'Book my workout tomorrow at 8:30 AM', request_id='stop-after-sleep', cancel_event=cancel)
        await anext(stream)
        await asyncio.wait_for(started.wait(), 2)
        producer = flow.running('alice', 'stop-after-sleep')
        await stream.aclose()
        monkeypatch.setattr(chat_api, '_require_user', lambda _: 'alice')
        async def body(): return {'request_id': 'stop-after-sleep'}
        assert (await chat_api.cancel_chat(SimpleNamespace(json=body)))['cancelled']
        await asyncio.wait_for(producer, 2)
        saved = chat.get_conversation('alice', rig[0])
        assert 'preparation stopped' in saved['messages'][-1]['content']
        assert not flow.pending(saved) and not rig[1]['posts']
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['prepare', 'submit'])
def test_bounded_producer_retires_authority_without_replaying_submission(rig, monkeypatch, stage, inline_class_lookup):
    if stage == 'submit': review(rig)
    monkeypatch.setattr(flow, 'TURN_SECONDS', .05)
    async def paused(*args): await asyncio.Event().wait()
    monkeypatch.setattr(flow, 'dispatch', paused)
    frames = turn(rig, 'yes' if stage == 'submit' else 'Book my workout tomorrow at 8:30 AM', 'deadline')
    answer = frames[-1]['content']
    assert inline_class_lookup
    assert ('unknown' in answer) if stage == 'submit' else ('Nothing was booked' in answer)
    if stage == 'prepare':
        assert 'reached its time limit' in answer and 'Browser Host preparation' in answer
        assert 'browser pass 1 of 16' in answer and 'interrupted' not in answer
    saved = chat.get_conversation('alice', rig[0])
    assert not flow.pending(saved) and not rig[1]['posts']
    row = booking_store.BookingStore().get('alice', saved['messages'][-1]['workout_booking']['id'])
    assert row['state'] == ('unknown' if stage == 'submit' else 'blocked')
    assert row['data']['cancel_requested']
    turn(rig, 'yes', 'repeat-deadline')
    assert not rig[1]['posts']


@pytest.mark.parametrize('phase', ['details', 'planner'])
def test_slow_preparation_model_has_its_own_deadline_and_saved_stage(rig, monkeypatch, phase):
    monkeypatch.setattr(flow, 'MODEL_SECONDS', .02)
    async def paused(**kw):
        await asyncio.Event().wait()
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=paused))
    text = 'Book my workout' if phase == 'details' else 'Book my workout tomorrow at 8:30 AM'
    result = turn(rig, text, 'slow-model')[-1]
    assert 'chat model exceeded its preparation step time limit' in result['content']
    assert flow.PREPARATION_STAGES[phase] in result['content']
    assert 'Nothing was booked' in result['content'] and not rig[1]['posts']
    saved = chat.get_conversation('alice', rig[0])
    assert not flow.pending(saved)
    row = booking_store.BookingStore().get('alice', saved['messages'][-1]['workout_booking']['id'])
    assert row['state'] == 'blocked'
    if phase == 'planner': assert row['data']['cancel_requested']
    assert row['data']['preparation']['phase'] == phase
    assert len(rig[1]['calls']) == (1 if phase == 'planner' else 0)


@pytest.mark.parametrize('parent_deadline', [False, True])
def test_model_wait_distinguishes_parent_deadline_from_worker_interruption(rig, monkeypatch, parent_deadline, inline_class_lookup):
    monkeypatch.setattr(flow, 'TURN_SECONDS', .2)
    monkeypatch.setattr(flow, 'MODEL_SECONDS', 10)
    async def paused(**kw):
        if not parent_deadline:
            raise asyncio.CancelledError()
        await asyncio.Event().wait()
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=paused))
    result = turn(rig, 'Book my workout tomorrow at 8:30 AM', 'interrupted-model')[-1]
    assert inline_class_lookup
    assert ('reached its time limit' if parent_deadline else 'was interrupted') in result['content']
    assert 'chat model to choose a portal control' in result['content']
    assert 'browser pass 1 of 16' in result['content']
    assert 'model exceeded' not in result['content'] and not rig[1]['posts']
    saved = chat.get_conversation('alice', rig[0])
    row = booking_store.BookingStore().get('alice', saved['messages'][-1]['workout_booking']['id'])
    assert row['state'] == 'blocked' and row['data']['cancel_requested']
    assert not flow.pending(saved)


@pytest.mark.parametrize('approval', [False, True])
def test_class_lookup_deadline_reports_schedule_stage_before_any_submission(rig, monkeypatch, approval):
    if approval: review(rig)
    monkeypatch.setattr(flow, 'TURN_SECONDS', .1)
    to_thread = asyncio.to_thread
    async def paused(func, *args, **kwargs):
        if func is workout_class_reader.class_at:
            await asyncio.Event().wait()
        return await to_thread(func, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', paused)
    calls = len(rig[1]['calls'])
    result = turn(rig, 'yes' if approval else 'Book my workout tomorrow at 8:30 AM', 'slow-schedule')[-1]
    assert 'reached its time limit' in result['content'] and 'checking the public class schedule' in result['content']
    assert 'Nothing was booked' in result['content'] and 'unknown' not in result['content']
    assert len(rig[1]['calls']) == calls and not rig[1]['posts']
    assert not flow.pending(chat.get_conversation('alice', rig[0]))


@pytest.mark.parametrize('pages,expected_calls', [(['A', 'A'], 2), (['A', 'B', 'A'], 3)])
def test_repeated_portal_page_and_action_stops_without_replaying_again(rig, monkeypatch, pages, expected_calls):
    browser = browser_host_bridge.request_browser_dispatch
    def looping(**kw):
        result, error = browser(**kw)
        result.pop('review', None)
        snap = result['snapshot']
        snap['text'] += ' ' + pages[len(rig[1]['calls']) - 1]
        # Opaque reference renumbering must not defeat the loop guard.
        snap['controls'][0]['ref'] = 'c' + str(len(rig[1]['calls']))
        return result, error
    async def choose(**kw):
        data = json.loads(kw['messages'][1]['content'])
        ref = data['snapshot']['controls'][0]['ref']
        return {'content': json.dumps({'action': 'click', 'ref': ref})}
    monkeypatch.setattr(browser_host_bridge, 'request_browser_dispatch', looping)
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=choose))
    result = turn(rig, 'Book my workout tomorrow at 8:30 AM', 'looping-portal')[-1]
    assert 'repeated portal page/action loop' in result['content'] and 'Nothing was booked' in result['content']
    assert len(rig[1]['calls']) == expected_calls and not rig[1]['posts']
    saved = chat.get_conversation('alice', rig[0])
    row = booking_store.BookingStore().get('alice', saved['messages'][-1]['workout_booking']['id'])
    assert row['state'] == 'blocked' and row['data']['cancel_requested']
    assert len(row['data']['steps']) == expected_calls - 1


def test_repeated_control_with_changed_page_can_still_reach_review(rig, monkeypatch):
    browser = browser_host_bridge.request_browser_dispatch
    def progressing(**kw):
        result, error = browser(**kw)
        if len(rig[1]['calls']) < 3:
            result.pop('review', None)
            result['snapshot']['text'] += ' page ' + str(len(rig[1]['calls']))
        return result, error
    monkeypatch.setattr(browser_host_bridge, 'request_browser_dispatch', progressing)
    result = turn(rig, 'Book my workout tomorrow at 8:30 AM', 'progressing-portal')[-1]
    assert 'Reply **yes**' in result['content'] and len(rig[1]['calls']) == 3
    assert not rig[1]['posts']


@pytest.mark.parametrize('correction', ['book', 'repeat', 'unknown', 'changed_time', 'blocked', 'cancel'])
def test_repeated_action_gets_one_model_only_correction_with_history(rig, monkeypatch, correction):
    browser = browser_host_bridge.request_browser_dispatch
    plans = []
    cancel = asyncio.Event()

    def with_classes_tab(**kw):
        result, error = browser(**kw)
        tab = control()
        tab.update(ref='c-tab-' + str(len(rig[1]['calls'])), selector='css=#classes', label='Classes')
        tab['fingerprint'] = common.fingerprint(tab)
        result['snapshot']['controls'].insert(0, tab)
        return result, error

    async def choose(**kw):
        data = json.loads(kw['messages'][1]['content'])
        plans.append(data)
        tab = data['snapshot']['controls'][0]
        if data['rejected_action']:
            assert len(rig[1]['calls']) == 2, 'Replanning must not dispatch the repeated action.'
            assert len(data['previous_actions']) == 1
            assert data['previous_actions'][0]['control']['selector'] == 'css=#classes'
            assert data['previous_actions'][0]['control']['ref'] != tab['ref']
            assert data['rejected_action'] == {'action': 'click', 'ref': tab['ref']}
            if correction == 'book':
                plan = {'action': 'click', 'ref': 'c0'}
            elif correction == 'unknown':
                plan = {'action': 'click', 'ref': 'not-observed'}
            elif correction == 'changed_time':
                plan = {'action': 'fill', 'ref': 'c0', 'field': 'time', 'value': '09:30'}
            elif correction == 'blocked':
                plan = {'action': 'blocked', 'evidence': ''}
            else:
                plan = {'action': 'click', 'ref': tab['ref']}
        else:
            if correction == 'cancel' and data['previous_actions']:
                cancel.set()
            plan = {'action': 'click', 'ref': tab['ref']}
        return {'content': json.dumps(plan)}

    monkeypatch.setattr(browser_host_bridge, 'request_browser_dispatch', with_classes_tab)
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=choose))
    result = turn(rig, 'Book my workout tomorrow at 8:30 AM', 'correct-loop', cancel=cancel)[-1]
    assert not rig[1]['posts']
    assert len(plans) == (2 if correction == 'cancel' else 3)
    assert len(rig[1]['calls']) == (3 if correction == 'book' else 2)
    saved = chat.get_conversation('alice', rig[0])
    row = booking_store.BookingStore().get('alice', saved['messages'][-1]['workout_booking']['id'])
    assert [s['control']['selector'] for s in row['data']['steps']] == ['css=#classes']
    if correction == 'book':
        assert 'Reply **yes**' in result['content'] and row['state'] == 'proposed'
        assert row['data']['submit']['selector'] == 'css=#book'
    else:
        assert row['state'] in {'blocked', 'cancelled'}
        assert 'Reply **yes**' not in result['content']
        if correction == 'repeat':
            assert 'repeated portal page/action loop' in result['content']


def test_preparation_failure_projects_only_allowlisted_stage_and_step():
    secret = 'private page and account data'
    answer = flow.preparation_failure({'preparation': {'phase': secret, 'step': secret}}, 'deadline')
    assert secret not in answer and 'Last stage' not in answer
    answer = flow.preparation_failure({'preparation': {'phase': 'browser', 'step': secret}}, 'interrupted')
    assert secret not in answer and 'Browser Host preparation' in answer and 'pass' not in answer


def test_worker_recovery_preserves_saved_preparation_stage_without_dispatch(rig):
    store = booking_store.BookingStore()
    row = store.create('alice', rig[0], 'abandoned-planning')
    row = store.update(row, {'kind': 'workout', 'preparation': {'phase': 'planner', 'step': 3}}, 'preparing')
    conv = chat.get_conversation('alice', rig[0])
    message = chat._append_message('alice', conv, 'assistant', 'Checking workout')
    message['workout_pending'] = {'deadline': 0, 'id': row['id']}
    chat._write('alice', conv)
    recovered = chat.get_conversation('alice', rig[0])
    answer = recovered['messages'][-1]['content']
    assert 'was interrupted' in answer and 'chat model to choose a portal control' in answer
    assert 'browser pass 3 of 16' in answer and 'Nothing was booked' in answer
    assert not flow.pending(recovered) and not rig[1]['calls']
    assert store.get('alice', row['id'])['state'] == 'blocked'


def test_model_deadline_while_changing_details_invalidates_old_proposal(rig, monkeypatch):
    review(rig)
    monkeypatch.setattr(flow, 'MODEL_SECONDS', .02)
    async def paused(**kw): await asyncio.Event().wait()
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=paused))
    result = turn(rig, 'Please check the date again', 'change-slow')[-1]
    assert 'chat model exceeded' in result['content'] and 'Nothing was booked' in result['content']
    saved = chat.get_conversation('alice', rig[0])
    row = booking_store.BookingStore().get('alice', saved['messages'][-1]['workout_booking']['id'])
    assert row['state'] == 'blocked'
    turn(rig, 'yes', 'stale-yes')
    assert not rig[1]['posts']


@pytest.mark.parametrize('state,legacy', [('preparing', False), ('submitting', False),
                                       ('confirmed', False), ('preparing', True), ('submitting', True)])
def test_restart_and_legacy_recovery_never_dispatch_or_guess_success(rig, state, legacy):
    store = booking_store.BookingStore()
    conv = chat.get_conversation('alice', rig[0])
    row = store.create('alice', rig[0], 'workout:' + rig[0] + ':orphan')
    row = store.update(row, {'kind': 'workout', 'details': DETAILS,
                            'receipt': {'reference': 'recorded-receipt'}}, state)
    with store.db() as db: db.execute('UPDATE bookings SET updated=0 WHERE id=?', (row['id'],))
    chat._append_message('alice', conv, 'user', 'Book my workout tomorrow at 8:30 AM', identity='turn-orphan-user')
    if not legacy:
        message = chat._append_message('alice', conv, 'assistant', 'Checking the existing booking', identity='turn-orphan-assistant')
        message['workout_pending'] = {'deadline': 0, 'id': row['id']}
    chat._write('alice', conv)
    assert chat.get_conversation('bob', rig[0]) is None
    recovered = chat.get_conversation('alice', rig[0])
    answer = recovered['messages'][-1]['content']
    assert not flow.pending(recovered) and not rig[1]['calls']
    if state == 'confirmed':
        assert 'Glofox confirmed' in answer and 'recorded-receipt' in answer
    elif state == 'submitting':
        assert 'unknown' in answer and 'Nothing was booked' not in answer
    else:
        assert 'interrupted' in answer and 'Nothing was booked' in answer
    assert len(chat.get_conversation('alice', rig[0])['messages']) == 2


def test_stop_during_class_recheck_cannot_claim_or_dispatch_approval(rig, monkeypatch):
    review(rig)
    real_class_at = workout_class_reader.class_at
    async def run():
        cancel = asyncio.Event()
        loop = asyncio.get_running_loop()
        def recheck(*args):
            loop.call_soon_threadsafe(cancel.set)
            return real_class_at(*args)
        monkeypatch.setattr(workout_class_reader, 'class_at', recheck)
        count = len(rig[1]['calls'])
        frames = [f async for f in chat.stream_chat('alice', rig[0], 'yes', request_id='stop-recheck', cancel_event=cancel)]
        assert frames[-1]['status'] == 'cancelled'
        assert len(rig[1]['calls']) == count and not rig[1]['posts']
        assert not flow.pending(chat.get_conversation('alice', rig[0]))
    asyncio.run(run())


def test_another_worker_reads_unexpired_pending_turn_without_retiring_it(rig):
    store = booking_store.BookingStore()
    conv = chat.get_conversation('alice', rig[0])
    row = store.create('alice', rig[0])
    row = store.update(row, {'kind': 'workout'}, 'preparing')
    message = chat._append_message('alice', conv, 'assistant', 'Checking the existing booking')
    message['workout_pending'] = {'id': row['id'], 'deadline': flow.time.time() + 60}
    chat._write('alice', conv)
    assert flow.pending(chat.get_conversation('alice', rig[0]))
    assert store.get('alice', row['id'])['revision'] == row['revision']
    assert not rig[1]['calls']
