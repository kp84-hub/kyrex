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
            details = state.get('details', DETAILS)
            if req.method == 'OPTIONS':
                route.fulfill(status=204, headers={'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'authorization,content-type', 'Access-Control-Allow-Methods': 'POST'}); return
            if req.method == 'POST':
                state['posts'].append(json.loads(req.post_data))
                if state.get('close_after_post'):
                    context.close(); return
                payload = {'success': True, 'Booking': {'_id': 'd' * 24, 'branch_id': schema.BRANCH,
                    'event_id': 'c' * 24, 'user_id': 'e' * 24, 'time_start': details['start'],
                    'confirmed': True, 'status': state['outcome']}}
                if state.get('wrong'): payload['Booking']['event_id'] = 'a' * 24
                route.fulfill(status=200, content_type='application/json', body=json.dumps(payload),
                              headers={'Access-Control-Allow-Origin': '*'}); return
            if req.url.startswith(schema.URL):
                route.fulfill(status=200, content_type='text/html', body=f'<iframe src="{PORTAL}"></iframe>'); return
            send = "fetch(" + json.dumps(state['api']) + ",{method:'POST',headers:{'Content-Type':'application/json',Authorization:" + json.dumps('Bearer ' + state['token']) + "},body:JSON.stringify(" + json.dumps(state['body']) + ")}).catch(()=>{})"
            header = 'My bookings · Logout' if state['member'] else 'Please sign in'
            markup = f'''<!doctype html><title>Level 6 Training</title><header>{header}</header>
            <article><h1>Group Fitness Class</h1><p>{details['date']} · {html.escape(state.get('clock_label', '8:30 AM'))} · Donna Albertone</p>
            <p>{state['terms']}</p><button id="book" {'disabled' if state.get('disabled_book') else ''} onclick="{html.escape(send, quote=True)}">{html.escape(state.get('book_label', 'Book'))}</button></article>'''
            if state.get('nested_book_rows'):
                def nested_button(ident, event):
                    native = send.replace('"' + 'c' * 24 + '"', '"' + event + '"')
                    disabled = ' disabled' if state.get('disabled_parent') and ident == 'actual' else ''
                    return f'<md-button role="button" ng-click="bookClass()"{disabled} onclick="{html.escape(native, quote=True)}"><button id="{ident}">Book</button></md-button>'
                markup = f'''<!doctype html><header>{header}</header><article><div>
                  <h1>Group Fitness Class</h1><p>5:30 AM</p>{nested_button('early', 'a' * 24)}
                  <section><h2>Group Fitness Class</h2><p>6:30 AM</p>{nested_button('middle', 'b' * 24)}
                  <div><h3>Group Fitness Class</h3><p>8:30 AM</p>{nested_button('actual', 'c' * 24)}</div>
                  </section></div></article>'''
                if state.get('two_matching_rows'):
                    markup = f'''<!doctype html><header>{header}</header><article>
                      <div><h1>Group Fitness Class</h1><p>8:30 AM</p>{nested_button('actual', 'c' * 24)}</div>
                      <div><h1>Group Fitness Class</h1><p>8:30 AM</p>{nested_button('other', 'a' * 24)}</div>
                      </article>'''
            if state.get('confirm_dialog'):
                mode = state['confirm_dialog']
                opening = "document.body.dataset.opened=String(Number(document.body.dataset.opened||0)+1);"
                if mode != 'absent': opening += "document.querySelector('#confirmation').hidden=false;"
                markup = markup.replace('onclick="' + html.escape(send, quote=True) + '"',
                                        'onclick="' + html.escape(opening, quote=True) + '"')
                role = '' if mode == 'unmarked' else ' role="dialog" aria-modal="true"'
                clock = '9:30 AM' if mode == 'wrong_time' else '8:30 AM'
                if mode == 'no_time': clock = ''
                dialog = f'''<section{role}><h2>Group Fitness Class</h2><p>{clock}</p>
                  <button id="confirm" onclick="{html.escape(send, quote=True)}">Book</button></section>'''
                if mode == 'nested': dialog = '<div role="dialog">' + dialog + '</div>'
                if mode == 'multiple': dialog += dialog.replace('id="confirm"', 'id="confirm-other"')
                if mode != 'absent': markup += '<div id="confirmation" hidden>' + dialog + '</div>'
            if state.get('delayed_book'):
                markup += '''<script>document.querySelector('#book').hidden=true;
                  setTimeout(()=>document.querySelector('#book').hidden=false,2600);</script>'''
            if state.get('delayed_member'):
                markup = markup.replace(header, 'Login / Register') + '''<script>
                  setTimeout(() => document.querySelector('header').innerText='Profile · Log out', 1500);
                  </script>'''
            if state['auto']: markup += '<script>' + send + '</script>'
            if state.get('calendar'):
                markup = markup.replace('<article>', '<article hidden>')
                markup += '''<style>[ng-click]{min-height:20px;min-width:20px}</style>
                <section id="calendar"><h2>October 2026</h2>
                  <div id="previous-week" ng-click="previousWeek()" onclick="week(false)"><i class="arrow-left"></i></div>
                  <ul id="days"></ul>
                  <div id="next-week" ng-click="move(1)" onclick="week(true)"><img src="https://app.glofox.com/next-arrow.svg"></div>
                  <div id="ambiguous" ng-click="menu()"><i class="arrow"></i></div>
                  <div id="unsafe-arrow" ng-click="cancelBooking()"><i class="arrow-right"></i></div>
                  <li id="invalid-day" ng-click="pickDay(day)">MON 32</li>
                  <div id="disabled" class="disabled" ng-click="nextWeek()"><i class="arrow-right"></i></div>
                </section>
                <aside><div id="family-next" ng-click="nextFamily()"><i class="arrow-right"></i></div></aside>
                <script>
                  function pickDay(el) {
                    document.querySelector('article').hidden=el.innerText.replace(/\\s+/g,' ').trim()!=='MON 19';
                  }
                  function week(next) {
                    const days=next?['MON\\n19','TUE\\n20','WED\\n21']:['TUE\\n13','WED\\n14','THU\\n15'];
                    document.querySelector('#days').innerHTML=days.map(label =>
                      '<li ng-click="pickDay(day)" onclick="pickDay(this)">'+label+'</li>').join('');
                  }
                  week(''' + ('true' if state.get('visible_target') else 'false') + ''');
                </script>'''
                if state.get('tamper_day'):
                    markup += "<script>document.querySelector('#days li').innerText='TUE\\n20';</script>"
                if state.get('calendar_roles'):
                    markup = markup.replace('id="next-week" ng-click="move(1)"', 'id="next-week" role="button"')
                    markup = markup.replace('<li ng-click="pickDay(day)"', '<li role="button"')
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


def calendar_spec(state, *, visible_target):
    spec = initial()
    spec['details'].update(date='2026-10-19', time='08:30',
        start=int(datetime(2026, 10, 19, 8, 30, tzinfo=ZoneInfo('America/New_York')).timestamp()))
    state.update(calendar=True, visible_target=visible_target, details=spec['details'])
    return spec


@pytest.mark.parametrize('visible_target, calendar_roles', [(True, False), (False, False), (False, True)])
def test_calendar_tiles_and_icon_arrows_replay_into_exact_approved_booking(browser, visible_target, calendar_roles):
    live, state, proto = browser
    spec = calendar_spec(state, visible_target=visible_target)
    state['calendar_roles'] = calendar_roles
    result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    snap = result['snapshot']
    labels = [c['label'] for c in snap['controls']]
    assert 'Next calendar week' in labels and 'Previous calendar week' in labels
    assert not any(c['selector'] in {'css=#ambiguous', 'css=#family-next', 'css=#unsafe-arrow', 'css=#invalid-day'}
                   for c in snap['controls'])
    disabled = next(c for c in snap['controls'] if c['selector'] == 'css=#disabled')
    assert disabled['disabled']
    # The generic booking adapter does not acquire Angular click targets.
    generic = driver.base.snapshot(live, lambda url: True)
    if not calendar_roles:
        assert not any(c['tag'] in {'li', 'div'} for c in generic['controls'])
    if not visible_target:
        arrow = next(c for c in snap['controls'] if c['selector'] == 'css=#next-week')
        spec['steps'].append({'op': 'click', 'control': arrow})
        snap = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)['snapshot']
    day = next(c for c in snap['controls'] if c['label'] == 'MON 19')
    assert day['tag'] == 'li'
    spec['steps'].append({'op': 'click', 'control': day})
    snap = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)['snapshot']
    book = next(c for c in snap['controls'] if c['label'] == 'Book')
    spec['steps'].append({'op': 'click', 'control': book})
    result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert not state['posts'], 'Calendar preparation and Book inspection must send no booking.'
    review = result['review']
    spec.update(stage='submit', steps=review['steps'], submit=review['submit'],
        request=review['request'], expected=common.review_hash(result['snapshot']))
    result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert result['outcome'] == 'confirmed'
    assert state['posts'] == [BODY]
    driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert state['posts'] == [BODY], 'Replaying the task must not book twice.'


