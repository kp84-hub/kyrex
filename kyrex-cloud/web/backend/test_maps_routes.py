"""Offline Routes boundary, error privacy and Chat host integration regressions."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import maps_routes as maps


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv(maps.KEY_ENV, "server-only-secret")


def read(body=None, status=200, handler=None):
    def response(request):
        assert request.url == maps.ENDPOINT
        assert request.headers['X-Goog-Api-Key'] == 'server-only-secret'
        assert request.headers['X-Goog-FieldMask'] == 'routes.duration,routes.distanceMeters'
        assert json.loads(request.content) == {
            'origin': {'address': 'Willow Spring, NC'},
            'destination': {'address': 'Crabtree Valley Mall, Raleigh, NC'},
            'travelMode': 'DRIVE', 'routingPreference': 'TRAFFIC_AWARE',
            'computeAlternativeRoutes': False, 'units': 'IMPERIAL'}
        return httpx.Response(status, json=body)
    with httpx.Client(transport=httpx.MockTransport(handler or response)) as client:
        return maps.read_route('Willow Spring, NC', 'Crabtree Valley Mall, Raleigh, NC', client=client)


def test_route_request_body_result_and_no_credentials():
    result = read({'routes': [{'duration': '2100.50s', 'distanceMeters': 32186.88}]})
    assert result['status'] == 'ok'
    assert result['duration_seconds'] == 2100.5
    assert result['duration_minutes'] == 35.0
    assert result['distance_miles'] == 20.0
    assert result['traffic_aware'] and result['departure'] == 'now'
    assert 'server-only-secret' not in json.dumps(result)
    assert parse_qs(urlparse(result['maps_url']).query)['origin'] == ['Willow Spring, NC']


@pytest.mark.parametrize('status,kind', [(401,'access_denied'), (403,'access_denied'),
    (429,'quota_exceeded'), (500,'provider_error'), (302,'provider_error'), (400,'provider_error')])
def test_safe_errors_never_expose_provider_body(status, kind):
    result = read({'error': 'server-only-secret PRIVATE /address'}, status)
    assert result['error_type'] == kind
    assert 'duration_seconds' not in result
    assert 'PRIVATE' not in json.dumps(result) and 'server-only-secret' not in json.dumps(result)
    assert result['maps_url'].startswith('https://www.google.com/maps/dir/?')


@pytest.mark.parametrize('body', [{'routes':[{'duration':'oops','distanceMeters':12}]},
    {'routes':[{'duration':'100s','distanceMeters':True}]}, {'routes':[None]},
    {'routes':[{'duration':'100s','distanceMeters':-1}]}, [], {'routes':None},
    {'routes':[{'distanceMeters':12}]}])
def test_missing_or_invalid_data_is_never_a_verified_eta(body):
    assert read(body)['error_type'] == 'invalid_response'


def test_no_route_and_network_failure():
    assert read({})['error_type'] == 'no_route'
    def timeout(request):
        raise httpx.ReadTimeout('server-only-secret', request=request)
    assert read(handler=timeout)['error_type'] == 'timeout'
    def network(request):
        raise httpx.ConnectError('server-only-secret', request=request)
    assert read(handler=network)['error_type'] == 'network_error'


def test_response_bounded():
    assert read(handler=lambda req: httpx.Response(200, content=b'x'*128001))['error_type'] == 'invalid_response'


@pytest.mark.parametrize('origin', ['', None, False, 'x'*501, 'a\nb', 'a\x00b'])
def test_validate_before_network(origin):
    assert maps.read_route(origin, 'Mall')['error_type'] == 'invalid_arguments'


def test_no_key_returns_setup_and_link_without_network(monkeypatch):
    monkeypatch.delenv(maps.KEY_ENV)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: pytest.fail('must not call Google'))
    result = maps.read_route('Willow Spring, NC', 'Mall')
    assert result['error_type'] == 'not_configured'
    assert maps.KEY_ENV in result['error'] and result['maps_url']


@pytest.mark.parametrize('text', [
    'How long does it take to get to Coquette from Willow Spring?',
    'How long does it take to Crabtree Valley Mall?',
    'Im thinking of going to crabtree mall can you map it from willow Spring',
    'Give me driving directions to the mall', 'Route from Raleigh to Durham'])
def test_trip_routing(text):
    assert maps.route_request(text)


@pytest.mark.parametrize('text', ['Implement a Google Maps API feature', 'Fix driving directions in this repo',
    'Map this function', 'Review today’s workout', 'Put directions in my calendar'])
def test_other_work_is_not_stolen(text):
    assert not maps.route_request(text)


def test_host_owner_and_capability_boundary(monkeypatch):
    import chat_service
    session = object.__new__(chat_service.EngineSession)
    session.allowed_tools = {'maps_route'}
    calls = []
    monkeypatch.setattr(maps, 'read_route', lambda *args: calls.append(args) or {'status':'ok','duration_seconds':100})
    assert not session._wait_maps_route({'origin':'home','destination':'mall'})[0]
    session.maps_owner = 'alice'
    session.allowed_tools = set()
    assert not session._wait_maps_route({'origin':'home','destination':'mall'})[0]
    assert calls == []
    session.allowed_tools = {'maps_route'}
    assert session._wait_maps_route({'origin':'home','destination':'mall'})[1]['duration_seconds'] == 100
    assert calls == [('home','mall')]


def test_cancel_remains_responsive(monkeypatch):
    import chat_service
    import threading
    session = object.__new__(chat_service.EngineSession)
    session.allowed_tools = {'maps_route'}
    session.maps_owner = 'alice'
    finish = threading.Event()
    monkeypatch.setattr(maps, 'read_route', lambda *args: finish.wait(1))
    try:
        assert session._wait_maps_route({}, lambda: True)[1]['error'] == 'Google Routes request cancelled.'
    finally:
        finish.set()


def test_key_never_enters_model_subprocess(tmp_path, monkeypatch):
    import chat_service
    captured = []
    monkeypatch.setattr(chat_service.subprocess, 'Popen', lambda *a, **k: captured.append(k['env']) or SimpleNamespace())
    monkeypatch.setattr(chat_service.threading.Thread, 'start', lambda *a: None)
    monkeypatch.setattr(chat_service.EngineSession, '_wait_handshake', lambda *a: None)
    chat_service.EngineSession(tmp_path, {'provider':'openai','model':'test','api_key':'model-key','base_url':''})
    assert maps.KEY_ENV not in captured[0]
    assert 'maps_route' in captured[0]['KYREX_ALLOWED_TOOLS']


def test_coordinator_gets_direct_route_guidance(monkeypatch):
    import chat_service
    monkeypatch.setattr(chat_service.delegation, 'visible_targets', lambda *a, **k: [])
    prompt = chat_service.build_coordinator_context('alice', {'id':'chief'})
    assert maps.GUIDANCE in prompt


def test_confirm_dispatch_sends_the_route_result_to_engine(monkeypatch):
    import chat_service
    import queue
    import threading
    session = object.__new__(chat_service.EngineSession)
    session._closed = False
    session._turn_lock = threading.Lock()
    session._stderr_lock = threading.Lock()
    session.stderr_tail = []
    session.surface_context = None
    session.allowed_tools = {'maps_route'}
    session.maps_owner = 'alice'
    session._frames = queue.Queue()
    session._frames.put({'type':'confirm_request','id':'route-1','value':'maps_route',
                         'origin':'Willow Spring, NC','destination':'Mall'})
    session._frames.put({'type':'chat_done','content':'About 35 minutes.'})
    session._frames.put({'type':'phase','value':'IDLE'})
    output = []
    session._send = output.append
    monkeypatch.setattr(maps, 'read_route', lambda *a: {'status':'ok','duration_seconds':2100})
    assert session.run_turn('Map it', lambda token: None) == ('About 35 minutes.', None)
    assert output[1] == {'type':'confirm_response','id':'route-1','approved':True,
                         'result':{'status':'ok','duration_seconds':2100}}


def test_jev_does_not_force_travel_to_a_browser_peer(monkeypatch):
    import asyncio
    import jev_stream_router as router
    observed = []
    async def original(*args, **kwargs):
        observed.append(router._bot_target_hint.get())
        yield {'status':'complete'}
    chat = SimpleNamespace(stream_chat=original,
        get_conversation=lambda *a: {'bot_id':'chief'},
        resolve_bot_for_user=lambda *a: {'id':'chief','owner':'alice','policy':{}},
        serve=SimpleNamespace(DEVELOPER_PRESET={'fs:write':1},
            is_browser_bot_policy=lambda p: False, coordinator_granted=lambda b: True))
    dev = SimpleNamespace(is_writable_bot_policy=lambda p: False,
        browser_route_ready=lambda b: False, gmail_route_ready=lambda b: False,
        email_calendar_route_ready=lambda b: False)
    monkeypatch.setattr(router,'_installed',False)
    monkeypatch.setattr(router.jev_routing,'decide_bot_target',
                        lambda *a, **k: pytest.fail('trip must not be forced onto a peer'))
    router.install(chat, dev)
    async def collect():
        return [f async for f in chat.stream_chat('alice','c1','Can you map it from Willow Spring?')]
    assert asyncio.run(collect()) == [{'status':'complete'}]
    assert observed == [None]
