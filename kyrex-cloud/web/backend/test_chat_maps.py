"""Consented per-turn location, fixed Google adapter, and no invented distances."""
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
import bots
import chat_maps as maps
import chat_service as chat
from test_vision import setup
from test_chat_bot_policy import _StubSession, _spawn_capture


def fix(**changes):
    return {'status': 'available', 'latitude': 35.701234, 'longitude': -78.731234,
            'accuracy_meters': 20, 'captured_at': time.time() * 1000, **changes}


@pytest.mark.parametrize('change', [{'latitude': True}, {'longitude': float('nan')},
    {'latitude': 91}, {'longitude': -181}, {'accuracy_meters': -1}, {'captured_at': 'now'}, {'status': 'spoofed'}])
def test_invalid_device_fixes_fail_closed(change):
    with pytest.raises(ValueError): maps.validate_location(fix(**change))


def test_stale_and_unshared_fixes_discard_coordinates():
    assert maps.validate_location(fix(captured_at=1)) == {'status': 'unavailable'}
    assert maps.validate_location({'status': 'denied', 'latitude': 12}) == {'status': 'denied'}
    assert maps.validate_location(None) == {'status': 'not_shared'}
    assert maps.requested('How far is the Example store from me?')
    assert not maps.requested('How far along is the pull request?')
    assert not maps.requested('Where am I in the setup process?')


def test_maps_secret_never_reaches_the_engine_environment(setup, monkeypatch):
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'test-only-maps-key')
    captured = _spawn_capture(setup[1]['rift'], chat._resolve_provider(),
        bot_cfg={'allowed_tools': ['maps_route', 'task_complete']})
    assert 'GOOGLE_MAPS_API_KEY' not in captured['env']
    assert captured['env']['KYREX_READ_ONLY_REPO'] == '1'


def test_maps_link_works_without_key_and_without_origin(monkeypatch):
    monkeypatch.delenv('GOOGLE_MAPS_API_KEY', raising=False)
    result = maps.route('Test store & cafe, Example City', fix())
    assert result['status'] == 'maps_only'
    query = parse_qs(urlparse(result['maps_url']).query)
    assert query['destination'] == ['Test store & cafe, Example City']
    assert query['api'] == ['1'] and query['dir_action'] == ['navigate']
    assert 'origin' not in query and 'distance_miles' not in result
    assert 'latitude' not in json.dumps(result)
    no_fix = maps.route('Example store', {'status': 'denied'})
    assert no_fix['status'] == 'needs_origin' and 'maps_url' in no_fix


def fake_routes(monkeypatch, *, data=None, status=200):
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'test-only-maps-key')
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=status, content=b'{}', json=lambda: data or {
            'routes': [{'distanceMeters': 8047, 'duration': '720s'}],
            'geocodingResults': {'destination': {'type': ['street_address'], 'placeId': 'verified-place'}}})
    monkeypatch.setattr(maps.requests, 'post', post)
    return calls


def test_verified_route_uses_actual_fix_and_keeps_key_and_coordinates_host_only(monkeypatch):
    calls = fake_routes(monkeypatch)
    result = maps.route('120 Example Street, Testville', fix())
    assert result['status'] == 'verified' and result['distance_miles'] == 5.0 and result['duration_minutes'] == 12
    url, args = calls[0]
    assert url == 'https://routes.googleapis.com/directions/v2:computeRoutes'
    assert not args['allow_redirects'] and args['timeout'] == (3, 8)
    assert args['json']['origin']['location']['latLng']['latitude'] == fix()['latitude']
    assert args['json']['routingPreference'] == 'TRAFFIC_AWARE'
    assert args['headers']['X-Goog-Api-Key'] == 'test-only-maps-key'
    assert 'test-only-maps-key' not in json.dumps(result) and str(fix()['latitude']) not in json.dumps(result)
    assert parse_qs(urlparse(result['maps_url']).query)['destination_place_id'] == ['verified-place']
    manual = maps.route('120 Example Street', None, origin='22 Starting Street', mode='walking')
    assert calls[1][1]['json']['origin'] == {'address': '22 Starting Street'}
    assert 'routingPreference' not in calls[1][1]['json']
    assert parse_qs(urlparse(manual['maps_url']).query)['origin'] == ['22 Starting Street']


@pytest.mark.parametrize('data', [{'routes': []}, {'routes': [{'distanceMeters': -1, 'duration': 'NaNs'}]},
    {'routes': [{'distanceMeters': 1234, 'duration': '60s'}], 'geocodingResults': {'destination': {'partialMatch': True}}},
    {'routes': [{'distanceMeters': 1234, 'duration': '60s'}], 'geocodingResults': {'destination': {'type': ['locality', 'political']}}}])
def test_missing_invalid_or_area_routes_cannot_become_numeric_answers(monkeypatch, data):
    fake_routes(monkeypatch, data=data)
    result = maps.route('Example store', fix())
    assert result['status'] != 'verified' and 'distance_miles' not in result and 'maps_url' in result


