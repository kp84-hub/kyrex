"""Persistent Developer Bot repositories must not silently run on stale main."""
import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import git_workflow


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class RiftFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src = root / "source"
        self.rift = root / "rift"
        self.src.mkdir()
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q",
                        str(self.src)], check=True)
        for repo in (self.src,):
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.invalid")
        self.commit(self.src, "seed", "first")
        self.args = argparse.Namespace(
            local_repo=None, rift=str(self.rift), repo_url=str(self.src),
            token=None, base="main", workdir_root=str(root), keep_workdir=True)
        git_workflow.prepare_workspace(self.args, "kyrex/first")
        git(self.rift, "config", "user.name", "Test")
        git(self.rift, "config", "user.email", "test@example.invalid")

    def commit(self, repo, name, message):
        (repo / name).write_text(message, encoding="utf-8")
        git(repo, "add", name)
        git(repo, "commit", "-m", message)

    def test_clean_rift_fast_forwards_to_new_remote_main(self):
        self.commit(self.src, "new", "second")
        git_workflow.prepare_workspace(self.args, "kyrex/second")
        self.assertEqual(git(self.rift, "rev-parse", "HEAD"),
                         git(self.src, "rev-parse", "HEAD"))
        self.assertEqual(git(self.rift, "rev-parse", "origin/main"),
                         git(self.src, "rev-parse", "HEAD"))

    def test_failed_fetch_does_not_use_cached_base(self):
        git(self.rift, "remote", "set-url", "origin", str(self.rift / "missing"))
        with self.assertRaisesRegex(RuntimeError, "Could not refresh"):
            git_workflow.prepare_workspace(self.args, "kyrex/second")

    def test_dirty_rift_behind_main_preserves_work(self):
        (self.rift / "notes").write_text("keep me", encoding="utf-8")
        original_head = git(self.rift, "rev-parse", "HEAD")
        self.commit(self.src, "new", "second")
        with self.assertRaisesRegex(RuntimeError, "uncommitted work"):
            git_workflow.prepare_workspace(self.args, "kyrex/second")
        self.assertEqual(git(self.rift, "rev-parse", "HEAD"), original_head)
        self.assertEqual((self.rift / "notes").read_text(), "keep me")

    def test_diverged_branch_requires_review(self):
        self.commit(self.rift, "local", "local")
        self.commit(self.src, "remote", "remote")
        with self.assertRaisesRegex(RuntimeError, "diverged"):
            git_workflow.prepare_workspace(self.args, "kyrex/second")
        self.assertTrue((self.rift / "local").exists())

    def test_agent_refresh_keeps_untracked_notes_and_branch(self):
        (self.rift / "DEV_BOT_SMOKE_TEST.md").write_text("keep both lines\n")
        branch = git(self.rift, "branch", "--show-current")
        self.commit(self.src, "install-button", "new app feature")
        state = git_workflow.refresh_workspace_agent(self.rift, "main", None)
        self.assertEqual(state["status"], "updated")
        self.assertEqual(git(self.rift, "rev-parse", "HEAD"), state["remote_head"])
        self.assertEqual(git(self.rift, "branch", "--show-current"), branch)
        self.assertEqual((self.rift / "DEV_BOT_SMOKE_TEST.md").read_text(), "keep both lines\n")
        self.assertTrue((self.rift / "install-button").exists())

    def test_agent_refresh_preserves_tracked_changes_and_index(self):
        (self.rift / "seed").write_text("staged work")
        git(self.rift, "add", "seed")
        (self.rift / "seed").write_text("unstaged work")
        before = git_workflow.workspace_fingerprint(self.rift)
        self.commit(self.src, "new", "remote addition")
        state = git_workflow.refresh_workspace_agent(self.rift, "main", None)
        self.assertEqual(state["status"], "behind")
        self.assertEqual(git_workflow.workspace_fingerprint(self.rift), before)
        self.assertEqual(git(self.rift, "rev-parse", "origin/main"), git(self.src, "rev-parse", "HEAD"))

    def test_agent_refresh_preserves_divergence(self):
        self.commit(self.rift, "local", "local commit")
        self.commit(self.src, "remote", "remote commit")
        before = git_workflow.workspace_fingerprint(self.rift)
        state = git_workflow.refresh_workspace_agent(self.rift, "main", None)
        self.assertEqual(state["status"], "diverged")
        self.assertEqual(git_workflow.workspace_fingerprint(self.rift), before)
        self.assertTrue((self.rift / "local").exists())

    def test_owner_can_start_from_latest_without_losing_the_old_branch(self):
        self.commit(self.rift, "local", "old calendar change")
        old_branch = git(self.rift, "branch", "--show-current")
        old_head = git(self.rift, "rev-parse", "HEAD")
        self.commit(self.src, "remote", "current base")
        self.args.read_only = False
        self.args.task = 'Start from latest main'
        (self.rift / 'notes').write_text('keep')
        refused = git_workflow.run_workspace_agent(self.args, None, lambda _: None)
        self.assertIn('uncommitted files', refused['errors'][0])
        self.assertEqual(git(self.rift, 'rev-parse', 'HEAD'), old_head)
        (self.rift / 'notes').unlink()

        result = git_workflow.run_workspace_agent(self.args, None, lambda _: None)
        self.assertEqual(result['status'], 'no_changes')
        self.assertIn('The previous branch is still available', result['final_response'])
        self.assertEqual(git(self.rift, 'rev-parse', old_branch), old_head)
        self.assertEqual(git(self.rift, 'rev-parse', 'HEAD'), git(self.src, 'rev-parse', 'HEAD'))
        self.assertTrue((self.rift / 'remote').exists())
        self.assertFalse((self.rift / 'local').exists())

    def test_agent_refresh_preserves_untracked_collision(self):
        (self.rift / "new").write_text("local notes")
        before = git_workflow.workspace_fingerprint(self.rift)
        self.commit(self.src, "new", "remote file")
        state = git_workflow.refresh_workspace_agent(self.rift, "main", None)
        self.assertEqual(state["status"], "behind")
        self.assertEqual(git_workflow.workspace_fingerprint(self.rift), before)

    def test_agent_refresh_failure_is_explicit_and_nonblocking(self):
        git(self.rift, "remote", "set-url", "origin", str(self.rift / "missing"))
        before = git_workflow.workspace_fingerprint(self.rift)
        state = git_workflow.refresh_workspace_agent(self.rift, "main", None)
        self.assertEqual(state["status"], "unverified")
        self.assertNotIn("remote_head", state)
        self.assertEqual(git_workflow.workspace_fingerprint(self.rift), before)

    def test_explicit_start_then_work_runs_on_new_base_and_preserves_old_branch(self):
        self.commit(self.rift, 'local', 'earlier local feature')
        old_branch = git(self.rift, 'branch', '--show-current')
        old_head = git(self.rift, 'rev-parse', 'HEAD')
        self.commit(self.src, 'remote', 'latest remote feature')
        args = self.args
        args.read_only = False
        args.task = 'Start from latest main, then fix the parser'
        args.python = 'python3'
        args.startup_timeout = args.idle_timeout = args.overall_timeout = 1
        seen = []

        class Agent:
            chat_done_seen = terminal = True
            outcome = 'complete'
            execution_error = False
            approvals = []; tool_calls = []; errors = []
            final_response = 'Prepared the parser fix for review.'
            def __init__(agent, bridge, root, **kwargs):
                self.assertFalse(kwargs['read_only'])
                self.assertEqual(git(root, 'rev-parse', 'HEAD'), git(self.src, 'rev-parse', 'HEAD'))
            def start(agent, task):
                seen.append(task)
                return True
            def run(agent):
                (self.rift / 'parser.py').write_text('prepared fix')

        with patch.object(git_workflow, 'HeadlessAgent', Agent), \
             patch.object(git_workflow, 'commit_and_push', side_effect=AssertionError('no publication')), \
             patch.object(git_workflow, 'open_pull_request', side_effect=AssertionError('no publication')):
            result = git_workflow.run_workspace_agent(args, None, lambda _: None)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(seen, ['fix the parser'])
        self.assertTrue(result['has_changes'])
        self.assertEqual(git(self.rift, 'rev-parse', old_branch), old_head)
        self.assertNotEqual(git(self.rift, 'branch', '--show-current'), old_branch)
        self.assertEqual((self.rift / 'parser.py').read_text(), 'prepared fix')

    def test_start_then_work_cannot_switch_past_dirty_work(self):
        self.commit(self.rift, 'local', 'earlier feature')
        self.commit(self.src, 'remote', 'remote feature')
        (self.rift / 'notes').write_text('keep these notes')
        self.args.read_only = False
        self.args.task = 'Start from latest main and implement the new feature'
        before = git_workflow.workspace_fingerprint(self.rift)
        with patch.object(git_workflow, 'HeadlessAgent', side_effect=AssertionError('must not start')):
            result = git_workflow.run_workspace_agent(self.args, None, lambda _: None)
        self.assertEqual(result['status'], 'error')
        self.assertIn('uncommitted files', result['errors'][0])
        self.assertEqual(git_workflow.workspace_fingerprint(self.rift), before)

    def test_start_parser_requires_explicit_base_specific_instruction(self):
        for task, expected in [
            ('Start from latest main', ''),
            ('Please work from the latest main.', ''),
            ('Start from latest main and fix it', 'fix it'),
            ('Start from latest main, then fix it', 'fix it'),
            ('Start from latest main; and then test it', 'test it'),
            ('Start from latest main then inspect it', 'inspect it'),
            ('Should I start from latest main?', None),
            ('Do not start from latest main', None),
            ('Review the phrase "Start from latest main"', None),
            ('Start from latest mainline and fix it', None),
            ('Start from latest develop and fix it', None),
        ]:
            with self.subTest(task=task):
                self.assertEqual(git_workflow.workspace_start_request(task, 'main'), expected)


if __name__ == "__main__":
    unittest.main()
