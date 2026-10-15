"""Viewer shutdown must leave a reusable profile without unlocking a rival."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import manual_mode as mm
import viewer_ctl as vc


def singleton_links(profile, pid, hostname=None):
    profile.mkdir(exist_ok=True)
    targets = {'SingletonLock': f'{hostname or socket.gethostname()}-{pid}',
               'SingletonSocket': '/tmp/fixture-socket',
               'SingletonCookie': 'fixture-cookie'}
    for name, target in targets.items():
        (profile / name).symlink_to(target)


@pytest.fixture
def browser_process(monkeypatch):
    proc = vc._spawn([sys.executable, '-c', 'import time; time.sleep(60)'])
    # These unit cases isolate unlink ownership from procfs. The full browser
    # CI test below exercises the actual process-group proof and handoff.
    monkeypatch.setattr(vc, '_browser_group_stopped', lambda p: p.poll() is not None)
    try:
        yield proc
    finally:
        vc._terminate_all([proc])


def test_only_stopped_own_browser_links_are_removed(tmp_path, browser_process):
    singleton_links(tmp_path, browser_process.pid)
    login = tmp_path / 'Cookies'
    login.write_bytes(b'opaque-test-session')
    other = tmp_path / 'other-profile'
    singleton_links(other, browser_process.pid)
    with mm.acquire('owner-a', 'bot-a', root=tmp_path / 'state'):
        vc._terminate_all([browser_process])
        assert vc._cleanup_browser_locks(tmp_path, browser_process, had_lock=False)
    assert not any((tmp_path / name).is_symlink() for name in
                   ('SingletonLock', 'SingletonCookie', 'SingletonSocket'))
    assert login.read_bytes() == b'opaque-test-session'
    assert (other / 'SingletonLock').is_symlink()


@pytest.mark.parametrize('hostname,pid_offset', [('other-container', 0), (None, 1)])
def test_foreign_hostname_or_pid_is_never_unlocked(tmp_path, browser_process,
                                                  hostname, pid_offset):
    singleton_links(tmp_path, browser_process.pid + pid_offset, hostname)
    vc._terminate_all([browser_process])
    assert not vc._cleanup_browser_locks(tmp_path, browser_process, had_lock=False)
    assert (tmp_path / 'SingletonLock').is_symlink()
    assert (tmp_path / 'SingletonCookie').is_symlink()


def test_preexisting_lock_is_kept_even_if_container_hostname_and_pid_repeat(
        tmp_path, browser_process):
    singleton_links(tmp_path, browser_process.pid)
    vc._terminate_all([browser_process])
    assert not vc._cleanup_browser_locks(tmp_path, browser_process, had_lock=True)
    assert (tmp_path / 'SingletonLock').is_symlink()
    assert (tmp_path / 'SingletonSocket').is_symlink()


def test_live_browser_or_surviving_group_cannot_be_unlocked(tmp_path,
                                                          browser_process, monkeypatch):
    singleton_links(tmp_path, browser_process.pid)
    # Skip the bounded wait, without weakening the process/group proof.
    monkeypatch.setattr(vc.time, 'monotonic', iter([0, 3]).__next__)
    assert not vc._cleanup_browser_locks(tmp_path, browser_process, had_lock=False)
    assert (tmp_path / 'SingletonLock').is_symlink()
    vc._terminate_all([browser_process])
    monkeypatch.setattr(vc, '_browser_group_stopped', lambda proc: False)
    monkeypatch.setattr(vc.time, 'monotonic', iter([0, 3]).__next__)
    assert not vc._cleanup_browser_locks(tmp_path, browser_process, had_lock=False)
    assert (tmp_path / 'SingletonLock').is_symlink()


def test_unexpected_regular_file_and_symlinked_profile_are_not_removed(
        tmp_path, browser_process):
    profile = tmp_path / 'profile'
    singleton_links(profile, browser_process.pid)
    vc._terminate_all([browser_process])
    (profile / 'SingletonCookie').unlink()
    (profile / 'SingletonCookie').write_text('keep')
    assert not vc._cleanup_browser_locks(profile, browser_process, had_lock=False)
    assert (profile / 'SingletonLock').is_symlink()
    alias = tmp_path / 'alias'
    alias.symlink_to(profile, target_is_directory=True)
    assert not vc._cleanup_browser_locks(alias, browser_process, had_lock=False)
    assert (profile / 'SingletonCookie').read_text() == 'keep'


def test_unreadable_process_state_fails_closed(monkeypatch):
    def denied(path):
        raise PermissionError('unavailable')
    monkeypatch.setattr(Path, 'iterdir', denied)
    assert not vc._browser_group_stopped(SimpleNamespace(pid=123, poll=lambda: 0))


def test_procfs_from_a_different_pid_namespace_cannot_prove_shutdown(monkeypatch):
    monkeypatch.setattr(vc.os, 'getpid', lambda: -1)
    assert not vc._browser_group_stopped(SimpleNamespace(pid=123, poll=lambda: 0))


def test_browser_gets_graceful_parent_signal_before_display_teardown(monkeypatch):
    events = []
    browser = SimpleNamespace(poll=lambda: None,
        send_signal=lambda sig: events.append(('chrome', sig)),
        wait=lambda timeout: events.append(('chrome-wait', timeout)))
    vc._stop_browser(browser)
    assert events == [('chrome', signal.SIGTERM), ('chrome-wait', 8)]


def test_signal_group_still_works_after_its_parent_exits(monkeypatch):
    calls = []
    monkeypatch.setattr(vc.os, 'killpg', lambda pgid, sig: calls.append((pgid, sig)))
    # getpgid would fail for a reaped parent; the group still has this ID.
    monkeypatch.setattr(vc.os, 'getpgid', lambda pid: pytest.fail('dead parent lookup'))
    vc._signal_tree(SimpleNamespace(pid=123), signal.SIGTERM)
    assert calls == [(123, signal.SIGTERM)]


def test_compose_stop_budget_covers_graceful_and_forced_teardown():
    import yaml
    compose = yaml.safe_load((Path(vc.__file__).parent / 'docker-compose.viewer.yml').read_text())
    assert compose['services']['viewer']['stop_grace_period'] == '70s'


def test_real_chromium_viewer_to_automation_preserves_login(tmp_path, monkeypatch):
    """Required CI uses full Chromium under Xvfb; never visits a real site."""
    if os.environ.get('KYREX_TEST_VIEWER_BROWSER') != '1':
        pytest.skip('Full headed Chromium handoff runs in booking CI.')
    from playwright.sync_api import sync_playwright
    from browser_operator import PlaywrightDriver
    profile = tmp_path / 'profile'
    with sync_playwright() as pw:
        executable = pw.chromium.executable_path
        context = pw.chromium.launch_persistent_context(str(profile),
            executable_path=executable, headless=False, args=['--no-sandbox'])
        context.add_cookies([{'name': 'fixture-session', 'value': 'opaque',
                             'url': 'https://example.test', 'expires': time.time() + 3600}])
        context.close()
    monkeypatch.setenv('KYREX_BROWSER_EXECUTABLE', executable)
    monkeypatch.setenv('KYREX_BROWSER_HEADLESS', '0')
    # Exercise both normal viewer exit and a viewer browser crash. The
    # helper must not need a different-container/PID guess to recover.
    for crash in (False, True):
        with mm.acquire('owner-a', 'bot-a', root=tmp_path / 'state'):
            browser = vc._spawn([executable, f'--user-data-dir={profile}',
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-background-networking', '--no-first-run', 'about:blank'])
            try:
                deadline = time.monotonic() + 15
                while not (profile / 'SingletonLock').is_symlink():
                    assert browser.poll() is None, 'viewer browser exited early'
                    assert time.monotonic() < deadline, 'viewer lock never appeared'
                    time.sleep(.05)
                if crash:
                    vc._signal_tree(browser, signal.SIGKILL)
                    browser.wait(timeout=5)
                else:
                    vc._stop_browser(browser)
            finally:
                vc._terminate_all([browser])
            vc._cleanup_browser_locks(profile, browser, had_lock=False)
            assert not (profile / 'SingletonLock').is_symlink()
        # The next automated task takes the same owner/Bot lock and opens
        # the same profile. No reservation, credential or network request.
        with mm.acquire('owner-a', 'bot-a', kind=mm.KIND_AUTOMATION,
                        root=tmp_path / 'state'):
            driver = PlaywrightDriver(profile, managed=True)
            driver.diagnostic_mode = True
            try:
                driver.open()
                assert any(c['name'] == 'fixture-session' and c['value'] == 'opaque'
                           for c in driver._context.cookies('https://example.test'))
            finally:
                driver.close()