def test_calendar_changed_day_and_disabled_arrow_cannot_be_replayed(browser):
    live, state, proto = browser
    spec = calendar_spec(state, visible_target=True)
    snap = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)['snapshot']
    disabled = next(c for c in snap['controls'] if c['selector'] == 'css=#disabled')
    spec['steps'] = [{'op': 'click', 'control': disabled}]
    with pytest.raises(ValueError, match='control changed'):
        driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    day = next(c for c in snap['controls'] if c['label'] == 'MON 19')
    spec['steps'] = [{'op': 'click', 'control': day}]
    state['tamper_day'] = True
    with pytest.raises(ValueError, match='control changed'):
        driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert not state['posts']


def test_preparation_waits_for_a_saved_member_session_without_submitting(browser):
    live, state, proto = browser
    state['delayed_member'] = True
    result = driver.run(live, proto, initial(), allowlist=['*'], root=live.session_dir)
    assert result['snapshot']['signed_in']
    assert result['status'] == 'no_changes'
    assert not state['posts']


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


def test_real_browser_loss_after_the_post_keeps_the_outcome_unknown(browser):
    live, state, proto = browser; spec = review(browser); state['close_after_post'] = True
    result = driver.run(live, proto, spec, allowlist=['*'], root=live.session_dir)
    assert result['outcome'] == 'unknown' and len(state['posts']) == 1
    assert not driver.base.claim(live.session_dir, spec['id']), 'A lost browser must not erase the durable attempt.'


