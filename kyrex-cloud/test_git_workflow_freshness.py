"""Persistent Developer Bot repositories must not silently run on stale main."""
import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