def test_provider_failure_coarse_location_and_revocation_cannot_fabricate_a_route(monkeypatch):
    calls = fake_routes(monkeypatch, status=403)
    assert maps.route('Example destination', fix())['status'] == 'route_unavailable'
    assert maps.route('Example destination', fix(accuracy_meters=10000))['status'] == 'needs_origin'
    assert len(calls) == 1
    with pytest.raises(ValueError): maps.route('Example destination', fix(), authorize=lambda: False)


def test_live_owner_policy_and_turn_authority_at_host_boundary(setup, monkeypatch):
    engine = chat.EngineSession.__new__(chat.EngineSession)
    engine.maps_owner = 'alice'; engine.bot_id = 'chief'; engine.allowed_tools = {'maps_route'}
    engine._maps_request = 'How far is Example store from me?'; engine._maps_location = fix()
    frame = {'destination': 'Example store, Testville'}
    monkeypatch.delenv('GOOGLE_MAPS_API_KEY', raising=False)
    assert engine._handle_maps_route(frame)[0]
    assert not engine._handle_maps_route({**frame, 'origin': 'A remembered home town'})[0]
    assert not engine._handle_maps_route(frame, lambda: True)[0]
    engine._maps_request = 'Read my email'
    assert not engine._handle_maps_route(frame)[0]
    engine._maps_request = 'Directions to Example store'
    for rules in ({'maps:read': 'deny'}, {'maps:*': 1}, {'*': 'deny'}):
        bots.update_bot('chief', policy=rules)
        assert not maps.bot_allowed('alice', 'chief')
        assert not engine._handle_maps_route(frame)[0]
    bots.update_bot('chief', policy={'maps:read': 0})
    assert maps.bot_allowed('alice', 'chief') and not maps.bot_allowed('bob', 'chief')
    bots.set_status('chief', 'paused')
    assert not engine._handle_maps_route(frame)[0]


def test_api_validates_location_and_real_turn_only_exposes_safe_context(setup, monkeypatch):
    seen = []
    host_handler = chat.EngineSession._handle_maps_route
    class Engine(_StubSession):
        def run_turn(self, text, on_token, cancel_check=None):
            seen.append((self, self.surface_context, self._maps_location))
            assert 'maps_route' in self.allowed_tools
            assert 'town-to-town estimate' in self.surface_context
            assert str(fix()['latitude']) not in self.surface_context
            ok, result = host_handler(self, {'destination': '120 Example Street, Testville'})
            assert ok and result['status'] in {'maps_only', 'needs_origin'}
            return '[Open in Google Maps](' + result['maps_url'] + ')', None
    monkeypatch.setattr(chat, 'EngineSession', Engine)
    monkeypatch.delenv('GOOGLE_MAPS_API_KEY', raising=False)
    client = setup[4]; cid = setup[3]['conversation_id']
    invalid = client.post('/api/chat', json={'conversation_id': cid, 'message': 'How far is Example store?', 'device_location': fix(latitude=1000)})
    assert invalid.status_code == 400
    response = client.post('/api/chat', json={'conversation_id': cid, 'message': 'Compare walking and driving distance to Example store from me', 'device_location': fix()})
    assert response.status_code == 200
    assert 'maps/dir/' in response.text, response.text
    assert seen and seen[0][2]['latitude'] == fix()['latitude']
    assert seen[0][0]._maps_location is None and seen[0][0]._maps_request == ''
    again = client.post('/api/chat', json={'conversation_id': cid, 'message': 'Compare walking and driving distance to Another store from me'})
    assert 'maps/dir/' in again.text, again.text
    assert seen[1][0] is seen[0][0] and seen[1][2] == {'status': 'not_shared'}
    saved = json.dumps(chat.get_conversation('alice', cid))
    assert str(fix()['latitude']) not in saved and 'device_location' not in saved
    assert chat.get_conversation('bob', cid) is None


@pytest.mark.parametrize('question', [
    'How far away am I from 120 Example Ridge Dr Testville NC',
    'How far away is 120 Example Ridge Dr Testville NC from me?',
    'Directions to 120 Example Ridge Dr Testville NC',
])
def test_simple_route_cannot_be_answered_with_stale_model_small_talk(setup, monkeypatch, question):
    conv = setup[3]; cid = conv['conversation_id']; client = setup[4]
    chat._append_message('alice', conv, 'user', 'hi')
    chat._append_message('alice', conv, 'assistant', "Hi! You're welcome.")
    chat._append_message('alice', conv, 'user', 'How')
    chat._write('alice', conv)
    monkeypatch.setattr(chat, '_get_engine_session', lambda *a, **k: pytest.fail('A simple route must not depend on model tool selection.'))
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: pytest.fail('A model cannot substitute small talk for this route.'))
    body = {'conversation_id': cid, 'message': question, 'request_id': 'native-route'}
    first = client.post('/api/chat', json=body)
    assert 'I need your current location' in first.text and 'maps/dir/' in first.text
    assert "You're welcome" not in first.text and 'needs_origin' in first.text
    saved = chat.get_conversation('alice', cid)
    assert saved['messages'][-1]['maps_route'] == {'request': question, 'status': 'needs_origin'}
    assert client.get(f'/api/conversations/{cid}').json()['messages'][-1]['maps_route']['request'] == question
    # A repeated read of the same turn returns its saved answer, not another read.
    monkeypatch.setattr(maps, 'route', lambda *a, **k: pytest.fail('A replay must use its saved route answer.'))
    assert client.post('/api/chat', json=body).text == first.text


