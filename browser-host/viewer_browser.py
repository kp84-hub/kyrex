"""Private sign-in browser with session checkpoints and a pipe-only controller.

There is no CDP TCP listener. The Playwright controller stays inside the viewer
container and reads cookies only into the profile's encrypted local vault.
Human keyboard/screen traffic continues over the existing private noVNC route.
"""
import argparse
import os
import signal
import sys
import time

from browser_session_cookies import SessionCookies, SessionCookieError


def open_context(pw, profile):
    from viewer_ctl import CHROMIUM_BIN, CHROMIUM_FLAGS
    from browser_operator import _managed_storage_options
    options = _managed_storage_options(pw.chromium, CHROMIUM_BIN)
    ignored = options.pop('ignore_default_args', []) + ['--enable-automation']
    return pw.chromium.launch_persistent_context(
        profile, executable_path=CHROMIUM_BIN, headless=False,
        chromium_sandbox=True, ignore_default_args=ignored,
        args=[*CHROMIUM_FLAGS, *options.pop('args', [])],
        handle_sigterm=False, handle_sigint=False, handle_sighup=False, **options)


def run(profile, *, url='', connector=''):
    from playwright.sync_api import sync_playwright
    from viewer_ctl import CHROMIUM_BIN, CHROMIUM_FLAGS
    # Match the native viewer's partitioned storage. Discover the installed
    # Playwright defaults with a disposable, blank browser, never an owner page.
    from browser_operator import _managed_storage_options
    stopped = False
    def stop(*_):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with sync_playwright() as pw:
        context = open_context(pw, profile)
        keeper = SessionCookies(profile, context)
        restored = False
        try:
            try: keeper.restore()
            except SessionCookieError:
                # This helper runs under the human's exclusive viewer lock.
                keeper.recover_for_manual_sign_in()
            restored = True
            page = context.pages[0] if context.pages else context.new_page()
            if connector == 'messages': url = 'https://messages.google.com/web/'
            if url: page.goto(url, wait_until='domcontentloaded', timeout=30000)
            while not stopped and context.pages:
                keeper.save()
                context.pages[0].wait_for_timeout(250)
        finally:
            try:
                if restored: keeper.save(force=True)
            finally: context.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', required=True)
    parser.add_argument('--connector', default='', choices=['', 'messages'])
    parser.add_argument('--url', default='')
    args = parser.parse_args()
    try: run(args.profile, url=args.url, connector=args.connector)
    except Exception:
        print('Private sign-in browser stopped. Check the host session configuration.', file=sys.stderr)
        raise SystemExit(1) from None
