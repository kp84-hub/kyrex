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
    response = client.post('/api/chat', json={'conversation_id': cid, 'message': 'How far is Example store from me?', 'device_location': fix()})
    assert response.status_code == 200
    assert 'maps/dir/' in response.text, response.text
    assert seen and seen[0][2]['latitude'] == fix()['latitude']
    assert seen[0][0]._maps_location is None and seen[0][0]._maps_request == ''
    again = client.post('/api/chat', json={'conversation_id': cid, 'message': 'How far is Another store from me?'})
    assert 'maps/dir/' in again.text, again.text
    assert seen[1][0] is seen[0][0] and seen[1][2] == {'status': 'not_shared'}
    saved = json.dumps(chat.get_conversation('alice', cid))
    assert str(fix()['latitude']) not in saved and 'device_location' not in saved
    assert chat.get_conversation('bob', cid) is None
