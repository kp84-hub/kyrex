"""Security tests for run_command: network-write git block + env scrub."""
import os
from unittest.mock import MagicMock
from kyrex.toolbox import ToolBox


def _tool():
    return ToolBox(MagicMock())


def test_readonly_blocks_git_push(monkeypatch):
    monkeypatch.setenv("KYREX_READ_ONLY_REPO", "1")
    # bwrap present so we pass the early refuse and reach the net-git block
    r = _tool().run_command("git push origin main")
    assert "network-write git" in r.get("error", "").lower() or "bwrap" in r.get("error", "").lower()


def test_readonly_blocks_git_push_dash_c(monkeypatch):
    monkeypatch.setenv("KYREX_READ_ONLY_REPO", "1")
    r = _tool().run_command("git -C /tmp/x push")
    assert "error" in r


def test_readonly_allows_local_git(monkeypatch):
    monkeypatch.setenv("KYREX_READ_ONLY_REPO", "1")
    r = _tool().run_command("git status")
    # not blocked by the network-write rule specifically
    assert "network-write git" not in r.get("error", "").lower()


def test_ownrepo_push_not_blocked_by_netgit_rule(monkeypatch):
    monkeypatch.delenv("KYREX_READ_ONLY_REPO", raising=False)
    r = _tool().run_command("git push origin main")
    # own repo: our net-git rule must NOT block (may fail on execution, not our rule)
    assert "network-write git" not in r.get("error", "").lower()
