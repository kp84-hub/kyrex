"""Pure safety tests for the fixed-group inbound watcher."""
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import messages_watcher as watcher  # noqa: E402


def test_trigger_is_exact_after_outer_whitespace_only():
    assert watcher.exact_trigger("#L6Workout")
    assert watcher.exact_trigger("  #L6Workout\n")
    for bad in ("#l6workout", "#L6Workout now", "x#L6Workout", ""):
        assert not watcher.exact_trigger(bad)


def test_cloud_url_is_fixed_and_never_contains_conversation():
    assert watcher.cloud_trigger_url(
        "wss://chat.kyrex.dev/api/browser-hosts/ws"
    ) == "https://chat.kyrex.dev/api/browser-hosts/google-messages-trigger"


def test_fingerprint_is_stable_and_opaque():
    value = watcher.trigger_fingerprint("private-dom-identity")
    assert len(value) == 64
    assert value == watcher.trigger_fingerprint("private-dom-identity")
    assert "private" not in value


class _Page:
    def __init__(self, url="https://messages.google.com/web/conversations/fixed"):
        self.url = url
        self.goto_args = None
        self.waited = None

    def goto(self, url, **kwargs):
        self.goto_args = (url, kwargs)

    def wait_for_timeout(self, milliseconds):
        self.waited = milliseconds


class TimeoutError(Exception):
    pass


class _TimeoutPage(_Page):
    def goto(self, url, **kwargs):
        self.goto_args = (url, kwargs)
        raise TimeoutError("navigation lifecycle remained pending")


class _TextNode:
    def __init__(self, identity):
        self.identity = identity

    def evaluate(self, _script):
        return self.identity


class _TextHits:
    def __init__(self, identities):
        self.identities = identities

    def count(self):
        return len(self.identities)

    def nth(self, index):
        return _TextNode(self.identities[index])


class _ShadowTextPage:
    def __init__(self, identities):
        self.identities = identities
        self.lookup = None

    def get_by_text(self, value, *, exact):
        self.lookup = (value, exact)
        return _TextHits(self.identities)


def test_open_conversation_waits_for_commit_not_domcontentloaded():
    page = _Page()
    watcher._open_conversation(page, page.url)
    assert page.goto_args == (
        page.url, {"wait_until": "commit", "timeout": 30000})
    assert page.waited == 2000


def test_open_conversation_rejects_unpaired_profile():
    page = _Page("https://messages.google.com/web/welcome")
    try:
        watcher._open_conversation(page, page.url)
    except RuntimeError as exc:
        assert "pairing is not active" in str(exc)
    else:
        raise AssertionError("unpaired profile must fail closed")


def test_open_conversation_recovers_timeout_on_messages_surface():
    page = _TimeoutPage()
    watcher._open_conversation(page, page.url)
    assert page.waited == 2000


def test_open_conversation_does_not_recover_timeout_before_navigation():
    page = _TimeoutPage("about:blank")
    try:
        watcher._open_conversation(
            page, "https://messages.google.com/web/conversations/fixed")
    except TimeoutError:
        pass
    else:
        raise AssertionError("blank-page navigation timeout must fail closed")


def test_latest_trigger_uses_shadow_dom_aware_exact_text_lookup():
    page = _ShadowTextPage(["old-message", "new-message"])
    assert watcher._latest_trigger(page) == "2|new-message"
    assert page.lookup == (watcher.TRIGGER, True)


def test_latest_trigger_returns_none_without_exact_text_match():
    page = _ShadowTextPage([])
    assert watcher._latest_trigger(page) is None


def test_profile_browser_pids_matches_exact_profile(tmp_path):
    proc = tmp_path / "proc"
    (proc / "101").mkdir(parents=True)
    (proc / "102").mkdir()
    (proc / "103").mkdir()
    profile = Path("/profiles/bot-calendar/owner-kp84-hub")
    (proc / "101" / "cmdline").write_bytes(
        b"/usr/bin/chromium\0--user-data-dir=" + str(profile).encode() + b"\0")
    (proc / "102" / "cmdline").write_bytes(
        b"/usr/bin/chromium\0--user-data-dir=/profiles/other\0")
    (proc / "103" / "cmdline").write_bytes(
        b"python3\0--user-data-dir=" + str(profile).encode() + b"\0")
    assert watcher._profile_browser_pids(profile, proc) == [101]


def test_failed_launch_recovery_terminates_and_cleans_singletons(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (profile / name).touch()
    calls = []
    answers = iter([[44], []])
    monkeypatch.setattr(watcher, "_profile_browser_pids",
                        lambda value: next(answers))
    monkeypatch.setattr(watcher.os, "kill",
                        lambda pid, sig: calls.append((pid, sig)))
    watcher._recover_failed_launch(profile)
    assert calls == [(44, signal.SIGTERM)]
    assert not any((profile / name).exists() for name in (
        "SingletonLock", "SingletonSocket", "SingletonCookie"))
