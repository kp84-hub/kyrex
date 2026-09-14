"""Profile-isolation tests for ``browser-host/profiles.py``.

These are the on-disk half of the Phase-1 guarantee: distinct ``(owner,
bot_id)`` pairs must never resolve to the same profile directory, and no input
may escape the profiles root.

Run: python3 -m pytest test_profiles.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profiles  # noqa: E402

ROOT = "/tmp/kx-browser-host-profiles"


def test_distinct_bots_are_isolated():
    a = profiles.profile_dir("me", "b1", root=ROOT)
    b = profiles.profile_dir("me", "b2", root=ROOT)
    assert a != b


def test_distinct_owners_are_isolated():
    a = profiles.profile_dir("alice", "b1", root=ROOT)
    b = profiles.profile_dir("bob", "b1", root=ROOT)
    assert a != b


def test_per_bot_profile_is_not_the_shared_profile():
    assert profiles.profile_dir("me", "b1", root=ROOT) != (
        profiles.shared_profile_path(ROOT)
    )


def test_shared_profile_is_opt_in():
    assert profiles.profile_dir("me", "b1", root=ROOT, shared=True) == (
        profiles.shared_profile_path(ROOT)
    )


def test_missing_isolation_key_is_refused():
    with pytest.raises(ValueError):
        profiles.profile_dir("", "b1", root=ROOT)
    with pytest.raises(ValueError):
        profiles.profile_dir("me", "", root=ROOT)


def test_traversal_cannot_escape_the_root():
    path = profiles.profile_dir("../../etc", "..", root=ROOT)
    assert ".." not in path.parts
    assert path.is_relative_to(ROOT)


def test_ensure_profile_creates_the_directory(tmp_path):
    created = profiles.ensure_profile("me", "b1", root=str(tmp_path))
    assert created.is_dir()
    assert created.is_relative_to(str(tmp_path))


def test_env_overrides_the_root(monkeypatch):
    monkeypatch.setenv("KYREX_BROWSER_PROFILES_ROOT", "/custom/root")
    expected = profiles.profiles_root() / "bot-b1" / "owner-me"
    assert profiles.profile_dir("me", "b1") == expected
