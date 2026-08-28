"""Fail-closed repo-write policy: own repo writable, everything else read-only."""
import importlib
import git_workflow as g


def test_own_repo_is_writable(monkeypatch):
    monkeypatch.setenv("KYREX_TARGET_REPO_URL", "https://github.com/kp84-hub/kyrex.git")
    importlib.reload(g)
    assert g.is_own_repo("https://github.com/kp84-hub/kyrex.git") is True


def test_unknown_external_is_read_only():
    assert g.is_own_repo("https://github.com/someone/other") is False


def test_garbage_url_is_read_only():
    assert g.is_own_repo("not-a-url") is False


def test_empty_is_read_only():
    assert g.is_own_repo("") is False


def test_target_repo_override(monkeypatch):
    monkeypatch.setenv("KYREX_TARGET_REPO_URL", "https://github.com/me/thing.git")
    importlib.reload(g)
    assert g.is_own_repo("https://github.com/me/thing.git") is True
    assert g.is_own_repo("https://github.com/kp84-hub/kyrex.git") is False
    monkeypatch.delenv("KYREX_TARGET_REPO_URL", raising=False)
    importlib.reload(g)
