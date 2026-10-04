import copy
import json
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch
import pytest
import maps_routes as maps

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv('GOOGLE_MAPS_API_KEY', 'private-test-key')
    monkeypatch.setenv('KYREX_MAPS_ROUTE_OWNERS', 'keith')


def response():
    return {'routes': [{'distanceMeters': 32186, 'duration': '1800s', 'staticDuration': '1200s'}],
            'geocodingResults': {'origin': {'placeId': 'origin_id', 'type': ['street_address']},
                                 'destination': {'placeId': 'dest_id', 'type': ['establishment']}}}


def compute(data=None, **kwargs):
    return maps.compute_route('keith', 'Willow Spring, NC', 'Coquette, Raleigh, NC',
                              now=NOW, transport=lambda *a: data if data is not None else response(), **kwargs)


def test_traffic_duration_not_static_duration_and_miles_eta():
    r = compute()
    assert r['duration_minutes'] == 30
    assert r['distance_miles'] == 20.0
    assert r['estimated_arrival'] == '2026-10-04T12:30:00+00:00'
    assert r['source'] == 'Google Maps Routes API'
    assert 'origin_place_id=origin_id' in r['directions_url']
    assert 'private-test-key' not in json.dumps(r)


def test_future_departure_offset_is_sent_to_pinned_driving_request():
    seen = {}
    def transport(payload, key):
        seen.update(payload)
        assert key == 'private-test-key'
        return response()
    maps.compute_route('keith', 'one', 'two', '2026-10-04T09:30:00-04:00', now=NOW, transport=transport)
    assert seen['departureTime'] == '2026-10-04T13:30:00Z'
    assert seen['routingPreference'] == 'TRAFFIC_AWARE_OPTIMAL'
    assert seen['travelMode'] == 'DRIVE'
    assert seen['computeAlternativeRoutes'] is False


@pytest.mark.parametrize('departure', ['2026-10-04T07:30:00-04:00', 'tomorrow', '2026-10-04T09:30:00', True])
def test_invalid_or_past_departures_never_make_provider_call(departure):
    with pytest.raises(maps.MapsError):
        maps.compute_route('keith', 'one', 'two', departure, now=NOW,
                           transport=lambda *a: pytest.fail('provider called'))


def test_foreign_owner_and_missing_key_never_make_provider_call(monkeypatch):
    with pytest.raises(maps.MapsError, match='not enabled'):
        maps.compute_route('other', 'one', 'two', transport=lambda *a: pytest.fail('provider called'))
    monkeypatch.delenv('GOOGLE_MAPS_API_KEY')
    with pytest.raises(maps.MapsError, match='not enabled'):
        compute()


@pytest.mark.parametrize('origin', ['', None, 123, 'x' * 501, 'one\nsecret'])
def test_bad_locations_do_not_reach_provider(origin):
    with pytest.raises(maps.MapsError):
        maps.compute_route('keith', origin, 'two', transport=lambda *a: pytest.fail('provider called'))


def test_partial_geocoding_and_traffic_fallback_are_not_verified_routes():
    data = response(); data['geocodingResults']['destination']['partialMatch'] = True
    with pytest.raises(maps.MapsError) as exc: compute(data)
    assert exc.value.code == 'location_ambiguous'
    data = response(); data['fallbackInfo'] = {'routingMode': 'FALLBACK_TRAFFIC_UNAWARE'}
    with pytest.raises(maps.MapsError) as exc: compute(data)
    assert exc.value.code == 'routing_fallback'


def test_area_level_route_cannot_be_called_door_to_door():
    data = response(); data['geocodingResults']['origin']['type'] = ['locality']
    r = compute(data)
    assert r['area_based_waypoints'] == ['origin']
    assert 'not a precise door-to-door route' in r['caveat']


@pytest.mark.parametrize('duration', ['-30s', 'NaNs', '20', None])
def test_invalid_provider_duration_is_rejected(duration):
    data = response(); data['routes'][0]['duration'] = duration
    with pytest.raises(maps.MapsError) as exc: compute(data)
    assert exc.value.code == 'invalid_response'


def test_no_routes_and_missing_geocoder_evidence():
    data = response(); data['routes'] = []
    with pytest.raises(maps.MapsError) as exc: compute(data)
    assert exc.value.code == 'no_route'
    data = response(); del data['geocodingResults']
    with pytest.raises(maps.MapsError) as exc: compute(data)
    assert exc.value.code == 'location_unresolved'


def test_transport_uses_header_key_fieldmask_bounded_response_and_no_redirect():
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, size):
            assert size == maps.MAX_RESPONSE + 1
            return json.dumps(response()).encode()
    class Opener:
        def open(self, req, timeout):
            assert req.full_url == maps.ENDPOINT
            assert req.get_header('X-goog-api-key') == 'private-test-key'
            assert req.get_header('X-goog-fieldmask') == maps.FIELD_MASK
            assert timeout == maps.TIMEOUT
            assert 'private-test-key' not in req.full_url
            return Reply()
    with patch.object(maps.urllib.request, 'build_opener', return_value=Opener()) as opener:
        assert maps._request({}, 'private-test-key') == response()
        assert isinstance(opener.call_args.args[0], maps._NoRedirect)
        assert opener.call_args.args[0].redirect_request(None, None, 302, '', {}, 'https://evil.example/') is None


def test_provider_denial_does_not_echo_raw_response_or_key():
    error = urllib.error.HTTPError(maps.ENDPOINT, 403, 'private-test-key', {}, None)
    with patch.object(maps.urllib.request, 'build_opener') as opener:
        opener.return_value.open.side_effect = error
        with pytest.raises(maps.MapsError) as exc: maps._request({}, 'private-test-key')
        assert exc.value.code == 'provider_access_denied'
        assert 'private-test-key' not in str(exc.value)


def test_bot_lifecycle_owner_and_explicit_maps_restrictions():
    bot = {'owner': 'keith', 'status': 'running', 'policy': {'*': 'deny'}}
    assert maps.bot_enabled(bot)
    for change in ({'owner': 'other'}, {'status': 'stopped'}, {'policy': {'maps:route': 'deny'}},
                   {'policy': {'maps:*': 1}}, {'policy': None}):
        assert not maps.bot_enabled({**bot, **change})


def test_long_unicode_endpoint_labels_keep_bounded_link_with_verified_place_ids():
    result = maps.compute_route('keith', '城' * 400, '町' * 400, now=NOW,
                                transport=lambda *a: response())
    assert len(result['directions_url']) <= 2048
    assert 'origin_place_id=origin_id' in result['directions_url']
    assert 'destination_place_id=dest_id' in result['directions_url']
