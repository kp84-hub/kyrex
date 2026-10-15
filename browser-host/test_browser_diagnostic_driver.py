"""Read-only probe safety using real, intercepted Chromium pages."""
import os
from types import SimpleNamespace
import pytest

import browser_operator as operator
import browser_diagnostics as diag
import browser_diagnostic_driver as driver
import host_allowlist


@pytest.mark.parametrize('spec', [
    {'browser_diagnostic': {'url': 'https://example.com', 'steps': ['submit']}},
    {'browser_diagnostic': {'url': 'https://example.com'}, 'actions': [{'action': 'click'}]},
    {'actions': [{'action': 'browser_diagnostic', 'url': 'https://example.com'}]},
])
def test_diagnostic_cannot_smuggle_interaction(spec):
    import json
    with pytest.raises(operator.SpecError): operator.parse_spec(json.dumps(spec))


def test_host_preflight_intersects_permissions_and_recognizes_new_wire():
    wire = diag.wire('https://example.com')
    assert host_allowlist.preflight(wire, ['example.com'])[0]
    assert not host_allowlist.preflight(wire, ['other.example'])[0]
    assert not operator.preflight(wire, ['other.example'])[0]


def test_guest_bootstrap_exception_cannot_be_used_for_member_login_or_booking():
    import workout_booking_schema as workout
    guest = {'branch_id': workout.BRANCH, 'login': 'GUEST', 'password': 'GUEST'}
    assert workout.guest_bootstrap('https://api.glofox.com/2.0/login', 'POST', guest)
    assert not workout.guest_bootstrap('https://api.glofox.com/2.0/login', 'POST', {**guest, 'login': 'member'})
    assert not workout.guest_bootstrap('https://api.glofox.com/2.3/branches/' + workout.BRANCH + '/bookings', 'POST', guest)


def test_browser_launch_failure_has_completed_dispatch_stage(tmp_path):
    class FailedDriver:
        def open(self): raise operator.DriverError('private local path and account')
    proto = operator.FakeProto()
    result = operator.run_actions(operator.parse_spec(diag.wire('https://example.com')), FailedDriver(),
        root=tmp_path, allowlist=['example.com'], proto=proto)
    assert result['diagnostic']['code'] == 'browser_launch_failed'
    assert result['diagnostic']['stages']['dispatch'] == 'passed'
    assert 'private local' not in str(result)


@pytest.fixture
def real_browser():
    if os.environ.get('KYREX_TEST_BROWSER') != '1': pytest.skip('Required booking CI runs real Chromium.')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        chrome = pw.chromium.launch(headless=True, executable_path=os.environ.get('KYREX_TEST_BROWSER_EXECUTABLE'))
        context = chrome.new_context(service_workers='block')
        page = context.new_page()
        yield SimpleNamespace(_page=page, _context=context)
        chrome.close()


def test_page_reads_block_http_writes_and_do_not_click_or_leak_content(real_browser):
    d = real_browser; reached = []
    def fixture(route):
        reached.append((route.request.method, route.request.url))
        route.fulfill(status=200, content_type='text/html', body='''<body>private member information
          <button onclick="fetch('/bookings', {method:'POST'})">Book</button>
          <script>fetch('/write', {method:'POST',body:'private-cookie'}).catch(()=>{});</script></body>''')
    d._context.route('**/*', fixture)
    proto = operator.FakeProto()
    result = driver.run(d, proto, {'url': 'https://example.com'}, allowlist=['example.com'],
                        stages={'dispatch': 'passed', 'browser_launch': 'passed'})
    assert result['status'] == 'no_changes'
    assert result['diagnostic']['stages']['site_access'] == 'passed'
    assert all(method == 'GET' for method, _ in reached)
    assert all('/bookings' not in url and '/write' not in url for _, url in reached)
    assert 'private member' not in str(result) and 'private-cookie' not in str(result)


