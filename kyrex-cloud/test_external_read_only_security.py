"""Focused security tests for external repository read-only execution."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "kyrex_engine"))

import git_workflow
from kyrex.toolbox import ToolBox


def test_repository_identity_is_exact_and_canonical(monkeypatch):
    monkeypatch.setenv(
        "KYREX_EXTERNAL_REPO_ALLOWLIST",
        '["https://GitHub.com/Acme/Widget.git"]',
    )

    assert (
        git_workflow.canonical_repository_identity(
            "https://github.com/acme/widget"
        )
        == "github.com/acme/widget"
    )
    assert git_workflow.is_allowlisted_external_repo(
        "git@github.com:ACME/WIDGET.git"
    )
    assert not git_workflow.is_allowlisted_external_repo(
        "https://github.com/acme/widget-fork"
    )
    assert not git_workflow.is_allowlisted_external_repo(
        "https://github.com/acme/widget/issues"
    )
    assert not git_workflow.is_allowlisted_external_repo(
        "https://github.com.evil/acme/widget"
    )


def test_headless_external_read_only_child_has_no_github_token(tmp_path, monkeypatch):
    captured = {}

    class FakeProcess:
        stdin = MagicMock()
        stdout = []
        stderr = []

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    from headless_agent import HeadlessAgent

    monkeypatch.setenv("GITHUB_TOKEN", "secret")

    agent = HeadlessAgent(
        Path("bridge.py"),
        tmp_path,
        read_only=True,
    )

    with patch("headless_agent.subprocess.Popen", side_effect=fake_popen):
        with patch.object(agent, "_send"):
            agent.out_q.put(("stdout", '{"type":"phase","value":"IDLE"}'))
            assert agent.start("read")

    assert captured["env"].get("KYREX_READ_ONLY_REPO") == "1"
    assert "GITHUB_TOKEN" not in captured["env"]


def test_read_only_commit_and_pr_guards(tmp_path):
    with patch("git_workflow.run_git") as run_git:
        assert not git_workflow.commit_and_push(
            tmp_path,
            "main",
            "task",
            "https://github.com/a/b",
            "secret",
            True,
        )
        run_git.assert_not_called()

    result = git_workflow.open_pull_request(
        "https://github.com/a/b",
        "main",
        "main",
        "task",
        "done",
        "secret",
        read_only=True,
    )
    assert result["skipped"]


def test_search_and_list_reject_escape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    box = ToolBox(MagicMock())
    outside = tmp_path.parent

    assert "SECURITY BLOCK" in box.search(
        ".*",
        str(outside),
    )["error"]

    assert "SECURITY BLOCK" in box.list_local_files(
        str(outside),
    )["error"]


def test_read_only_bwrap_mount_is_read_only(monkeypatch):
    box = ToolBox(MagicMock())

    monkeypatch.setenv("KYREX_READ_ONLY_REPO", "1")
    monkeypatch.setenv("WORKSPACE_ROOT", os.getcwd())

    with patch(
        "kyrex.toolbox.shutil.which",
        return_value="/usr/bin/bwrap",
    ), patch(
        "kyrex.toolbox.subprocess.run"
    ) as run:
        run.return_value = SimpleNamespace(
            stdout="",
            stderr="",
            returncode=0,
        )

        box.run_command("true")

        args = run.call_args.args[0]
        assert "--ro-bind" in args

        workspace_index = args.index(os.getcwd()) - 1

        assert args[workspace_index] == "--ro-bind"
        assert args[workspace_index + 1] == os.getcwd()
