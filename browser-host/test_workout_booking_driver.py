"""Real intercepted member portal. Zero real credentials, Glofox calls or bookings."""
import copy
import html
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'kyrex-cloud'), str(ROOT / 'browser-host')]
import booking_schema as common
import workout_booking_driver as driver
import workout_booking_schema as schema

DAY = (datetime.now(ZoneInfo('America/New_York')) + timedelta(days=1)).date().isoformat()
DETAILS = {'date': DAY, 'time': '08:30', 'event_id': 'c' * 24,
           'class_name': 'Group Fitness Class', 'trainer_name': 'Donna Albertone',
           'start': int(datetime.fromisoformat(DAY + 'T08:30').replace(tzinfo=ZoneInfo('America/New_York')).timestamp())}
PORTAL = 'https://app.glofox.com/portal/#/branch/' + schema.BRANCH + '/classes-list-view'
API = 'https://api.glofox.com/2.3/branches/' + schema.BRANCH + '/bookings'
BODY = {'model': 'event', 'model_id': 'c' * 24, 'user_id': 'e' * 24,
        'guest_bookings': 0, 'join_waiting_list': False}


class Proto:
    def __init__(self): self.allowed = True
    def operation(self, *args): return self.allowed


@pytest.fixture
def browser(tmp_path):
    if os.environ.get('KYREX_TEST_BROWSER') != '1': pytest.skip('Chromium is required in the booking CI workflow.')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        chrome = pw.chromium.launch(headless=True)
        context = chrome.new_context(service_workers='block'); page = context.new_page()
        state = {'posts': [], 'token': 'test-member-only', 'body': dict(BODY), 'api': API,
                 'terms': 'Included in your membership', 'member': True, 'outcome': 'BOOKED', 'auto': False}
        def fixture(route):
            req = route.request
            if req.method == 'OPTIONS':
                route.fulfill(status=204, headers={'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'authorization,content-type', 'Access-Control-Allow-Methods': 'POST'}); return
            if req.method == 'POST':
                state['posts'].append(json.loads(req.post_data))
                payload = {'success': True, 'Booking': {'_id': 'd' * 24, 'branch_id': schema.BRANCH,
                    'event_id': 'c' * 24, 'user_id': 'e' * 24, 'time_start': DETAILS['start'],
                    'confirmed': True, 'status': state['outcome']}}
                if state.get('wrong'): payload['Booking']['event_id'] = 'a' * 24
                route.fulfill(status=200, content_type='application/json', body=json.dumps(payload),
                              headers={'Access-Control-Allow-Origin': '*'}); return
            if req.url.startswith(schema.URL):
                route.fulfill(status=200, content_type='text/html', body=f'<iframe src="{PORTAL}"></iframe>'); return
            send = "fetch(" + json.dumps(state['api']) + ",{method:'POST',headers:{'Content-Type':'application/json',Authorization:" + json.dumps('Bearer ' + state['token']) + "},body:JSON.stringify(" + json.dumps(state['body']) + ")}).catch(()=>{})"
            header = 'My bookings · Logout' if state['member'] else 'Please sign in'
            markup = f'''<!doctype html><title>Level 6 Training</title><header>{header}</header>
            <article><h1>Group Fitness Class</h1><p>{DAY} · 8:30 AM · Donna Albertone</p>
            <p>{state['terms']}</p><button id="book" onclick="{html.escape(send, quote=True)}">Book</button></article>'''
            if state['auto']: markup += '<script>' + send + '</script>'
            route.fulfill(status=200, content_type='text/html', body=markup)
        context.route('**/*', fixture)
        class Browser:
            _page = page; _context = context; session_dir = tmp_path
            def navigate(self, url): page.goto(url, wait_until='domcontentloaded')
        yield Browser(), state, Proto()
        context.close(); chrome.close()


def initial():
    return {'id': 'a' * 32, 'nonce': 'b' * 64, 'stage': 'prepare', 'url': schema.URL,
            'details': copy.deepcopy(DETAILS), 'steps': []}


def review(browser):
    live, state, proto = browser; spec = initial()
    snap = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)['snapshot']
    c = next(c for c in snap['controls'] if c['label'] == 'Book')
    spec['steps'].append({'op': 'click', 'control': c})
    result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert not state['posts'], 'Even the portal native Book request must be blocked before yes.'
    plan = result['review']
    spec.update(stage='submit', steps=plan['steps'], submit=plan['submit'], request=plan['request'],
                expected=common.review_hash(result['snapshot']))
    return spec


def test_real_native_request_is_captured_without_sending_then_submitted_once(browser):
    live, state, proto = browser; spec = review(browser)
    assert 'test-member-only' not in json.dumps(spec)
    assert spec['request']['body'] == BODY and len(spec['request']['identity']) == 64
    result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert result['outcome'] == 'confirmed' and result['reference'] == 'd' * 24
    assert state['posts'] == [BODY]
    driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert len(state['posts']) == 1


@pytest.mark.parametrize('change', ['account', 'terms', 'class', 'authority'])
def test_real_change_before_yes_prevents_booking(browser, change):
    live, state, proto = browser; spec = review(browser)
    if change == 'account': state['token'] = 'different-member-session'
    elif change == 'terms': state['terms'] = '$15.00'
    elif change == 'class': state['body']['model_id'] = 'f' * 24
    else: proto.allowed = False
    try:
        result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
        assert result.get('outcome') in {'changed', 'cancelled', 'unknown'}
    except ValueError: pass
    assert not state['posts']


@pytest.mark.parametrize('change', ['waitlist', 'paid', 'guest', 'legacy'])
def test_real_unsupported_native_request_is_never_sent(browser, change):
    live, state, proto = browser
    if change == 'waitlist': state['body']['join_waiting_list'] = True
    elif change == 'paid': state['body']['price'] = 15
    elif change == 'guest': state['member'] = False
    else: state['api'] = 'https://api.glofox.com/2.0/bookings'
    spec = initial()
    result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    if change == 'guest':
        assert 'Sign in' in result['final_response']; return
    c = next(c for c in result['snapshot']['controls'] if c['label'] == 'Book')
    spec['steps'] = [{'op': 'click', 'control': c}]
    result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert not result.get('review') and not state['posts']


def test_real_auto_submission_during_page_load_is_blocked(browser):
    live, state, proto = browser; state['auto'] = True
    result = driver.run(live, proto, initial(), allowlist=['*'], root=live.session_dir)
    assert not state['posts'] and result['status'] == 'error'


@pytest.mark.parametrize('outcome', ['WAITING', 'FAILED'])
def test_real_nonconfirmed_provider_response_is_unknown_and_not_retried(browser, outcome):
    live, state, proto = browser; spec = review(browser); state['outcome'] = outcome
    result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert result['outcome'] == 'unknown' and len(state['posts']) == 1
    driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert len(state['posts']) == 1


def test_real_wrong_class_receipt_is_not_reported_as_confirmed(browser):
    live, state, proto = browser; spec = review(browser); state['wrong'] = True
    assert driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)['outcome'] == 'unknown'


def test_workout_wire_rejects_arbitrary_sites_and_generic_browser_approval():
    import browser_operator
    spec = initial()
    assert browser_operator.parse_spec(json.dumps({schema.ACTION: spec}))[0]['action'] == schema.ACTION
    spec['url'] = 'https://other.example/book'
    with pytest.raises(ValueError): schema.task(spec)
    with pytest.raises(browser_operator.SpecError):
        browser_operator.parse_spec(json.dumps({'actions': [{'action': schema.ACTION, 'spec': initial()}]}))
