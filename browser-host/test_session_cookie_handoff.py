"""A real private viewer browser -> repeated managed tasks, with no live account."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from browser_session_cookies import COOKIE_FILE


@pytest.mark.parametrize('crash', [False, True])
def test_real_sign_in_browser_checkpoints_session_cookie_before_task_restart(tmp_path, monkeypatch, crash):
    if os.environ.get('KYREX_TEST_BROWSER') != '1':
        pytest.skip('Real browser handoff runs in booking CI under Xvfb.')
    from playwright.sync_api import sync_playwright
    from browser_operator import PlaywrightDriver
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Set-Cookie', 'session_fixture=opaque; Path=/; HttpOnly; SameSite=Lax')
            self.end_headers(); self.wfile.write(b'<html>Fixture member signed in</html>')
        def log_message(self, *_): pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    profile = tmp_path / 'owner-a'; profile.mkdir()
    with sync_playwright() as pw: executable = pw.chromium.executable_path
    # The production helper retains its sandbox. The test runner can prohibit
    # user namespaces, so inject no-sandbox into this fixture subprocess only.
    script = (
        'import sys, viewer_ctl, viewer_browser; '
        'viewer_ctl.CHROMIUM_BIN=sys.argv[1]; '
        'viewer_ctl.CHROMIUM_FLAGS=[*viewer_ctl.CHROMIUM_FLAGS,"--no-sandbox"]; '
        'viewer_browser.run(sys.argv[2],url=sys.argv[3])')
    process = subprocess.Popen([sys.executable, '-c', script, executable, str(profile),
        f'http://127.0.0.1:{server.server_port}/fixture-login'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 25
        while not (profile / COOKIE_FILE).exists():
            assert process.poll() is None, 'private viewer helper exited before saving its fixture sign-in'
            assert time.monotonic() < deadline, 'fixture login was not checkpointed'
            time.sleep(.1)
        process.send_signal(signal.SIGKILL if crash else signal.SIGTERM); process.wait(timeout=20)
        assert process.returncode == (-signal.SIGKILL if crash else 0)
        # The pipe controller/Playwright child must close Chrome even when
        # Python is killed. Do not hand a profile to another browser early.
        deadline = time.monotonic() + 15
        while (profile / 'SingletonLock').is_symlink() and time.monotonic() < deadline:
            time.sleep(.1)
        assert not (profile / 'SingletonLock').is_symlink(), 'viewer left an active or unverified browser profile lock'
        monkeypatch.setenv('KYREX_BROWSER_EXECUTABLE', executable)
        monkeypatch.setenv('KYREX_BROWSER_HEADLESS', '1')
        for directory, expected in ((profile, True), (tmp_path / 'owner-b', False), (profile, True)):
            driver = PlaywrightDriver(directory, managed=True)
            try:
                driver.open()
                values = driver._context.cookies()
                assert any(c['name'] == 'session_fixture' for c in values) is expected
            finally: driver.close()
        # A successful user logout is checkpointed as empty, never resurrected.
        driver = PlaywrightDriver(profile, managed=True)
        driver.open(); driver._context.clear_cookies(); driver.close()
        driver = PlaywrightDriver(profile, managed=True)
        try:
            driver.open()
            assert not any(c['name'] == 'session_fixture' for c in driver._context.cookies())
        finally: driver.close()
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        if process.stderr: process.stderr.close()
        server.shutdown(); server.server_close(); worker.join(timeout=3)
