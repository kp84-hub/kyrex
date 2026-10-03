"""Manual Chrome and the bounded reader contend for the same owner profile."""
import sys
from pathlib import Path

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
import manual_mode
import messages_connector
import profiles
import viewer_ctl


def test_manual_profile_is_exact_reader_profile_and_owner_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_BROWSER_PROFILES_ROOT", str(tmp_path))
    path, key = viewer_ctl.viewer_profile("alice", "messages-connector", "messages")
    assert path == messages_connector.MessagesBrowser("alice", tmp_path).path
    assert path != profiles.messages_profile_dir("bob", root=tmp_path)
    assert path != profiles.profile_dir("alice", "messages-connector", root=tmp_path)
    assert key == profiles.messages_lock_key("alice")
    assert profiles.messages_profile_dir("a" * 100 + "1") != profiles.messages_profile_dir("a" * 100 + "2")
    with pytest.raises(ValueError): profiles.messages_lock_key("")
    with pytest.raises(viewer_ctl.ViewerError): viewer_ctl.viewer_profile("alice", "calendar", "messages")
    with pytest.raises(viewer_ctl.ViewerError): viewer_ctl.viewer_profile("alice", "calendar", "unknown")


def test_manual_lock_blocks_read_and_disconnect_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_VIEWER_STATE_DIR", str(tmp_path / "state"))
    browser = messages_connector.MessagesBrowser("alice", tmp_path)
    browser.path.mkdir(parents=True)
    marker = browser.path / "saved-session"
    marker.write_text("cookie stays local")
    with manual_mode.acquire(*profiles.messages_lock_key("alice"), kind=manual_mode.KIND_MANUAL):
        with pytest.raises(manual_mode.ManualControlActive): browser.acquire_lock()
        with pytest.raises(manual_mode.ManualControlActive): browser.command("disconnect", {})
        assert marker.exists()
    browser.command("disconnect", {})
    assert not browser.path.exists()
    assert browser.lock is None


def test_reader_lock_blocks_manual_viewer(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_VIEWER_STATE_DIR", str(tmp_path / "state"))
    browser = messages_connector.MessagesBrowser("alice", tmp_path)
    browser.lock = browser.acquire_lock()
    try:
        with pytest.raises(manual_mode.ManualControlActive):
            manual_mode.acquire(*profiles.messages_lock_key("alice"), kind=manual_mode.KIND_MANUAL)
    finally:
        browser.close()
    with manual_mode.acquire(*profiles.messages_lock_key("alice"), kind=manual_mode.KIND_MANUAL):
        pass


def test_messages_start_selects_fixed_profile_without_automation_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_BROWSER_PROFILES_ROOT", str(tmp_path))
    monkeypatch.setenv("KYREX_VIEWER_CONNECTOR", "bad-inherited-value")
    calls = []
    monkeypatch.setattr(viewer_ctl, "_run_compose", lambda argv, env: calls.append((argv, env)) or 0)
    assert viewer_ctl.main(["start", "--owner", "alice", "--connector", "messages", "--state-dir", str(tmp_path / "state")]) == 0
    env = calls[0][1]
    assert env["KYREX_VIEWER_CONNECTOR"] == "messages"
    assert env["KYREX_VIEWER_BOT"] == "messages-connector"
    args = viewer_ctl.build_chromium_args("/profile", connector="messages")
    assert args[-1] == "https://messages.google.com/web/"
    assert not any("remote-debugging" in arg or "automation" in arg or arg == "--no-sandbox" for arg in args)
    assert viewer_ctl._active_env("alice", "calendar", 60)["KYREX_VIEWER_CONNECTOR"] == ""
    with pytest.raises(SystemExit): viewer_ctl.main(["start", "--owner", "alice", "--bot", "calendar", "--connector", "messages"])


def test_end_satisfies_compose_required_environment(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(viewer_ctl, "_run_compose", lambda argv, env: calls.append((argv, env)) or 0)
    assert viewer_ctl.main(["end", "--state-dir", str(tmp_path / "state")]) == 0
    assert calls[0][1]["KYREX_VIEWER_OWNER"]
    assert calls[0][1]["KYREX_VIEWER_BOT"]
    assert calls[0][1]["KYREX_VIEWER_VNC_PASSWORD_FILE"]


def test_hold_still_refuses_root(monkeypatch):
    monkeypatch.setattr(viewer_ctl.os, "getuid", lambda: 0)
    assert viewer_ctl.hold() == 4
