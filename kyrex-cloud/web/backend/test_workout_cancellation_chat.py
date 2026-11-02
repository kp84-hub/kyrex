"""Real native Chat routing and owner-bound review; no external cancellations."""
import asyncio

import pytest
import booking_store
import booking_schema as common
import browser_host_bridge
import member_bookings
import workout_action_choice as choice
import workout_booking_chat as flow
import workout_cancellation_chat as cancellation
import workout_cancellation_schema as schema
import chat_service as chat

from test_workout_booking_chat import rig, turn, control, DAY, DETAILS

ROW = {'reference': 'a' * 24, 'event_id': 'c' * 24, 'title': 'Group Fitness Class',
       'start': DETAILS['start'], 'end': DETAILS['start'] + 2700}
IDENTITY = 'f' * 64
TERMS = 'Confirm booking cancellation Are you sure you want to cancel your booking? Cancel booking'


@pytest.fixture
def cancel_rig(rig, monkeypatch):
    state = rig[1]
    state.update(deletes=0, reads=0, cancel_calls=[], cancel_outcome='confirmed')
    def read(owner, window, task_id, store):
        assert owner == 'alice'
        state['reads'] += 1
        return {'bookings': state.get('cancel_rows', [dict(ROW)]), 'identity': IDENTITY,
                'window': window, 'browser_binding': {'bot': 'browser-bot', 'host': 'test-host', 'allowlist': ['*']}}
    monkeypatch.setattr(member_bookings, 'read', read)
    original = browser_host_bridge.request_browser_dispatch
    def browser(**kw):
        import json
        spec = json.loads(kw['task_text'])['workout_browser']
        if spec.get('operation') != 'cancel': return original(**kw)
        store = booking_store.BookingStore()
        store.authorize(kw['owner'], kw['bot_id'], kw['host_id'], kw['task_text'])
        assert store.active(kw['owner'], kw['bot_id'], kw['host_id'], kw['task_text'])
        state['cancel_calls'].append(spec)
        if spec['stage'] == 'submit':
            if state['cancel_outcome'] != 'changed': state['deletes'] += 1
            if state.get('cancel_transport_error'): return None, 'HostUnavailable: response lost'
            return {'mode': 'workout_browser', 'outcome': state['cancel_outcome'], 'reference': ROW['reference']}, None
        c = control(); c.update(selector='css=#general-dialog-confirm-button', label='Cancel booking')
        c['fingerprint'] = common.fingerprint(c)
        native = schema.request('https://api.glofox.com/2.0/bookings/' + ROW['reference'], 'DELETE', None, IDENTITY, ROW['reference'])
        review = {'submit': c, 'request': native, 'terms': TERMS,
                  'expected': schema.digest(spec, c, TERMS, native)}
        if state.get('tamper_cancel'): review['expected'] = 'd' * 64
        return {'mode': 'workout_browser', 'snapshot': {}, 'review': review}, None
    monkeypatch.setattr(browser_host_bridge, 'request_browser_dispatch', browser)
    return rig


def test_cancel_routes_to_existing_reservation_and_requires_its_own_exact_approval(cancel_rig, monkeypatch):
    async def forbidden(*a, **kw): pytest.fail('Cancellation reached the booking intent model.')
    monkeypatch.setattr(choice, 'classify', forbidden)
    result = turn(cancel_rig, 'Can you cancel my class at Level 6 tomorrow at 8:30am', 'cancel-first')[-1]
    assert 'Reply **yes** to cancel' in result['content'] and DAY in str(cancel_rig[1]['cancel_calls'][0])
    assert cancel_rig[1]['model'] == 0 and cancel_rig[1]['posts'] == 0 and cancel_rig[1]['deletes'] == 0
    assert 'Cancel **Level 6' in result['content']
    assert turn(cancel_rig, 'Can you cancel my class at Level 6 tomorrow at 8:30am', 'cancel-first')[-1] == result
    assert len(cancel_rig[1]['cancel_calls']) == 1
    assert 'confirmed cancellation' in turn(cancel_rig, 'yes', 'cancel-yes')[-1]['content']
    turn(cancel_rig, 'yes', 'cancel-yes'); turn(cancel_rig, 'yes', 'another-yes')
    assert cancel_rig[1]['deletes'] == 1 and not cancel_rig[1]['posts']


