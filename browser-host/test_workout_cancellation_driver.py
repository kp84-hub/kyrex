"""Intercepted Chromium only: no real reservation or account is ever contacted."""
import html
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import browser_operator
import member_bookings_schema as member
import workout_booking_schema as workout
import workout_booking_driver
import workout_cancellation_schema as schema

DAY = (datetime.now(member.TZ) + timedelta(days=2)).date()
START = int(datetime.combine(DAY, datetime.min.time(), member.TZ).replace(hour=8, minute=30).timestamp())
WINDOW = {'url': member.URL, 'start': DAY.isoformat(), 'end': (DAY + timedelta(days=1)).isoformat()}
MEMBER = 'e' * 24
PORTAL = 'https://app.glofox.com/portal/#/branch/' + member.BRANCH + '/classes-list-view'
ROW = {'reference': 'a' * 24, 'event_id': 'c' * 24, 'title': 'Group Fitness Class', 'start': START, 'end': START + 2700}
TERMS = 'Are you sure you want to cancel your booking?'
IDENTITY = member.session_identity('Bearer cancellation-fixture-only', MEMBER)


@pytest.fixture
def cancellation_browser(tmp_path, monkeypatch):
    if os.environ.get('KYREX_TEST_BROWSER') != '1': pytest.skip('Booking CI runs the real intercepted Chromium path.')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        chrome = pw.chromium.launch(headless=True, executable_path=os.environ.get('KYREX_TEST_BROWSER_EXECUTABLE'),
            args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = chrome.new_context(service_workers='block'); page = context.new_page()
        state = {'deletes': [], 'other_writes': [], 'unsafe_gets': [], 'terms': TERMS, 'row': dict(ROW), 'token': 'Bearer cancellation-fixture-only'}
        def fixture(route):
            req = route.request
            if req.method == 'OPTIONS':
                route.fulfill(status=204, headers={'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'authorization,content-type', 'Access-Control-Allow-Methods': 'GET,DELETE,POST'}); return
            if req.method == 'DELETE':
                state['deletes'].append(req.url)
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': not state.get('failed_receipt')}),
                    headers={'Access-Control-Allow-Origin': '*'}); return
            if req.method not in {'GET', 'HEAD'}:
                state['other_writes'].append(req.url); route.fulfill(status=400); return
            if '/delete' in req.url or 'action=cancel' in req.url:
                state['unsafe_gets'].append(req.url); route.fulfill(status=200); return
            if '/2.0/bookings?' in req.url:
                r = state['row']; raw = {'_id': r['reference'], 'event_id': r['event_id'], 'model_name': r['title'],
                    'branch_id': member.BRANCH, 'user_id': MEMBER, 'model': 'events', 'status': 'BOOKED',
                    'time_start': r['start'], 'duration': 45}
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'object': 'list', 'page': 1,
                    'has_more': False, 'total_count': 1, 'data': [raw]}), headers={'Access-Control-Allow-Origin': '*'}); return
            if req.url.startswith(member.URL):
                route.fulfill(status=200, content_type='text/html', body=f'<iframe src="{PORTAL}"></iframe>'); return
            native = 'https://api.glofox.com/2.0/bookings?user_id=' + MEMBER + '&branchId=' + member.BRANCH
            read = 'fetch(' + json.dumps(native) + ',{headers:{Authorization:' + json.dumps(state['token']) + '}}).catch(()=>{})'
            target = state.get('delete_ref', ROW['reference'])
            send = 'fetch(' + json.dumps('https://api.glofox.com/2.0/bookings/' + target) + ',{method:' + json.dumps(state.get('method', 'DELETE')) + ',headers:{Authorization:' + json.dumps(state['token']) + '}}).catch(()=>{})'
            send += ";fetch('https://api.glofox.com/2.0/delete').catch(()=>{});fetch('https://api.glofox.com/2.0/bookings?action=cancel').catch(()=>{});"
            markup = f'''<header>Profile · Log out</header><button onclick="{html.escape(read, quote=True)}">My bookings</button>
              <article><h2>Group Fitness Class</h2><button id="profile-booking-{ROW['reference']}-cancel"
              onclick="document.querySelector('#dialog').hidden=false">Cancel</button></article>
              <div id="dialog" hidden><section role="dialog"><h2>Confirm booking cancellation</h2><p>{html.escape(state['terms'])}</p>
              <button id="general-dialog-confirm-button" onclick="{html.escape(send, quote=True)}">Cancel booking</button></section></div>'''
            if state.get('initially_hidden'):
                markup = markup.replace('<article>', '<div id="profile-modal" hidden><article>').replace('</article>', '</article></div>')
                markup += '<button id="mobile-header-profile-button" onclick="document.querySelector(\'#profile-modal\').hidden=false" aria-label="Profile">Profile</button>'
                markup += '<script>' + read + '</script>'
            route.fulfill(status=200, content_type='text/html', body=markup)
        context.route('**/*', fixture)
        driver = SimpleNamespace(_context=context, _page=page, session_dir=tmp_path)
        def reopen():
            fresh = chrome.new_context(service_workers='block')
            fresh.route('**/*', fixture)
            driver._context = fresh; driver._page = fresh.new_page()
        state['reopen'] = reopen
        yield driver, state, browser_operator.FakeProto()
        chrome.close()


