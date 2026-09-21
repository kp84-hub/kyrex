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
