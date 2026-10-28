"""Pin the public lookup to a real future slot; it may never issue a booking write."""
import json
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import pytest
import glofox_api as api
import workout_class_reader


@pytest.fixture
def rig(monkeypatch):
    start = (datetime.now(api.BRANCH_TZ) + timedelta(days=1)).replace(hour=8, minute=30, second=0, microsecond=0)
    event = {'_id': 'c' * 24, 'branch_id': api.BRANCH_ID, 'name': api.SCHEDULED_CLASS_NAME,
             'time_start': int(start.timestamp()), 'type': 'event', 'duration': 45,
             'trainers': ['d' * 24], 'trainers_obj': [], 'size': 18, 'booked': 10, 'status': 'AVAILABLE'}
    state = {'events': [event], 'calls': []}
    def http(request):
        state['calls'].append(request)
        path = urlsplit(request.full_url).path
        if path.endswith('/login'): return json.dumps({'token': 'fixture-guest'}).encode()
        if path.endswith('/' + api.BRANCH_ID):
            return json.dumps({'_id': api.BRANCH_ID, 'address': {'timezone_id': 'America/New_York'}}).encode()
        records = state['events'] if path.endswith('/events') else [
            {'_id': 'd' * 24, 'branch_id': api.BRANCH_ID, 'name': 'Donna Albertone', 'type': 'trainer', 'active': True}]
        return json.dumps({'object': 'list', 'data': records, 'total_count': len(records), 'has_more': False}).encode()
    monkeypatch.setattr(api, '_bounded_raw_response', http)
    return start, state


def test_exact_class_lookup_is_public_and_never_books(rig):
    start, state = rig
    result = workout_class_reader.class_at(start.date().isoformat(), '08:30')
    assert result['event_id'] == 'c' * 24 and result['trainer_name'] == 'Donna Albertone'
    assert result['start'] == int(start.timestamp())
    assert [r.method for r in state['calls']] == ['POST', 'GET', 'GET', 'GET']
    assert json.loads(state['calls'][0].data) == {'branch_id': api.BRANCH_ID, 'login': 'GUEST', 'password': 'GUEST'}


@pytest.mark.parametrize('change', ['full', 'duplicate', 'foreign', 'wrong_time'])
def test_unavailable_or_ambiguous_slot_cannot_produce_a_booking_proposal(rig, change):
    start, state = rig
    if change == 'full': state['events'][0]['booked'] = 18
    elif change == 'duplicate': state['events'].append(dict(state['events'][0]))
    elif change == 'foreign': state['events'][0]['branch_id'] = 'other'
    else: state['events'][0]['time_start'] += 3600
    with pytest.raises(api.GlofoxError): workout_class_reader.class_at(start.date().isoformat(), '08:30')


def test_trainer_read_uses_same_validated_source_even_when_class_is_full(rig):
    start, state = rig
    state['events'][0]['booked'] = 18
    result = workout_class_reader.class_at(start.date().isoformat(), '08:30', for_booking=False)
    assert result['trainer_name'] == 'Donna Albertone' and result['event_id'] == 'c' * 24
    with pytest.raises(api.GlofoxDataError):
        workout_class_reader.class_at(start.date().isoformat(), '08:30')
    assert all('/bookings' not in r.full_url for r in state['calls'])


@pytest.mark.parametrize('change', ['duplicate', 'foreign', 'wrong_time', 'unresolved_trainer'])
def test_trainer_reads_keep_exact_class_and_trainer_validation(rig, change):
    start, state = rig
    if change == 'duplicate': state['events'].append(dict(state['events'][0]))
    elif change == 'foreign': state['events'][0]['branch_id'] = 'other'
    elif change == 'wrong_time': state['events'][0]['time_start'] += 3600
    else: state['events'][0]['trainers'] = ['e' * 24]
    with pytest.raises(api.GlofoxError):
        workout_class_reader.class_at(start.date().isoformat(), '08:30', for_booking=False)


def test_trainer_read_can_inspect_earlier_today_without_making_it_bookable(rig):
    _, state = rig
    start = datetime.now(api.BRANCH_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    state['events'][0]['time_start'] = int(start.timestamp())
    assert workout_class_reader.class_at(start.date().isoformat(), '00:00', for_booking=False)['trainer_name'] == 'Donna Albertone'
    count = len(state['calls'])
    with pytest.raises(api.GlofoxDataError): workout_class_reader.class_at(start.date().isoformat(), '00:00')
    assert len(state['calls']) == count
