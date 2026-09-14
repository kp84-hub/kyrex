"""Managed-persistent-context and profile-isolation tests for the Browser
Operator driver (``browser_operator.py``).

The Phase-1 fix has one job: a MANAGED session must reuse the persistent
context/profile instead of a throwaway ``new_context()``, so a login survives
across runs. These tests drive ``PlaywrightDriver`` through a FAKE Playwright
(no real Chromium, no network) and assert:

  * managed + local launch  -> ``launch_persistent_context(user_data_dir=...)``
    and ``new_context`` is NEVER called;
  * managed + CDP endpoint  -> reuses ``browser.contexts[0]`` (never
    ``new_context``), and ``close()`` does not tear the host's context down;
  * unmanaged               -> unchanged behaviour (``new_context`` still used);
  * profile isolation       -> distinct ``(owner, bot_id)`` pairs resolve to
    distinct on-disk directories, with no traversal escape.

Run: python3 -m pytest test_browser_operator_managed.py
"""
import sys
import types

import pytest

import browser_operator as bo


# ── fake playwright ────────────────────────────────────────────────
# A minimal stand-in, injected into sys.modules, so the driver's control flow
# can be asserted without Playwright or Chromium installed.

class _Calls:
    def __init__(self):
        self.new_context = 0
        self.launch = []
        self.launch_persistent = []
        self.connect_cdp = []


class _FakePage:
    def __init__(self):
        self.url = "about:blank"


class _FakeContext:
    def __init__(self):
        self._pages = []
        self.closed = False

    @property
    def pages(self):
        return list(self._pages)

    def new_page(self):
        page = _FakePage()
        self._pages.append(page)
        return page

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, calls, contexts=None):
        self._calls = calls
        self._contexts = list(contexts or [])
        self.closed = False

    @property
    def contexts(self):
        return list(self._contexts)

    def new_context(self, **kwargs):  # noqa: ARG002 - signature parity
        self._calls.new_context += 1
        ctx = _FakeContext()
        self._contexts.append(ctx)
        return ctx

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, calls):
        self._calls = calls
        self.persistent_context = None
        self.remote_browser = None
        self.seed_remote = True

    def launch_persistent_context(self, **kwargs):
        self._calls.launch_persistent.append(kwargs)
        # Chromium opens a persistent profile with one initial page.
        self.persistent_context = _FakeContext()
        self.persistent_context.new_page()
        return self.persistent_context

    def launch(self, **kwargs):
        self._calls.launch.append(kwargs)
        return _FakeBrowser(self._calls)

    def connect_over_cdp(self, endpoint, **kwargs):  # noqa: ARG002
        self._calls.connect_cdp.append(endpoint)
        if self.remote_browser is None:
            self.remote_browser = _FakeBrowser(self._calls)
        if self.seed_remote and not self.remote_browser.contexts:
            # A host-launched Chromium has ONE default (persistent) context.
            ctx = _FakeContext()
            ctx.new_page()
            self.remote_browser._contexts.append(ctx)
        return self.remote_browser


class _FakePW:
    def __init__(self):
        self.calls = _Calls()
        self.chromium = _FakeChromium(self.calls)
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeSyncPlaywright:
    def __init__(self, pw):
        self._pw = pw

    def start(self):
        return self._pw


def _install_fake_playwright(monkeypatch, pw):
    sync_mod = types.ModuleType("playwright.sync_api")
    sync_mod.sync_playwright = lambda: _FakeSyncPlaywright(pw)
    playwright_mod = types.ModuleType("playwright")
    playwright_mod.sync_api = sync_mod
    monkeypatch.setitem(sys.modules, "playwright", playwright_mod)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_mod)
    return pw


