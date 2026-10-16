"""Real intercepted Chromium: member reads cannot submit/cancel bookings."""
import copy
import html
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
                    'Access-Control-Allow-Headers': 'authorization', 'Access-Control-Allow-Methods': 'GET'}); return
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
            markup = (f'<body><header>{header}</header><button onclick="{html.escape(send,quote=True)}">My bookings</button>'
                      f'<button onclick="{html.escape(bad,quote=True)}">Book</button><script>{bad}</script></body>')
            route.fulfill(status=200, content_type='text/html', body=markup)
        context.route('**/*', fixture)
        original = driver.member_session
        monkeypatch.setattr(driver, 'member_session', lambda p, allow: original(p, allow, timeout_ms=2000))
        yield SimpleNamespace(_context=context, _page=page), state
        chrome.close()


def run(browser):
    d, state = browser
    return driver.run(d, operator.FakeProto(), SPEC, allowlist=list(schema.HOSTS))


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


def test_cannot_select_foreign_member_or_smuggle_booking_actions():
    wire = json.dumps({schema.ACTION: SPEC})
    assert host_allowlist.preflight(wire, list(schema.HOSTS))[0]
    assert operator.preflight(wire, list(schema.HOSTS))[0]
    for spec in ({**SPEC, 'member_id': MEMBER}, {**SPEC, 'submit': True}, {**SPEC, 'url': 'https://other.example'}):
        with pytest.raises(operator.SpecError): operator.parse_spec(json.dumps({schema.ACTION: spec}))
    with pytest.raises(operator.SpecError): operator.parse_spec(json.dumps({schema.ACTION: SPEC, 'actions': [{'action': 'click'}]}))
    assert not schema.native_member(NATIVE.replace(schema.BRANCH, 'b' * 24))
    assert not schema.native_member(NATIVE.replace('api.glofox.com', 'api.glofox.com.evil.example'))
