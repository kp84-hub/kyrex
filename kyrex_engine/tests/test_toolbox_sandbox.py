"""Sandbox hardening tests for run_command and search.

Covers the security-review findings that PR #71's first pass did not reach:

  - spawned commands receive a scrubbed (allowlist) environment in BOTH
    execution paths: inside bwrap via --clearenv/--setenv, and in the
    unsandboxed fallback via subprocess env= (findings #1 and #4)
  - cloud mode (KYREX_SURFACE=cloud) refuses to execute when bwrap is
    missing instead of degrading to an unsandboxed shell with the worker's
    secret environment (finding #1)
  - an approved rm/rmdir/unlink is re-checked for workspace containment
    immediately before execution — the deletion gate only displays paths
    and headless auto-approvers approve everything (finding #2)
  - search() skips symlinked files that resolve outside the workspace
    (finding #5)
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kyrex.toolbox import ToolBox, _sandbox_env, _SANDBOX_ENV_ALLOWLIST


@pytest.fixture
def local_workspace(monkeypatch):
    """Pin the workspace plumbing to the test cwd.

    The cloud harness runs engines with KYREX_SURFACE=cloud and a
    PROJECT_SOURCE_ROOT that is the repo root — neither of which a unit
    test should inherit. These tests are about the local code paths, so
    they pin WORKSPACE_ROOT/PROJECT_SOURCE_ROOT to cwd and clear the
    cloud surface flag.
    """
    cwd = os.getcwd()
    monkeypatch.setenv("WORKSPACE_ROOT", cwd)
    monkeypatch.setenv("PROJECT_SOURCE_ROOT", cwd)
    monkeypatch.delenv("KYREX_SURFACE", raising=False)
    monkeypatch.delenv("KYREX_READ_ONLY_REPO", raising=False)
    return cwd


class TestSandboxEnv:
    """Test the allowlist-based environment scrubber."""

    def test_scrubs_secret_variables(self, monkeypatch):
        """Known secret names must never survive scrubbing."""
        for name in ("KYREX_API_KEY", "TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN",
                     "AWS_SECRET_ACCESS_KEY"):
            monkeypatch.setenv(name, f"secret-{name}")
        monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))

        scrubbed = _sandbox_env()

        assert "PATH" in scrubbed
        for name in ("KYREX_API_KEY", "TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN",
                     "AWS_SECRET_ACCESS_KEY"):
            assert name not in scrubbed

    def test_keeps_allowlisted_operational_variables(self, monkeypatch):
        """Workspace plumbing and mode flags must survive scrubbing."""
        monkeypatch.setenv("WORKSPACE_ROOT", "/somewhere/ws")
        monkeypatch.setenv("PROJECT_SOURCE_ROOT", "/somewhere/src")
        monkeypatch.setenv("KYREX_READ_ONLY_REPO", "1")
        monkeypatch.setenv("KYREX_SURFACE", "cloud")
        monkeypatch.setenv("KYREX_VSCODE", "1")

        scrubbed = _sandbox_env()

        assert scrubbed["WORKSPACE_ROOT"] == "/somewhere/ws"
        assert scrubbed["PROJECT_SOURCE_ROOT"] == "/somewhere/src"
        assert scrubbed["KYREX_READ_ONLY_REPO"] == "1"
        assert scrubbed["KYREX_SURFACE"] == "cloud"
        assert scrubbed["KYREX_VSCODE"] == "1"

    def test_unknown_variables_are_dropped(self, monkeypatch):
        """Allowlist semantics: anything not listed is dropped by default."""
        monkeypatch.setenv("KYREX_BRAND_NEW_VARIABLE", "x")
        assert "KYREX_BRAND_NEW_VARIABLE" not in _sandbox_env()

    def test_allowlist_has_no_secret_shaped_entries(self):
        """Guard the guardrail: the allowlist itself must not grow secrets."""
        secret_shaped = ("TOKEN", "KEY", "SECRET", "PASSWORD")
        for entry in _SANDBOX_ENV_ALLOWLIST:
            upper = entry.upper()
            assert not any(s in upper for s in secret_shaped), entry


@pytest.fixture
def toolbox():
    return ToolBox(MagicMock())


class TestRunCommandEnvScrubbing:
    """Both execution paths must hand the command a scrubbed environment."""

    def test_unsandboxed_fallback_gets_scrubbed_env(self, toolbox, local_workspace, monkeypatch):
        """Without bwrap the command still must not see secrets (env=)."""
        monkeypatch.setenv("KYREX_API_KEY", "sk-super-secret-value")

        with patch("kyrex.toolbox.shutil.which", return_value=None):
            result = toolbox.run_command("env")

        assert result["status"] == "ok"
        assert "sk-super-secret-value" not in result["output"]
        assert "PATH=" in result["output"]

    def test_bwrap_invocation_clearenv_plus_setenv_allowlist(self, toolbox, local_workspace, monkeypatch):
        """The sandbox starts empty and only allowlisted vars are handed in."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_super-secret-value")

        fake_completed = MagicMock(returncode=0, stdout="hi", stderr="")
        with patch("kyrex.toolbox.shutil.which", return_value="/usr/bin/bwrap"), \
             patch("kyrex.toolbox.subprocess.run", return_value=fake_completed) as run:
            result = toolbox.run_command("echo hi")

        assert result["status"] == "ok"
        argv = run.call_args.args[0]
        assert isinstance(argv, list)
        assert "--clearenv" in argv
        # Python images keep the interpreter in /usr/local — the sandbox must
        # bind it in wherever it exists, or cloud commands lose python.
        if Path("/usr/local").exists():
            # bind appears as: --ro-bind /usr/local /usr/local
            assert argv.count("/usr/local") == 2
        # The token must not cross the sandbox boundary via argv either.
        assert "ghp_super-secret-value" not in " ".join(str(a) for a in argv)
        # PATH is allowlisted, so it must be set inside the sandbox.
        setenv_pairs = {
            argv[i + 1]: argv[i + 2]
            for i, a in enumerate(argv) if a == "--setenv"
        }
        assert "PATH" in setenv_pairs
        assert "GITHUB_TOKEN" not in setenv_pairs
        # And the bwrap process itself is spawned with the scrubbed env.
        assert "GITHUB_TOKEN" not in run.call_args.kwargs.get("env", {})
        assert run.call_args.kwargs.get("shell") is False


