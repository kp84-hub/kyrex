"""Manual Chrome and managed automation must address the same iframe storage."""
import os
from types import SimpleNamespace

import pytest

import browser_operator as operator


def test_storage_options_preserve_unrelated_defaults_and_existing_native_mode():
    old = '--disable-features=KeepA,ThirdPartyStoragePartitioning,KeepB'
    assert operator._native_storage_options(['--other', old]) == {
        'ignore_default_args': [old], 'args': ['--disable-features=KeepA,KeepB']}
    assert operator._native_storage_options(['--disable-features=KeepA']) == {}
    assert operator._native_storage_options([]) == {}
    assert operator._native_storage_options([
        '--disable-features=ThirdPartyStoragePartitioning']) == {
            'ignore_default_args': ['--disable-features=ThirdPartyStoragePartitioning'],
            'args': []}


@pytest.mark.parametrize('argv', [None, ['--disable-features=A', '--disable-features=B'], [1]])
def test_unexpected_arguments_fail_closed(argv):
    with pytest.raises(ValueError):
        operator._native_storage_options(argv)


def test_probe_closes_on_protocol_failure_and_does_not_expose_command_line():
    closed = []
    def broken(method):
        raise RuntimeError('private browser command line')
    browser = SimpleNamespace(new_browser_cdp_session=lambda: SimpleNamespace(send=broken),
                              close=lambda: closed.append(True))
    chromium = SimpleNamespace(launch=lambda **kwargs: browser)
    with pytest.raises(operator.DriverError, match='compatible browser storage') as error:
        operator._managed_storage_options(chromium, '/fixture/chrome')
    assert closed == [True]
    assert 'private' not in str(error.value)


def fixture(route):
    # Reserved, intercepted domains: no real account, site, or network writes.
    body = ('<iframe src="https://portal.test/frame"></iframe>'
            if '/frame' not in route.request.url else '<body>Fixture portal</body>')
    route.fulfill(status=200, content_type='text/html', body=body)


def portal(page, site='studio.test'):
    page.goto('https://' + site, wait_until='load')
    frame = page.frames[1]
    frame.locator('body').wait_for()
    return frame


@pytest.mark.parametrize('diagnostic_mode', [False, True])
def test_native_iframe_session_survives_managed_handoff_with_isolation(
        tmp_path, monkeypatch, diagnostic_mode):
    if os.environ.get('KYREX_TEST_BROWSER') != '1':
        pytest.skip('Required booking CI runs real Chromium.')
    from playwright.sync_api import sync_playwright
    profile = tmp_path / 'owner-a'
    with sync_playwright() as pw:
        executable = os.environ.get('KYREX_TEST_BROWSER_EXECUTABLE') or pw.chromium.executable_path
        # Native viewer storage semantics: no feature-disable overrides.
        context = pw.chromium.launch_persistent_context(str(profile),
            executable_path=executable, headless=True,
            args=['--no-sandbox', '--disable-features='])
        try:
            context.route('**/*', fixture)
            portal(context.pages[0]).evaluate("localStorage.setItem('fixture-session', 'opaque')")
        finally:
            context.close()
    monkeypatch.setenv('KYREX_BROWSER_EXECUTABLE', executable)
    monkeypatch.setenv('KYREX_BROWSER_HEADLESS', '1')
    # The same profile sees the login; another top-level site and owner do not.
    for owner_profile, expected in ((profile, True), (tmp_path / 'owner-b', False), (profile, True)):
        driver = operator.PlaywrightDriver(owner_profile, managed=True)
        driver.diagnostic_mode = diagnostic_mode
        try:
            driver.open()
            driver._context.route('**/*', fixture)
            assert portal(driver._page).evaluate(
                "localStorage.getItem('fixture-session') === 'opaque'") is expected
            assert portal(driver._page, 'unrelated.test').evaluate(
                "localStorage.getItem('fixture-session') === 'opaque'") is False
        finally:
            driver.close()
