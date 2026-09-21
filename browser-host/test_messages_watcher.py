"""Pure safety tests for the fixed-group inbound watcher."""
import os
import sys

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