def test_workout_wire_rejects_arbitrary_sites_and_generic_browser_approval():
    import browser_operator
    spec = initial()
    assert browser_operator.parse_spec(json.dumps({schema.ACTION: spec}))[0]['action'] == schema.ACTION
    spec['url'] = 'https://other.example/book'
    with pytest.raises(ValueError): schema.task(spec)
    with pytest.raises(browser_operator.SpecError):
        browser_operator.parse_spec(json.dumps({'actions': [{'action': schema.ACTION, 'spec': initial()}]}))


@pytest.mark.parametrize('text', ['Group Fitness Class $0.50', 'Group Fitness Class USD 0.50 Free towel',
                                  'Group Fitness Class Not included in your membership',
                                  'Group Fitness Class Free cancellation'])
def test_small_fees_and_unrelated_free_benefits_cannot_approve_a_class(text):
    with pytest.raises(ValueError): driver.terms({'contexts': {'c0': text}}, {'ref': 'c0'})


def test_endpoint_probe_shape_never_exports_request_secrets_or_unknown_field_names():
    from types import SimpleNamespace
    import workout_booking_probe as probe
    private = 'private-value-not-for-output'
    request = SimpleNamespace(url='https://api.glofox.com/2.0/bookings/' + private + '?token=' + private,
        method='POST', post_data=json.dumps({'model': 'event', 'model_id': private,
            'user_id': private, 'branch_id': private, private: private,
            'join_waiting_list': False, 'charge': True, 'payment_method': private}))
    shape = probe.request_shape(request)
    assert private not in json.dumps(shape)
    assert shape['path'] == '/2.0/bookings/:redacted' and shape['query_present']
    assert shape['fields']['user_id'] == 'string' and shape['other_field_count'] == 1
    assert shape['join_waiting_list'] is False and shape['charge'] is True
    assert shape['payment_method'] == 'other' and not shape['requested_branch']
    request.url = 'https://' + private + '/2.0/bookings'
    assert probe.request_shape(request)['host'] == 'other'


@pytest.mark.parametrize('payload', ['not-json', 'null', '[]', '42'])
def test_endpoint_probe_handles_non_object_request_bodies_without_exporting_them(payload):
    from types import SimpleNamespace
    import workout_booking_probe as probe
    shape = probe.request_shape(SimpleNamespace(url=API, method='POST', post_data=payload))
    assert 'fields' not in shape and 'body_type' in shape


