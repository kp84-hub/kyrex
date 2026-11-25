"""Reproduce rejected Git auth without live accounts or weakening isolation."""
import subprocess
from unittest.mock import MagicMock

import pytest

import kyrex.toolbox as tools


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("KYREX_READ_ONLY_REPO", raising=False)
    monkeypatch.delenv("KYREX_TOOL_TIMEOUT", raising=False)
    monkeypatch.setattr(tools, "_bwrap_functional", lambda *args: False)
    monkeypatch.setattr(tools, "_changed_snapshot", lambda *args: None)
    return tools.ToolBox(MagicMock())


@pytest.mark.parametrize("message", [
    "remote: Invalid username or token.\nfatal: Authentication failed for 'https://github.com/example/private.git/'",
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    "fatal: could not read Password for 'https://github.com': terminal prompts disabled",
    "git@github.com: Permission denied (publickey).",
])
def test_rejected_auth_is_failed_with_bounded_recovery(runner, monkeypatch, message):
    monkeypatch.setattr(tools.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 128, "", message))
    result = runner.run_command("git fetch origin")
    assert result["status"] == "error" and result["returncode"] == 128
    assert result["error_type"] == "git_authentication"
    assert result["retryable"] is False
    assert "owner's normal terminal" in result["recovery"]
    assert "Do not assume the token expired" in result["recovery"]
    assert message in result["output"]


def test_missing_sandbox_keeps_secrets_scrubbed_and_reports_only_presence(runner, monkeypatch):
    marker = "inert-local-auth-fixture"
    monkeypatch.setenv("GITHUB_TOKEN", marker)
    monkeypatch.setenv("GH_TOKEN", marker)
    monkeypatch.setenv("OPENAI_API_KEY", marker)
    calls = []
    def command(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 128, "", "fatal: Authentication failed")
    monkeypatch.setattr(tools.subprocess, "run", command)
    result = runner.run_command("git fetch origin")
    assert len(calls) == 1  # No automatic retry or credential reads.
    assert all(key not in calls[0]["env"] for key in ("GITHUB_TOKEN", "GH_TOKEN", "OPENAI_API_KEY"))
    assert result["execution_context"] == {
        "sandboxed": False, "github_token_environment_filtered": True}
    assert marker not in str(result)


def test_working_sandbox_keeps_existing_policy_and_diagnoses_network(runner, monkeypatch):
    monkeypatch.setattr(tools, "_bwrap_functional", lambda *args: True)
    monkeypatch.setattr(tools.shutil, "which", lambda *args: "/usr/bin/bwrap")
    calls = []
    def command(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 128, "", "fatal: unable to access: Could not resolve host: github.com")
    monkeypatch.setattr(tools.subprocess, "run", command)
    result = runner.run_command("git fetch origin")
    assert result["error_type"] == "sandbox_network_unavailable"
    assert result["execution_context"]["sandboxed"] is True
    assert "--unshare-all" in calls[0] and "--share-net" not in calls[0]
    assert "host terminal" in result["recovery"]


def test_host_dns_failure_is_not_misdiagnosed_as_a_sandbox_or_token_problem(runner, monkeypatch):
    monkeypatch.setattr(tools.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 128, "", "Could not resolve host: github.com"))
    result = runner.run_command("git fetch origin")
    assert result["error_type"] == "command_failed"


def test_auth_marker_after_truncation_is_still_diagnosed(runner, monkeypatch):
    monkeypatch.setattr(tools.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 128, "progress\n" * 2000, "fatal: Authentication failed"))
    result = runner.run_command("git fetch origin")
    assert "truncated" in result["output"]
    assert result["error_type"] == "git_authentication"


def test_failures_still_pass_through_write_review(runner, monkeypatch):
    monkeypatch.setattr(tools.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 1, "", "failed build"))
    review = MagicMock(return_value={"error": "Write review declined"})
    monkeypatch.setattr(runner, "_gate_command_changes", review)
    assert runner.run_command("npm run build") == {"error": "Write review declined"}
    review.assert_called_once()


def test_real_shell_exit_is_not_a_success(runner):
    result = runner.run_command("exit 42")
    assert result["returncode"] == 42 and result["status"] == "error"
    assert result["error_type"] == "command_failed"


def test_successful_command_is_not_classified_from_log_text(runner, monkeypatch):
    monkeypatch.setattr(tools.subprocess, "run", lambda command, **kwargs:
        subprocess.CompletedProcess(command, 0, "old log: fatal: Authentication failed", ""))
    result = runner.run_command("git log -1")
    assert result["status"] == "ok" and "error" not in result