def test_cancel_invalidates_old_booking_review_without_booking_another_class(cancel_rig):
    from test_workout_booking_chat import review
    review(cancel_rig)
    prior = flow.restaurant.latest(chat.get_conversation('alice', cancel_rig[0]))['workout_booking']['id']
    turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'cancel-instead')
    assert booking_store.BookingStore().get('alice', prior)['state'] == 'cancelled'
    turn(cancel_rig, 'yes', 'cancel-not-book')
    assert cancel_rig[1]['deletes'] == 1 and cancel_rig[1]['posts'] == 0


def test_no_stops_only_the_cancellation_proposal(cancel_rig):
    turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'cancel-question')
    result = turn(cancel_rig, 'no', 'keep-booking')[-1]
    assert 'reservation was not cancelled' in result['content']
    turn(cancel_rig, 'yes', 'old-yes')
    assert not cancel_rig[1]['deletes'] and not cancel_rig[1]['posts']


def test_booking_words_are_not_cancellation_approval(cancel_rig):
    turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'separate-approval')
    conv = chat.get_conversation('alice', cancel_rig[0])
    assert cancellation.candidate('book it', conv)  # Never fall into an unrelated approval.
    assert 'reservation was not cancelled' in turn(cancel_rig, 'book it', 'not-cancellation')[-1]['content']
    assert not cancel_rig[1]['deletes'] and not cancel_rig[1]['posts']


def test_cancel_it_can_confirm_only_the_exact_cancellation_review(cancel_rig):
    turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'cancellation-words')
    assert 'confirmed cancellation' in turn(cancel_rig, 'cancel it', 'confirm-cancellation')[-1]['content']
    assert cancel_rig[1]['deletes'] == 1 and not cancel_rig[1]['posts']


@pytest.mark.parametrize('rows', [[], [ROW, ROW]])
def test_missing_or_ambiguous_reservation_never_prepares_a_cancel(cancel_rig, rows):
    cancel_rig[1]['cancel_rows'] = rows
    result = turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'no-exact-booking')[-1]
    assert 'one exact booked' in result['content'] and not cancel_rig[1]['cancel_calls']
    assert not cancel_rig[1]['deletes']


@pytest.mark.parametrize('transport', [False, True])
def test_unknown_cancellation_cannot_be_replayed_even_from_another_conversation(cancel_rig, transport):
    turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'cancel-review')
    cancel_rig[1].update(cancel_outcome='unknown', cancel_transport_error=transport)
    assert 'outcome is unknown' in turn(cancel_rig, 'yes', 'uncertain-cancel')[-1]['content']
    second = (chat.create_conversation('alice')['conversation_id'], cancel_rig[1], cancel_rig[2])
    answer = turn(second, 'Cancel my Level 6 class tomorrow at 8:30am', 'fresh-cancel')[-1]
    assert 'outcome is unknown' in answer['content']
    assert cancel_rig[1]['deletes'] == 1


def test_tampered_review_is_not_approvable(cancel_rig):
    cancel_rig[1]['tamper_cancel'] = True
    assert 'could not be verified' in turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'bad-review')[-1]['content']
    turn(cancel_rig, 'yes', 'bad-yes')
    assert not cancel_rig[1]['deletes']


@pytest.mark.parametrize('text', ["Don't cancel my Level 6 class tomorrow", 'How do I cancel a class at Level 6?',
    'Cancel my Level 6 membership', 'Cancel the Level 6 class on my calendar',
    "Cancel my daughter's Level 6 class tomorrow at 8:30am",
    'For example: cancel my Level 6 class tomorrow at 8:30am'])
def test_cancellation_does_not_capture_other_operations(text):
    assert not cancellation.requested(text)