@pytest.mark.parametrize('blocked_by', ['owner', 'missing_profile', 'viewer', 'automation'])
def test_endpoint_probe_cli_preserves_owner_and_exclusive_profile_boundary(tmp_path, monkeypatch, blocked_by):
    import manual_mode
    import workout_booking_probe as probe
    monkeypatch.setenv('KYREX_HOST_OWNER', 'different-owner' if blocked_by == 'owner' else 'alice')
    monkeypatch.setenv('KYREX_BROWSER_PROFILES_ROOT', str(tmp_path))
    monkeypatch.setenv('KYREX_VIEWER_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(sys, 'argv', ['probe', '--owner', 'alice', '--bot', 'browser-bot',
                                    '--date', '2026-10-19', '--time', '08:30'])
    if blocked_by in {'owner', 'missing_profile'}:
        with pytest.raises(SystemExit, match='refused'): probe.main()
        return
    probe.profiles.profile_dir('alice', 'browser-bot').mkdir(parents=True)
    kind = manual_mode.KIND_MANUAL if blocked_by == 'viewer' else manual_mode.KIND_AUTOMATION
    with manual_mode.acquire('alice', 'browser-bot', kind=kind):
        if blocked_by == 'viewer':
            with pytest.raises(SystemExit, match='End the private viewer'): probe.main()
        else:
            with pytest.raises(manual_mode.ManualControlActive): probe.main()


@pytest.mark.parametrize('api,change', [
    ('https://api.glofox.com/2.0/bookings', {}),
    (API, {'join_waiting_list': True}),
    (API, {'payment_method': 'credit_card', 'charge': True, 'price': 15}),
])
def test_readonly_endpoint_probe_captures_unsupported_native_writes_without_sending(browser, api, change, monkeypatch):
    import workout_booking_probe as probe
    # The deployed host predates this Cloud-only helper; no rebuild is needed.
    monkeypatch.delattr(schema, 'requested_day_control')
    live, state, _ = browser
    calendar_spec(state, visible_target=True)
    state['api'] = api
    state['body'].update(change)
    result = probe.inspect(live, ['*'], '2026-10-19', '08:30')
    assert result['status'] == 'writes_blocked' and not state['posts']
    assert len(result['requests']) == 1
    shape = result['requests'][0]
    assert shape['phase'] == 'class' and shape['method'] == 'POST'
    assert shape['event_model'] and shape['fields']['model_id'] == 'string'
    assert state['token'] not in json.dumps(result) and 'e' * 24 not in json.dumps(result)
    # Guards outlive the capture so late page timers cannot send a write.
    live._page.frames[1].locator('#book').click()
    live._page.wait_for_timeout(100)
    assert not state['posts']


def test_readonly_endpoint_probe_wont_guess_a_missing_requested_day(browser):
    import workout_booking_probe as probe
    live, state, _ = browser
    result = probe.inspect(live, ['*'], '2026-10-19', '08:30')
    assert result['status'] == 'requested_day_not_unique_or_visible'
    assert not result['requests'] and not state['posts']


@pytest.mark.parametrize('label,clock,delayed', [('Book now', '08:30 a.m.', False),
                                               ('Book Class', '8:30 am', True)])
def test_endpoint_probe_handles_ordinary_label_time_and_rendering_variants(browser, label, clock, delayed):
    import workout_booking_probe as probe
    live, state, _ = browser
    calendar_spec(state, visible_target=True)
    state.update(book_label=label, clock_label=clock, delayed_book=delayed)
    progress = []
    result = probe.inspect(live, ['*'], '2026-10-19', '08:30', progress=progress.append)
    assert result['status'] == 'writes_blocked' and not state['posts']
    assert progress == ['opening_portal', 'member_session_verified', 'requested_day_selected', 'inspecting_class_control']


@pytest.mark.parametrize('change,flag,expected', [
    ({'book_label': 'Reserve'}, 'supported_label', False),
    ({'clock_label': '9:30 AM'}, 'requested_time_in_context', False),
    ({'disabled_book': True}, 'disabled', True),
])
def test_endpoint_probe_explains_missing_control_without_leaking_page_text(browser, change, flag, expected):
    import workout_booking_probe as probe
    live, state, _ = browser
    calendar_spec(state, visible_target=True)
    state.update(change)
    result = probe.inspect(live, ['*'], '2026-10-19', '08:30', control_wait=0)
    assert result['status'] == 'class_control_not_unique_or_visible' and not state['posts']
    detail = result['controls']
    assert detail['matching_control_count'] == 0
    checks = [c for c in detail['control_checks'] if c['tag'] == 'button']
    assert checks and checks[0][flag] is expected
    assert 'dom' in detail
    serialized = json.dumps(result)
    assert 'Donna Albertone' not in serialized and state['token'] not in serialized
    assert 'Group Fitness Class' not in serialized and 'css=' not in serialized