def initial():
    return {'id': 'a' * 32, 'nonce': 'b' * 64, 'operation': 'cancel', 'stage': 'prepare',
        'url': member.URL, 'steps': [], 'details': dict(ROW), 'window': dict(WINDOW), 'identity': IDENTITY}


def prepare(browser):
    driver, state, proto = browser
    value = initial()
    result = workout_booking_driver.run(driver, proto, value, allowlist=['*'], root=driver.session_dir)
    assert not state['deletes'] and not state['other_writes']
    value.update(stage='submit', **result['review'])
    state['reopen']()  # Each enrolled Host dispatch uses a fresh managed context.
    return value


def test_native_cancellation_is_intercepted_until_review_and_sent_once(cancellation_browser):
    driver, state, proto = cancellation_browser
    spec = prepare(cancellation_browser)
    assert 'cancellation-fixture-only' not in json.dumps(spec)
    assert MEMBER not in json.dumps(spec)
    assert schema.task(spec) == spec
    result = workout_booking_driver.run(driver, proto, spec, allowlist=['*'], root=driver.session_dir)
    assert result['outcome'] == 'confirmed' and result['reference'] == ROW['reference']
    assert state['deletes'] == ['https://api.glofox.com/2.0/bookings/' + ROW['reference']]
    state['reopen']()
    result = workout_booking_driver.run(driver, proto, spec, allowlist=['*'], root=driver.session_dir)
    assert result['outcome'] == 'unknown' and len(state['deletes']) == 1
    driver._page.frames[1].evaluate("fetch('https://api.glofox.com/2.0/bookings',{method:'POST',body:'not-allowed'}).catch(()=>{})")
    driver._page.wait_for_timeout(200)
    assert not state['other_writes']
    assert not state['unsafe_gets']


@pytest.mark.parametrize('change', ['account', 'reservation', 'fee', 'other_target', 'book_instead'])
def test_changed_cancellation_can_never_cancel_or_create_another_booking(cancellation_browser, change):
    driver, state, proto = cancellation_browser
    spec = prepare(cancellation_browser)
    if change == 'account': state['token'] = 'Bearer another-fixture-member'
    elif change == 'reservation': state['row']['reference'] = 'd' * 24
    elif change == 'fee': state['terms'] = 'Cancelling now will result in a $15 late cancellation fee. Would you like to continue?'
    elif change == 'other_target': state['delete_ref'] = 'd' * 24
    else: state['method'] = 'POST'
    try:
        result = workout_booking_driver.run(driver, proto, spec, allowlist=['*'], root=driver.session_dir)
        assert result['outcome'] != 'confirmed'
    except ValueError:
        pass
    assert not state['deletes'] and not state['other_writes']


def test_uncertain_delete_is_not_reported_as_cancelled_or_retried(cancellation_browser):
    driver, state, proto = cancellation_browser
    spec = prepare(cancellation_browser); state['failed_receipt'] = True
    result = workout_booking_driver.run(driver, proto, spec, allowlist=['*'], root=driver.session_dir)
    assert result['outcome'] == 'unknown' and len(state['deletes']) == 1
    state['reopen']()
    workout_booking_driver.run(driver, proto, spec, allowlist=['*'], root=driver.session_dir)
    assert len(state['deletes']) == 1


def test_prefetched_booking_list_still_opens_the_actual_profile_control(cancellation_browser):
    cancellation_browser[1]['initially_hidden'] = True
    prepare(cancellation_browser)


@pytest.mark.parametrize('terms', [TERMS,
    'Are you sure you want to cancel this booking? No fees will be charged if you cancel now.',
    'You can cancel this booking now without a fee. If you don’t cancel and don’t attend, a $15 no-show fee applies. Free classes are exempt.'])
def test_supported_no_fee_portal_confirmations(cancellation_browser, terms):
    cancellation_browser[1]['terms'] = terms
    prepare(cancellation_browser)