def test_native_route_uses_fix_then_an_explicit_manual_origin_without_guessing(setup, monkeypatch):
    client = setup[4]; cid = setup[3]['conversation_id']
    calls = fake_routes(monkeypatch)
    question = 'How far away am I from 120 Example Ridge Dr Testville NC'
    first = client.post('/api/chat', json={'conversation_id': cid, 'message': question, 'device_location': fix()})
    assert '5 miles and 12 minutes' in first.text and 'maps/dir/' in first.text
    saved = json.dumps(chat.get_conversation('alice', cid))
    assert str(fix()['latitude']) not in saved
    # A later request without a fix cannot silently reuse coordinates from the first.
    second = client.post('/api/chat', json={'conversation_id': cid, 'message': question})
    assert 'needs_origin' in second.text and len(calls) == 1
    manual = client.post('/api/chat', json={'conversation_id': cid, 'message': "I'm starting at 22 Starting Street Testville"})
    assert '5 miles and 12 minutes' in manual.text and len(calls) == 2
    assert calls[-1][1]['json']['origin'] == {'address': '22 Starting Street Testville'}
    # No other conversation is allowed to borrow this route clarification.
    other = chat.create_conversation('alice', bot_id='chief')
    assert maps.route_request("I'm starting at 22 Starting Street Testville", other['messages']) is None


def test_native_route_keeps_policy_and_cancel_boundaries(setup, monkeypatch):
    import asyncio
    from test_chat_bot_policy import _frames
    cid = setup[3]['conversation_id']
    calls = fake_routes(monkeypatch)
    bots.update_bot('chief', policy={'maps:read': 'deny', 'bot:delegate': 0})
    response = setup[4].post('/api/chat', json={'conversation_id': cid, 'message': 'Directions to Example store', 'device_location': fix()})
    assert 'Maps reads are not available' in response.text and not calls
    bots.update_bot('chief', policy={'bot:delegate': 0})
    cancel = asyncio.Event(); cancel.set()
    frames = asyncio.run(_frames(chat.stream_chat('alice', cid, 'Directions to Example store', cancel, device_location=fix())))
    assert frames[-1]['status'] == 'cancelled' and not calls


def test_route_read_revocation_during_google_request_never_publishes_a_late_result(setup, monkeypatch):
    fake_routes(monkeypatch)
    original = maps.requests.post
    def changed(*args, **kwargs):
        reply = original(*args, **kwargs)
        bots.update_bot('chief', policy={'maps:read': 'deny', 'bot:delegate': 0})
        return reply
    monkeypatch.setattr(maps.requests, 'post', changed)
    cid = setup[3]['conversation_id']
    response = setup[4].post('/api/chat', json={'conversation_id': cid, 'message': 'Directions to Example store', 'device_location': fix()})
    assert 'Maps access changed' in response.text and '5 miles' not in response.text
    assert not any(m.get('maps_route') for m in chat.get_conversation('alice', cid)['messages'])


def test_later_engine_turn_retains_native_route_context_without_retaining_a_location(setup, monkeypatch):
    from test_chat_bot_policy import _RecordingEngine
    cid = setup[3]['conversation_id']
    question = 'How far away am I from 120 Example Ridge Dr Testville NC'
    assert 'needs_origin' in setup[4].post('/api/chat', json={'conversation_id': cid, 'message': question}).text
    class Engine(_RecordingEngine):
        def run_turn(self, text, on_token, cancel_check=None):
            assert 'Earlier route information' in self.surface_context
            assert question in self.surface_context
            assert 'not current location' in self.surface_context
            assert self._maps_location is None
            return 'You asked for a route to the address you supplied.', None
    monkeypatch.setattr(chat, '_get_engine_session', Engine)
    reply = setup[4].post('/api/chat', json={'conversation_id': cid, 'message': 'What was I asking you for?'})
    assert 'You asked for a route' in reply.text, reply.text


@pytest.mark.parametrize('text', ['hi', 'How', 'How far along is the PR?',
    'How far is Example store from me and book an appointment', 'How far am I from home?',
    'Compare walking and driving distance to Example store'])
def test_native_parser_leaves_ambiguous_or_combined_work_with_existing_engine(text):
    assert maps.route_request(text) is None