def test_endpoint_probe_control_evidence_never_exports_arbitrary_labels_or_contexts():
    import workout_booking_probe as probe
    secret = 'private-value-not-for-output'
    c = {'ref': 'c0', 'frame': PORTAL, 'label': 'Book ' + secret, 'tag': secret, 'disabled': False}
    snap = {'controls': [c], 'contexts': {'c0': 'Group Fitness Class 8:30 AM ' + secret},
            'frame_texts': {PORTAL: 'Group Fitness Class 8:30 AM ' + secret}}
    evidence = probe.control_evidence(snap, '08:30')
    assert secret not in json.dumps(evidence)
    assert evidence['requested_time_in_page'] and evidence['class_in_page']
    assert evidence['control_checks'][0]['tag'] == 'other'


def test_endpoint_probe_groups_nested_buttons_and_rejects_neighbouring_time_context(browser, monkeypatch):
    import workout_booking_probe as probe
    live, state, _ = browser
    calendar_spec(state, visible_target=True)
    state['nested_book_rows'] = True
    captured = []
    shape = probe.request_shape
    def observe(request):
        captured.append(json.loads(request.post_data))
        return shape(request)
    monkeypatch.setattr(probe, 'request_shape', observe)
    result = probe.inspect(live, ['*'], '2026-10-19', '08:30')
    snap = driver.observed(live, lambda url: True)
    # Reproduce the host evidence: three apparent time matches, each exposed
    # as both an Angular wrapper and a nested native button (six controls).
    old_matches = [c for c in snap['controls'] if probe.BOOK_LABEL.fullmatch(c['label'])
                   and probe.time_pattern('08:30').search(snap['contexts'].get(c['ref'], ''))]
    assert len(old_matches) == 6
    assert result['status'] == 'writes_blocked' and not state['posts']
    assert len(captured) == 1 and captured[0]['model_id'] == 'c' * 24
    candidates, grouping = probe.class_actions(live._page, snap, lambda url: True, '08:30')
    assert len(candidates) == 1 and candidates[0]['tag'] == 'md-button'
    assert grouping['book_control_count'] == 6 and grouping['action_group_count'] == 3
    assert sum(g['stopped_at_other_action'] for g in grouping['groups']) == 2


@pytest.mark.parametrize('change,expected_matches', [({'two_matching_rows': True}, 2), ({'disabled_parent': True}, 0)])
def test_endpoint_probe_does_not_choose_between_distinct_classes_or_bypass_disabled_wrapper(browser, change, expected_matches):
    import workout_booking_probe as probe
    live, state, _ = browser
    calendar_spec(state, visible_target=True)
    state.update(nested_book_rows=True, **change)
    result = probe.inspect(live, ['*'], '2026-10-19', '08:30', control_wait=0)
    assert result['status'] == 'class_control_not_unique_or_visible'
    assert result['controls']['matching_control_count'] == expected_matches
    assert not result['requests'] and not state['posts']
    raw = json.dumps(result)
    assert 'css=' not in raw and state['token'] not in raw and 'Group Fitness Class' not in raw


@pytest.mark.parametrize('mode', ['dialog', 'nested', 'unmarked'])
def test_endpoint_probe_follows_new_confirmation_without_reclicking_background_book(browser, mode):
    import workout_booking_probe as probe
    live, state, _ = browser
    calendar_spec(state, visible_target=True)
    state.update(nested_book_rows=True, confirm_dialog=mode, api='https://api.glofox.com/2.0/bookings')
    progress = []
    result = probe.inspect(live, ['*'], '2026-10-19', '08:30', progress=progress.append)
    assert result['status'] == 'writes_blocked' and len(result['requests']) == 1
    assert result['requests'][0]['path'] == '/2.0/bookings' and not state['posts']
    assert result['requests'][0]['phase'] == 'confirmation'
    assert live._page.frames[1].locator('body').get_attribute('data-opened') == '1'
    assert progress[-2:] == ['inspecting_class_control', 'inspecting_confirmation_control']
    assert state['token'] not in json.dumps(result)


@pytest.mark.parametrize('mode', ['wrong_time', 'no_time', 'multiple', 'absent'])
def test_endpoint_probe_stops_on_changed_ambiguous_or_missing_confirmation(browser, mode):
    import workout_booking_probe as probe
    live, state, _ = browser
    calendar_spec(state, visible_target=True)
    state.update(confirm_dialog=mode)
    result = probe.inspect(live, ['*'], '2026-10-19', '08:30', control_wait=0)
    assert result['status'] == 'class_control_not_unique_or_visible'
    assert not state['posts'] and not result['requests']
    assert live._page.frames[1].locator('body').get_attribute('data-opened') == '1'
    groups = result['controls']['action_groups']
    if mode == 'absent': assert groups['new_controls_only']
    elif mode == 'multiple': assert groups['ambiguous_dialogs']
    else: assert groups['dialog_scoped']