@pytest.fixture
def fake_pw(monkeypatch):
    pw = _FakePW()
    _install_fake_playwright(monkeypatch, pw)
    monkeypatch.delenv("KYREX_BROWSER_MANAGED", raising=False)
    monkeypatch.delenv("KYREX_BROWSER_SESSION_DIR", raising=False)
    monkeypatch.setenv("KYREX_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    return pw


# ── managed context/profile reuse ──────────────────────────────────

def test_managed_local_launches_persistent_context_without_new_context(
    tmp_path, fake_pw
):
    driver = bo.PlaywrightDriver(tmp_path, endpoint="", managed=True)
    driver.open()

    assert len(fake_pw.calls.launch_persistent) == 1, (
        "a managed local session must launch a persistent context"
    )
    assert fake_pw.calls.launch_persistent[0]["user_data_dir"] == str(tmp_path)
    assert fake_pw.calls.new_context == 0, "managed mode must not call new_context"
    assert fake_pw.calls.launch == []
    # The persistent profile's own page is reused, not a fresh one.
    assert driver._page is fake_pw.chromium.persistent_context.pages[0]


def test_managed_cdp_reuses_host_context_without_new_context(tmp_path, fake_pw):
    driver = bo.PlaywrightDriver(
        tmp_path, endpoint="http://127.0.0.1:9222", managed=True
    )
    driver.open()

    assert fake_pw.calls.connect_cdp == ["http://127.0.0.1:9222"]
    assert fake_pw.calls.new_context == 0, (
        "a managed CDP session must reuse the host's default context"
    )
    assert driver._context is fake_pw.chromium.remote_browser.contexts[0]
    assert driver._page is fake_pw.chromium.remote_browser.contexts[0].pages[0]


def test_managed_cdp_close_is_a_guest(tmp_path, fake_pw):
    driver = bo.PlaywrightDriver(
        tmp_path, endpoint="http://127.0.0.1:9222", managed=True
    )
    driver.open()
    host_context = driver._context

    driver.close()

    assert host_context.closed is False, (
        "a managed CDP guest must not destroy the host's persistent context"
    )
    assert fake_pw.stopped is True


def test_managed_local_close_releases_its_own_context(tmp_path, fake_pw):
    driver = bo.PlaywrightDriver(tmp_path, endpoint="", managed=True)
    driver.open()
    owned = driver._context

    driver.close()

    assert owned.closed is True


def test_unmanaged_still_uses_new_context(tmp_path, fake_pw):
    driver = bo.PlaywrightDriver(tmp_path, endpoint="", managed=False)
    driver.open()

    assert fake_pw.calls.launch_persistent == []
    assert fake_pw.calls.new_context == 1, "unmanaged behaviour must be unchanged"


# ── managed-mode derivation from the environment ───────────────────

def test_is_managed_env_derivation(monkeypatch):
    monkeypatch.delenv("KYREX_BROWSER_MANAGED", raising=False)
    monkeypatch.delenv("KYREX_BROWSER_SESSION_DIR", raising=False)
    assert bo._is_managed() is False

    monkeypatch.setenv("KYREX_BROWSER_SESSION_DIR", "/srv/profiles/x")
    assert bo._is_managed() is True, "a session dir implies managed"

    monkeypatch.setenv("KYREX_BROWSER_MANAGED", "0")
    assert bo._is_managed() is False, "an explicit 0 wins over the session dir"

    monkeypatch.setenv("KYREX_BROWSER_MANAGED", "1")
    monkeypatch.delenv("KYREX_BROWSER_SESSION_DIR", raising=False)
    assert bo._is_managed() is True


def test_driver_defaults_managed_from_env(tmp_path, fake_pw, monkeypatch):
    monkeypatch.setenv("KYREX_BROWSER_SESSION_DIR", str(tmp_path))
    driver = bo.PlaywrightDriver(tmp_path)
    assert driver.managed is True


# ── profile isolation ──────────────────────────────────────────────

class TestProfileIsolation:
    ROOT = "/tmp/kx-browser-operator-isolation"

    def test_distinct_bots_get_distinct_dirs(self):
        a = bo.browser_session_dir(self.ROOT, "bot-1", "owner")
        b = bo.browser_session_dir(self.ROOT, "bot-2", "owner")
        assert a != b

    def test_distinct_owners_get_distinct_dirs(self):
        a = bo.browser_session_dir(self.ROOT, "bot", "alice")
        b = bo.browser_session_dir(self.ROOT, "bot", "bob")
        assert a != b

    def test_traversal_is_neutralised(self):
        path = bo.browser_session_dir(self.ROOT, "../../etc", "..")
        assert ".." not in path.parts
        assert path.is_relative_to(self.ROOT)

    def test_managed_driver_uses_the_isolated_dir(self, fake_pw):
        isolated = bo.browser_session_dir(self.ROOT, "bot-9", "carol")
        driver = bo.PlaywrightDriver(isolated, managed=True)
        driver.open()
        assert fake_pw.calls.launch_persistent[0]["user_data_dir"] == str(isolated)
