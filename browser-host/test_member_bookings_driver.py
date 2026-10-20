"""Real intercepted Chromium: member reads cannot submit/cancel bookings."""
import copy
import html
import hashlib
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

import browser_operator as operator
import host_allowlist
import member_bookings_driver as driver
import member_bookings_schema as schema

DAY = (datetime.now(schema.TZ) + timedelta(days=2)).date()
SPEC = {'url': schema.URL, 'start': DAY.isoformat(), 'end': (DAY + timedelta(days=7)).isoformat()}
START = int(datetime.combine(DAY, datetime.min.time(), schema.TZ).replace(hour=8, minute=30).timestamp())
MEMBER = 'e' * 24
PORTAL = 'https://app.glofox.com/portal/#/branch/' + schema.BRANCH + '/classes-list-view'
NATIVE = 'https://api.glofox.com/2.0/bookings?user_id=' + MEMBER + '&branchId=' + schema.BRANCH
RAW = {'_id': 'a' * 24, 'branch_id': schema.BRANCH, 'user_id': MEMBER, 'status': 'BOOKED',
       'model': 'event', 'model_id': 'c' * 24, 'model_name': 'Group Fitness Class',
       'time_start': START, 'duration': 45}


@pytest.fixture
def browser(monkeypatch):
    if os.environ.get('KYREX_TEST_BROWSER') != '1': pytest.skip('The booking CI workflow runs intercepted Chromium.')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        chrome = pw.chromium.launch(headless=True, executable_path=os.environ.get('KYREX_TEST_BROWSER_EXECUTABLE'),
                                   args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = chrome.new_context(service_workers='block'); page = context.new_page()
        state = {'rows': [dict(RAW)], 'requests': [], 'signed_in': True, 'native': NATIVE}
        def fixture(route):
            req = route.request; state['requests'].append((req.method, req.url))
            if req.method == 'OPTIONS':
                route.fulfill(status=204, headers={'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'authorization,content-type', 'Access-Control-Allow-Methods': 'GET,POST'}); return
            if req.url == schema.REFRESH_URL:
                if state.get('refresh_redirect'):
                    route.fulfill(status=307, headers={'Location': 'https://api.glofox.com/2.0/bookings',
                        'Access-Control-Allow-Origin': '*'}); return
                route.fulfill(status=200, content_type='application/json', body='{"ok":true}',
                              headers={'Access-Control-Allow-Origin': '*'}); return
            if '/2.0/bookings' in req.url:
                query = parse_qs(urlsplit(req.url).query)
                number = int(query.get('page', ['1'])[0])
                rows = state.get('pages', {}).get(number, state['rows'])
                payload = {'object': 'list', 'page': number, 'has_more': number < len(state.get('pages', {})),
                           'total_count': sum(len(p) for p in state['pages'].values()) if state.get('pages') else len(rows),
                           'data': rows}
                if state.get('incomplete'): payload['total_count'] += 1
                if state.get('redirect'):
                    route.fulfill(status=302, headers={'Location': 'https://other.example/secret'}); return
                route.fulfill(status=200, content_type='application/json', body=json.dumps(payload),
                              headers={'Access-Control-Allow-Origin': '*'}); return
            if req.url.startswith(schema.URL):
                route.fulfill(status=200, content_type='text/html', body=f'<iframe src="{PORTAL}"></iframe>'); return
            send = 'fetch(' + json.dumps(state['native']) + ',{headers:{Authorization:"Bearer fixture-member-only"}}).catch(()=>{})'
            bad = "fetch('https://api.glofox.com/2.0/bookings',{method:'POST',body:'must-not-book'}).catch(()=>{});"
            bad += "fetch('https://api.glofox.com/booking/abc/user/def/cancel',{method:'POST'}).catch(()=>{});"
            bad += "fetch('https://api.glofox.com/2.0/delete').catch(()=>{});"
            header = 'Profile · Log out' if state['signed_in'] else 'Login / Register'
            refresh = ''
            if state.get('refresh'):
                header = 'Login / Register'
                refresh = ('fetch(' + json.dumps(schema.REFRESH_URL)
                    + ',{method:"POST",headers:{"Content-Type":"application/json"},'
                    + 'body:JSON.stringify({refresh_token:"fixture-refresh-only"})})'
                    + '.then(r=>{if(r.ok)document.querySelector("header").textContent="Profile · Log out"})'
                    + '.catch(()=>{});')
            markup = (f'<body><header>{header}</header><button onclick="{html.escape(send,quote=True)}">My bookings</button>'
                      f'<button onclick="{html.escape(bad,quote=True)}">Book</button><script>{bad}{refresh}</script></body>')
            route.fulfill(status=200, content_type='text/html', body=markup)
        context.route('**/*', fixture)
        original = driver.member_session
        monkeypatch.setattr(driver, 'member_session', lambda p, allow: original(p, allow, timeout_ms=2000))
        yield SimpleNamespace(_context=context, _page=page), state
        chrome.close()


def run(browser, allowlist=None):
    d, state = browser
    return driver.run(d, operator.FakeProto(), SPEC, allowlist=allowlist or list(schema.READER_HOSTS))


def test_live_native_read_blocks_every_write_and_preserves_member_secret(browser):
    result = run(browser)
    assert result['complete'] is True and len(result['bookings']) == 1
    assert result['bookings'][0]['end'] - result['bookings'][0]['start'] == 2700
    assert 'fixture-member-only' not in json.dumps(result)
    assert MEMBER not in json.dumps(result)
    assert all(method in {'GET', 'OPTIONS'} for method, _ in browser[1]['requests'])
    assert not any('/cancel' in url or '/delete' in url for _, url in browser[1]['requests'])
    reads = [url for method, url in browser[1]['requests'] if method == 'GET' and 'time_start=' in url]
    assert reads and all(parse_qs(urlsplit(url).query)['user_id'] == [MEMBER] for url in reads)


def test_pages_are_complete_and_cancelled_waitlisted_classes_are_not_booked(browser):
    browser[1]['pages'] = {1: [dict(RAW)], 2: [{**RAW, '_id': 'b' * 24, 'status': 'CANCELED'},
                                              {**RAW, '_id': 'd' * 24, 'status': 'WAITING'}]}
    assert len(run(browser)['bookings']) == 1


@pytest.mark.parametrize('case', ['member', 'studio', 'incomplete', 'redirect', 'status', 'duration', 'duplicate'])
def test_mismatched_or_incomplete_source_never_becomes_verified_bookings(browser, case):
    state = browser[1]
    if case == 'member': state['rows'][0]['user_id'] = 'b' * 24
    elif case == 'studio': state['rows'][0]['branch_id'] = 'b' * 24
    elif case == 'incomplete': state['incomplete'] = True
    elif case == 'redirect': state['redirect'] = True
    elif case == 'status': state['rows'][0]['status'] = 'mystery'
    elif case == 'duration': state['rows'][0]['duration'] = None
    else: state['pages'] = {1: [dict(RAW)], 2: [dict(RAW)]}
    with pytest.raises(ValueError): run(browser)


def test_logged_out_profile_does_not_return_empty_success(browser):
    browser[1]['signed_in'] = False
    with pytest.raises(ValueError, match='Sign in'): run(browser)


def test_genuine_empty_list_is_verified(browser):
    browser[1]['rows'] = []
    assert run(browser)['bookings'] == []


def reviewed_request():
    return {'url': 'https://api.glofox.com/2.3/branches/' + schema.BRANCH + '/bookings',
            'method': 'POST', 'body': {'model': 'event', 'model_id': 'c' * 24,
                'user_id': MEMBER, 'guest_bookings': 0, 'join_waiting_list': False},
            'identity': hashlib.sha256(b'Bearer fixture-member-only').hexdigest()}


@pytest.mark.parametrize('mismatch', ['credential', 'member'])
def test_reconciliation_binds_native_read_to_reviewed_member(browser, mismatch):
    request = reviewed_request()
    if mismatch == 'credential': request['identity'] = '0' * 64
    else: request['body']['user_id'] = 'b' * 24
    with pytest.raises(ValueError, match='account changed'):
        driver.run(browser[0], operator.FakeProto(), SPEC,
                   allowlist=list(schema.READER_HOSTS), expected_request=request)
    assert not any('/2.0/bookings?' in url for _, url in browser[1]['requests'])


def test_reconciliation_accepts_the_same_booked_row_without_optional_confirmation(browser):
    d, state = browser
    assert len(run(browser)['bookings']) == 1, 'Ordinary member-reader compatibility remains unchanged.'
    result = driver.run(d, operator.FakeProto(), SPEC, allowlist=list(schema.READER_HOSTS),
                        expected_request=reviewed_request())
    assert result['complete'] and len(result['bookings']) == 1


@pytest.mark.parametrize('flags,expected', [
    ({}, True), ({'confirmed': True}, True), ({'is_from_waiting_list': False}, True),
    ({'confirmed': False}, False), ({'confirmed': None}, False),
    ({'confirmed': 'true'}, False), ({'confirmed': 1}, False),
    ({'is_from_waiting_list': True}, False), ({'is_from_waiting_list': None}, False),
    ({'is_from_waiting_list': 'false'}, False),
    ({'status': 'WAITING'}, False), ({'status': 'CANCELLED'}, False),
])
def test_booking_status_is_evidence_but_explicit_negative_or_malformed_flags_are_not(flags, expected):
    assert driver.confirmed_booking({**RAW, **flags}) is expected


def test_expired_saved_session_can_refresh_without_permitting_booking_writes(browser):
    browser[1]['refresh'] = True
    result = run(browser)
    assert len(result['bookings']) == 1
    posts = [url for method, url in browser[1]['requests'] if method == 'POST']
    assert posts == [schema.REFRESH_URL]
    assert 'fixture-refresh-only' not in json.dumps(result)


def test_session_refresh_does_not_override_owner_host_permissions(browser):
    browser[1]['refresh'] = True
    with pytest.raises(schema.ReadFailure) as error:
        run(browser, allowlist=list(schema.HOSTS))
    assert error.value.code == 'refresh_blocked'
    assert not any(method == 'POST' for method, _ in browser[1]['requests'])


def test_native_refresh_cannot_redirect_its_body_into_a_booking(browser):
    browser[1].update(refresh=True, refresh_redirect=True)
    with pytest.raises(schema.ReadFailure):
        run(browser)
    assert [url for method, url in browser[1]['requests'] if method == 'POST'] == [schema.REFRESH_URL]


@pytest.mark.parametrize('state', ['signed_out', 'session_unverified', 'unsupported_portal'])
def test_session_failure_keeps_the_actual_state_instead_of_always_claiming_logout(browser, monkeypatch, state):
    monkeypatch.setattr(driver, 'member_session', lambda *args: state)
    with pytest.raises(schema.ReadFailure) as error:
        run(browser)
    assert error.value.code == state


def refresh_request(**overrides):
    return SimpleNamespace(**{
        'url': schema.REFRESH_URL, 'method': 'POST', 'redirected_from': None,
        'frame': SimpleNamespace(url=PORTAL),
        'post_data': json.dumps({'refresh_token': 'fixture-refresh-only'}), **overrides})


def test_only_the_native_refresh_endpoint_and_body_are_accepted():
    assert driver.session_refresh(refresh_request())


@pytest.mark.parametrize('overrides', [
    {'url': schema.REFRESH_URL + '?action=book'},
    {'url': 'https://auth.glofox.com.evil.example/refresh'},
    {'url': 'https://auth.glofox.com/login'},
    {'url': 'https://api.glofox.com/2.0/bookings'},
    {'method': 'GET'}, {'method': 'DELETE'}, {'redirected_from': object()},
    {'frame': SimpleNamespace(url='https://other.example')},
    {'frame': SimpleNamespace(url=PORTAL.replace(schema.BRANCH, 'b' * 24))},
    {'post_data': 'not json'}, {'post_data': 'null'},
    {'post_data': json.dumps({'refresh_token': 'fixture-refresh-only', 'book': True})},
    {'post_data': json.dumps({'refresh_token': ''})},
    {'post_data': json.dumps({'refresh_token': 'x' * 8193})},
    {'post_data': json.dumps({'refresh_token': 1})},
    {'post_data': json.dumps({'refresh_token': 'bad\nvalue'})},
])
def test_refresh_cannot_become_a_login_arbitrary_post_or_foreign_account_request(overrides):
    assert not driver.session_refresh(refresh_request(**overrides))


def test_failure_details_never_export_provider_or_exception_text():
    private = 'private-token-or-member-value'
    assert schema.failure(ValueError(private)) == {'code': 'unavailable'}
    assert private not in schema.failure_message({'code': private, 'message': private})
    assert schema.failure_message({'code': []}) == schema.FAILURES['unavailable']
    for code in schema.FAILURES:
        assert schema.failure(schema.ReadFailure(code)) == {'code': code}


@pytest.mark.parametrize('error,code', [
    (schema.ReadFailure('unsupported_portal'), 'unsupported_portal'),
    (schema.ReadFailure('session_unverified'), 'session_unverified'),
    (ValueError('private-token-or-member-value'), 'unavailable'),
])
def test_operator_keeps_safe_reader_failure_details(tmp_path, monkeypatch, error, code):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(driver, 'run', fail)
    fake = SimpleNamespace(open=lambda: None, close=lambda: None)
    result = operator.run_actions([{'action': schema.ACTION, 'spec': SPEC}], fake,
        root=tmp_path, allowlist=list(schema.READER_HOSTS), proto=operator.FakeProto())
    assert result['member_bookings_failure'] == {'code': code}
    assert 'private-token-or-member-value' not in json.dumps(result)


def test_cannot_select_foreign_member_or_smuggle_booking_actions():
    wire = json.dumps({schema.ACTION: SPEC})
    assert host_allowlist.preflight(wire, list(schema.HOSTS))[0]
    assert operator.preflight(wire, list(schema.HOSTS))[0]
    for spec in ({**SPEC, 'member_id': MEMBER}, {**SPEC, 'submit': True}, {**SPEC, 'url': 'https://other.example'}):
        with pytest.raises(operator.SpecError): operator.parse_spec(json.dumps({schema.ACTION: spec}))
    with pytest.raises(operator.SpecError): operator.parse_spec(json.dumps({schema.ACTION: SPEC, 'actions': [{'action': 'click'}]}))
    assert not schema.native_member(NATIVE.replace(schema.BRANCH, 'b' * 24))
    assert not schema.native_member(NATIVE.replace('api.glofox.com', 'api.glofox.com.evil.example'))