def test_unreachable_site_is_not_reported_as_connection_failure(real_browser):
    real_browser._context.route('**/*', lambda r: r.fulfill(status=503, body='private failure details'))
    result = driver.run(real_browser, operator.FakeProto(), {'url': 'https://example.com'},
                        allowlist=['example.com'], stages={'browser_launch': 'passed'})
    assert result['diagnostic']['code'] == 'site_unreachable'
    assert 'private failure' not in str(result)


@pytest.mark.parametrize('signed_in', [False, True])
def test_member_portal_sign_in_is_observed_without_account_text(real_browser, signed_in):
    import workout_booking_schema as workout
    portal = 'https://app.glofox.com/portal/#/branch/' + workout.BRANCH + '/classes-list-view'
    def fixture(route):
        body = ('<iframe src="' + portal + '"></iframe>' if 'level6training.com' in route.request.url
                else '<body>' + ('My bookings' if signed_in else 'Sign in') + ' private account name</body>')
        route.fulfill(status=200, content_type='text/html', body=body)
    real_browser._context.route('**/*', fixture)
    result = driver.run(real_browser, operator.FakeProto(), {'url': workout.URL},
                        allowlist=list(workout.HOSTS), stages={'dispatch': 'passed', 'browser_launch': 'passed'})
    assert result['diagnostic']['code'] == ('' if signed_in else 'login_required')
    if signed_in: assert result['diagnostic']['stages']['sign_in'] == 'passed'
    assert 'private account' not in str(result)


def test_delayed_iframe_and_member_session_do_not_report_an_initial_login_link(real_browser):
    import workout_booking_schema as workout
    portal = 'https://app.glofox.com/portal/#/branch/' + workout.BRANCH + '/classes-list-view'
    def fixture(route):
        body = ('''<body><script>setTimeout(() => {
          const f=document.createElement('iframe'); f.src=''' + repr(portal) + ''';
          document.body.appendChild(f); }, 900);</script></body>'''
          if 'level6training.com' in route.request.url else
          '''<body>Login / Register<script>setTimeout(() => {
            document.body.innerHTML='Profile · Log out'; }, 1200);</script></body>''')
        route.fulfill(status=200, content_type='text/html', body=body)
    real_browser._context.route('**/*', fixture)
    result = driver.run(real_browser, operator.FakeProto(), {'url': workout.URL},
                        allowlist=list(workout.HOSTS), stages={'browser_launch': 'passed'})
    assert result['diagnostic']['code'] == ''
    assert result['diagnostic']['stages']['sign_in'] == 'passed'


@pytest.mark.parametrize('blocked_auth', [False, True])
def test_loading_or_blocked_auth_is_not_reported_as_logout(real_browser, monkeypatch, blocked_auth):
    import workout_booking_schema as workout
    import workout_booking_driver as adapter
    original = adapter.member_session
    monkeypatch.setattr(adapter, 'member_session',
                        lambda page, allowed: original(page, allowed, timeout_ms=1000))
    portal = 'https://app.glofox.com/portal/#/branch/' + workout.BRANCH + '/classes-list-view'
    reached = []
    def fixture(route):
        reached.append(route.request.method)
        body = ('<iframe src="' + portal + '"></iframe>' if 'level6training.com' in route.request.url else
                '''<body>Login / Register<script>
                fetch('https://api.glofox.com/2.0/login', {method:'POST',
                  body:JSON.stringify({login:'private-fixture-member', password:'not-a-credential'})}).catch(()=>{});
                </script></body>''' if blocked_auth else '<body>Loading member session…</body>')
        route.fulfill(status=200, content_type='text/html', body=body)
    real_browser._context.route('**/*', fixture)
    result = driver.run(real_browser, operator.FakeProto(), {'url': workout.URL},
                        allowlist=list(workout.HOSTS), stages={'browser_launch': 'passed'})
    assert result['diagnostic']['code'] == ('session_verification_blocked' if blocked_auth else 'session_unverified')
    assert 'POST' not in reached
    assert 'private-fixture-member' not in str(result)
    assert 'not-a-credential' not in str(result)
