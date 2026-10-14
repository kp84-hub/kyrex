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
        if spec['stage'] == 'submit':
            assert spec['expected'] == common.review_hash(snap)
            state['posts'] += 1
            return {'mode': schema.ACTION, 'outcome': state['outcome'], 'reference': 'd' * 24, 'url': PORTAL}, None
        if spec['steps']:
            template = schema.request_template(API, 'POST', {'model': 'event', 'model_id': 'c' * 24,
                'user_id': 'e' * 24, 'join_waiting_list': False}, 'f' * 64)
            return {'mode': schema.ACTION, 'snapshot': snap, 'review': {'submit': c, 'request': template,
                'steps': [], 'terms': 'Group Fitness Class Included in your membership'}}, None
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


def test_missing_workout_date_and_time_are_collected_across_turns(rig):
    result = turn(rig, 'Book my workout', 'start')[-1]
    assert 'date and time' in result['content'] and not rig[1]['calls']
    assert 'Reply **yes**' in turn(rig, 'tomorrow at 8:30 AM', 'details')[-1]['content']


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