class TestCloudRequiresBwrap:
    """Cloud mode must refuse unsandboxed execution, not degrade silently."""

    def test_cloud_mode_refuses_without_bwrap(self, toolbox, monkeypatch):
        monkeypatch.setenv("KYREX_SURFACE", "cloud")

        def _explode(*a, **kw):
            raise AssertionError("subprocess.run must not be called in cloud mode without bwrap")

        with patch("kyrex.toolbox.shutil.which", return_value=None), \
             patch("kyrex.toolbox.subprocess.run", side_effect=_explode):
            result = toolbox.run_command("echo hi")

        assert "error" in result
        assert "requires bwrap" in result["error"]

    def test_local_mode_still_works_without_bwrap(self, toolbox, monkeypatch):
        """Local development without bwrap keeps working (scrubbed env)."""
        monkeypatch.delenv("KYREX_SURFACE", raising=False)

        with patch("kyrex.toolbox.shutil.which", return_value=None):
            result = toolbox.run_command("echo hello")

        assert result["status"] == "ok"
        assert "hello" in result["output"]


class TestDeletionContainment:
    """An approved deletion is consent, not containment — re-verify targets."""

    @pytest.fixture
    def auto_approved(self, toolbox, local_workspace, monkeypatch):
        """Simulate the headless cloud auto-approver: everything is approved."""
        monkeypatch.setattr("kyrex.toolbox._is_interactive", lambda: True)
        monkeypatch.setattr(toolbox, "_propose_deletion", lambda command: True)
        return toolbox

    def test_approved_rm_outside_workspace_is_blocked(self, auto_approved, tmp_path):
        """rm of a workspace-external path must fail after approval."""
        victim = tmp_path / "victim-outside.txt"
        victim.write_text("do not delete")

        result = auto_approved.run_command(f"rm {victim}")

        assert "error" in result
        assert "outside the workspace" in result["error"]
        assert victim.exists()

    def test_approved_git_rm_outside_workspace_is_blocked(self, auto_approved):
        result = auto_approved.run_command("git rm /etc/passwd")
        assert "error" in result
        assert "outside the workspace" in result["error"]

    def test_approved_rm_inside_workspace_executes(self, auto_approved, local_workspace):
        """Workspace-relative deletions keep working when approved."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         dir=os.getcwd(), delete=False) as f:
            f.write("bye")
            inside = f.name

        result = auto_approved.run_command(f"rm {inside}")

        assert result["status"] == "ok"
        assert not Path(inside).exists()

    def test_approved_rm_with_unverifiable_targets_fails_closed(self, auto_approved):
        """A direct rm whose targets cannot be parsed must not run at all."""
        result = auto_approved.run_command("rm -rf")
        assert "error" in result
        assert "no target paths could be verified" in result["error"]

    def test_indirect_deletion_keeps_gate_but_no_static_containment(self, auto_approved, local_workspace, tmp_path):
        """find -delete stays gate-only (documented limitation, not a regression)."""
        victim = tmp_path / "indirect-victim.txt"
        victim.write_text("x")

        result = auto_approved.run_command(f"find {tmp_path} -name 'indirect-victim.txt' -delete")

        assert "Deletion blocked" not in str(result)
        assert result["status"] == "ok"

    def test_rejected_deletion_still_cancelled(self, toolbox, local_workspace, monkeypatch):
        """Without approval nothing changed: deletion is cancelled."""
        monkeypatch.setattr("kyrex.toolbox._is_interactive", lambda: True)
        monkeypatch.setattr(toolbox, "_propose_deletion", lambda command: False)

        result = toolbox.run_command("rm some-file.txt")

        assert "error" in result
        assert "cancelled" in result["error"].lower()


class TestSearchSymlinkContainment:
    """search() must not read through symlinks pointing outside the tree."""

    @pytest.fixture
    def scanned_tree(self, local_workspace):
        """A scanned dir containing a real file and an escaping symlink."""
        outside = Path(tempfile.gettempdir()) / "kyrex-search-outside-canary.txt"
        outside.write_text("CANARY_OUTSIDE_4817")
        with tempfile.TemporaryDirectory(dir=local_workspace) as tmpdir:
            Path(tmpdir, "real.txt").write_text("CANARY_IN_TREE_7391")
            link = Path(tmpdir, "escape.txt")
            os.symlink(outside, link)
            yield tmpdir, outside
        outside.unlink(missing_ok=True)

    @pytest.fixture
    def toolbox(self):
        return ToolBox(MagicMock())

    def test_search_does_not_leak_symlinked_outside_content(self, toolbox, scanned_tree):
        tmpdir, _ = scanned_tree
        result = toolbox.search("CANARY_OUTSIDE_4817", path=tmpdir)
        assert result["status"] == "ok"
        assert result["results"] == []

    def test_search_still_finds_real_files_in_tree(self, toolbox, scanned_tree):
        tmpdir, _ = scanned_tree
        result = toolbox.search("CANARY_IN_TREE_7391", path=tmpdir)
        assert result["status"] == "ok"
        assert len(result["results"]) == 1
        assert "real.txt" in result["results"][0]

    def test_search_result_paths_stay_inside_tree(self, toolbox, scanned_tree):
        tmpdir, _ = scanned_tree
        result = toolbox.search("CANARY", path=tmpdir)
        assert all("/tmp/" not in r.split(":")[0] or "escape" not in r
                   for r in result["results"])