def test_recovery_never_replays_a_cancel_and_keeps_the_outcome_honest(cancel_rig):
    turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'saved-cancel')
    conv = chat.get_conversation('alice', cancel_rig[0]); entry = flow.restaurant.latest(conv)['workout_cancellation']
    store = booking_store.BookingStore(); row = store.get('alice', entry['id'])
    row = store.update(row, row['data'], 'submitting')
    pending = chat._append_message('alice', conv, 'assistant', 'Cancelling…')
    pending['workout_pending'] = {'id': row['id'], 'deadline': 0, 'submitting': True, 'operation': 'cancel'}
    chat._write('alice', conv)
    result = chat.get_conversation('alice', cancel_rig[0])
    assert 'cancellation outcome is unknown' in result['messages'][-1]['content']
    assert not cancel_rig[1]['deletes'] and len(cancel_rig[1]['cancel_calls']) == 1


def test_expired_cancellation_review_and_stop_never_send_a_delete(cancel_rig):
    turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'expiry-review')
    conv = chat.get_conversation('alice', cancel_rig[0]); entry = flow.restaurant.latest(conv)['workout_cancellation']
    store = booking_store.BookingStore()
    with store.db() as db: db.execute('UPDATE bookings SET updated=0 WHERE id=?', (entry['id'],))
    assert 'expired' in turn(cancel_rig, 'yes', 'expired-cancel')[-1]['content']
    assert not cancel_rig[1]['deletes']
    stop = asyncio.Event(); stop.set()
    result = turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'stopped-cancel', cancel=stop)[-1]
    assert result['status'] == 'cancelled' and not cancel_rig[1]['deletes']


def test_explicit_browser_cancel_deny_revokes_a_prepared_review(cancel_rig):
    import bots
    turn(cancel_rig, 'Cancel my Level 6 class tomorrow at 8:30am', 'denied-review')
    bot = bots.get_bot('browser-bot')
    bots.update_bot(bot['id'], policy={**bot['policy'], 'browser:cancel_booking': 'deny'})
    result = turn(cancel_rig, 'yes', 'denied-delete')[-1]
    assert 'confirmed cancellation' not in result['content'] and not cancel_rig[1]['deletes']


def test_cancel_wording_never_becomes_booking_preparation_with_old_booking_context(cancel_rig):
    from test_workout_booking_chat import review
    review(cancel_rig)
    conv = chat.get_conversation('alice', cancel_rig[0])
    assert flow.action('Cancel my Level 6 membership', conv) is None
    assert not choice.candidate('Cancel the Level 6 event on my calendar', conv)


@pytest.mark.parametrize('body', [None, [], {}, {'url': 'https://other.example/'}])
def test_review_cannot_smuggle_another_native_request(body):
    c = control(); c.update(selector='css=#general-dialog-confirm-button', label='Cancel booking')
    c['fingerprint'] = common.fingerprint(c)
    with pytest.raises(ValueError):
        schema.digest({'details': ROW, 'window': {}, 'identity': IDENTITY}, c, TERMS, body)


@pytest.mark.parametrize('terms', ['Confirm booking cancellation Cancelling now costs $15. Cancel booking',
    'Confirm booking cancellation You cannot cancel this booking. OK',
    TERMS + ' A cancellation fee may apply.', 'Cancel booking'])
def test_only_observed_no_fee_confirmations_are_supported(terms):
    with pytest.raises(ValueError): schema.terms(terms)


@pytest.mark.parametrize('owner,thread', [('bob', 'foreign'), ('alice', 'another-thread')])
def test_cancellation_review_cannot_cross_owner_or_conversation(cancel_rig, owner, thread):
    store = booking_store.BookingStore()
    row = store.create(owner, thread, 'foreign-cancellation')
    row = store.update(row, {'kind': 'workout_cancel', 'details': ROW}, 'proposed')
    conv = chat.get_conversation('alice', cancel_rig[0])
    message = chat._append_message('alice', conv, 'assistant', 'Cancel review')
    message['workout_cancellation'] = {'id': row['id'], 'revision': row['revision'], 'state': 'proposed'}
    chat._write('alice', conv)
    turn(cancel_rig, 'yes', 'foreign-cancel-approval')
    assert store.get(owner, row['id']) == row
    assert not cancel_rig[1]['deletes'] and not cancel_rig[1]['cancel_calls']
