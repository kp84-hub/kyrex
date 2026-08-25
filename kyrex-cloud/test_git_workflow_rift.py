"""Tests for the persistent-Rift mode added to git_workflow.prepare_workspace
(Step 1 of the KBot milestone).

Covers:
  - Empty --rift gets cloned/initialized from --repo-url.
  - cleanup() does NOT remove the persistent Rift.
  - A second run reuses the existing Rift rather than cloning a new /tmp
    workspace.
  - Existing temporary-workspace behaviour (no --rift) remains unchanged.
  - Existing --local-repo (worktree) behaviour remains unchanged.
  - INTEGRATION/REGRESSION: two consecutive repo runs bound to the same Bot's
    Rift see the state left by the previous run.

All cases use a local repo as the clone source so no network is required.

Run: python3 test_git_workflow_rift.py
"""
import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import git_workflow

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Helpers ────────────────────────────────────────────────────────────────

def _args(**kw):
    ns = argparse.Namespace()
    ns.local_repo = kw.get("local_repo")
    ns.rift = kw.get("rift")
    ns.repo_url = kw.get("repo_url")
    ns.token = kw.get("token")
    ns.base = kw.get("base", "main")
    ns.workdir_root = kw.get("workdir_root", "/tmp")
    ns.keep_workdir = kw.get("keep_workdir", False)
    return ns


def _make_src_repo(td):
    """Create a small local git repo (with an origin remote) to clone from."""
    src = os.path.join(td, "src")
    os.makedirs(src)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", src],
                   check=True)
    subprocess.run(["git", "-C", src, "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", src, "config", "user.name", "t"], check=True)
    with open(os.path.join(src, "seed.txt"), "w") as f:
        f.write("seed\n")
    subprocess.run(["git", "-C", src, "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", src, "commit", "-q", "-m", "init"], check=True)
    # Give it an origin remote so --local-repo mode (which reads origin) works.
    subprocess.run(["git", "-C", src, "remote", "add", "origin", src],
                   check=True)
    return src


def _is_git_repo(path):
    if not os.path.isdir(path):
        return False
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"],
        capture_output=True, text=True)
    return proc.returncode == 0


# ── Test 1: Empty --rift gets cloned/initialized ────────────────────────────
print("Test 1: Empty --rift gets cloned/initialized from --repo-url")

with tempfile.TemporaryDirectory() as td:
    src = _make_src_repo(td)
    rift = os.path.join(td, "rift")
    args = _args(rift=rift, repo_url=src, base="main")
    workdir, remote_url, cleanup = git_workflow.prepare_workspace(args, "kyrex/run1")

    check("workdir is the rift path",
          os.path.realpath(workdir) == os.path.realpath(rift),
          f"workdir={workdir} rift={rift}")
    check("rift now contains the cloned repo",
          os.path.exists(os.path.join(rift, "seed.txt")))
    check("rift is a git repository",
          _is_git_repo(rift))
    check("remote_url is the clean (non-token) repo url",
          remote_url == src,
          f"got {remote_url!r}")


# ── Test 2: cleanup() does NOT remove the persistent Rift ───────────────────
print("\nTest 2: cleanup() does NOT remove the persistent Rift")

with tempfile.TemporaryDirectory() as td:
    src = _make_src_repo(td)
    rift = os.path.join(td, "rift")
    args = _args(rift=rift, repo_url=src, base="main")
    workdir, remote_url, cleanup = git_workflow.prepare_workspace(args, "kyrex/run1")

    # Simulate state left behind by the run.
    marker = os.path.join(rift, "run1-state.txt")
    with open(marker, "w") as f:
        f.write("left by run 1")
    cleanup()

    check("cleanup did NOT remove the persistent Rift",
          os.path.isdir(rift),
          f"rift gone: {rift}")
    check("state left in the Rift survives cleanup",
          os.path.exists(marker),
          f"marker gone: {marker}")


# ── Test 3: A second run reuses the existing Rift ───────────────────────────
print("\nTest 3: A second run reuses the existing Rift (no new /tmp workspace)")

with tempfile.TemporaryDirectory() as td:
    src = _make_src_repo(td)
    rift = os.path.join(td, "rift")
    args1 = _args(rift=rift, repo_url=src, base="main")
    wd1, ru1, cleanup1 = git_workflow.prepare_workspace(args1, "kyrex/run1")
    marker = os.path.join(rift, "run1-state.txt")
    with open(marker, "w") as f:
        f.write("left by run 1")
    cleanup1()

    args2 = _args(rift=rift, repo_url=src, base="main")
    wd2, ru2, cleanup2 = git_workflow.prepare_workspace(args2, "kyrex/run2")
    check("second run reuses the same persistent Rift workdir",
          os.path.realpath(wd2) == os.path.realpath(rift),
          f"wd2={wd2} rift={rift}")
    check("second run does NOT use a fresh /tmp/kyrex-task-* workspace",
          not os.path.basename(os.path.realpath(wd2)).startswith("kyrex-task-"),
          f"wd2={wd2}")
    check("state left by run 1 is still visible in run 2",
          os.path.exists(marker),
          f"marker gone: {marker}")
    cleanup2()
    check("second cleanup also does NOT remove the Rift",
          os.path.isdir(rift))


# ── Test 4: Existing temporary-workspace behaviour (no --rift) unchanged ────
print("\nTest 4: Temporary workspace (no --rift) behaves as before")

with tempfile.TemporaryDirectory() as td:
    src = _make_src_repo(td)
    wr = os.path.join(td, "workspaces")
    os.makedirs(wr)
    args = _args(repo_url=src, base="main", workdir_root=wr)
    workdir, remote_url, cleanup = git_workflow.prepare_workspace(args, "kyrex/tmp")

    check("temp workdir is created under workdir_root",
          str(workdir).startswith(wr),
          f"workdir={workdir}")
    check("temp workdir cloned the repo",
          os.path.exists(os.path.join(workdir, "seed.txt")))
    check("temp mode did not touch any --rift",
          args.rift is None)
    check("temp remote_url is the repo url",
          remote_url == src,
          f"got {remote_url!r}")
    cleanup()
    check("temp cleanup removes the workspace",
          not os.path.exists(workdir),
          f"workdir still exists: {workdir}")


# ── Test 5: Existing --local-repo (worktree) behaviour unchanged ────────────
print("\nTest 5: --local-repo (worktree) behaviour unchanged")

with tempfile.TemporaryDirectory() as td:
    src = _make_src_repo(td)
    wr = os.path.join(td, "wtroot")
    os.makedirs(wr)
    args = _args(local_repo=src, base="main", workdir_root=wr)
    workdir, remote_url, cleanup = git_workflow.prepare_workspace(args, "kyrex/wt")

    check("local-repo workdir is created under workdir_root",
          str(workdir).startswith(wr),
          f"workdir={workdir}")
    check("local-repo remote_url comes from origin",
          remote_url == src,
          f"got {remote_url!r}")
    cleanup()
    check("local-repo cleanup removes the worktree",
          not os.path.exists(workdir),
          f"worktree still exists: {workdir}")


# ── Integration/regression: two consecutive runs see prior-state ────────────
print("\nIntegration: two consecutive repo runs bound to the same Bot's Rift\n"
      "             see state left by the previous run")

with tempfile.TemporaryDirectory() as td:
    src = _make_src_repo(td)
    rift = os.path.join(td, "bot-rift")

    # Run 1: empty rift -> cloned, agent leaves committed state (as
    # commit_and_push would), then cleanup must NOT wipe the rift.
    args1 = _args(rift=rift, repo_url=src, base="main")
    wd1, ru1, cleanup1 = git_workflow.prepare_workspace(args1, "kyrex/run1")
    artifact = os.path.join(rift, "run1-artifact.txt")
    with open(artifact, "w") as f:
        f.write("produced by run 1\n")
    subprocess.run(["git", "-C", rift, "add", "run1-artifact.txt"], check=True)
    # commit_and_push sets author identity via -c; replicate that here.
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                     "-C", rift, "commit", "-q", "-m", "run1"], check=True)
    cleanup1()
    check("Rift persists after run 1 cleanup",
          os.path.isdir(rift))

    # Run 2: reuse the same persistent Rift — its working tree was NOT reset,
    # so the artifact committed by run 1 is still present.
    args2 = _args(rift=rift, repo_url=src, base="main")
    wd2, ru2, cleanup2 = git_workflow.prepare_workspace(args2, "kyrex/run2")
    check("run 2 workdir is the same persistent Rift",
          os.path.realpath(wd2) == os.path.realpath(rift),
          f"wd2={wd2} rift={rift}")
    check("run 2 sees the artifact committed by run 1",
          os.path.exists(artifact),
          f"artifact gone: {artifact}")
    cleanup2()
    check("run 2 cleanup still does NOT remove the Rift",
          os.path.isdir(rift))


print("\n" + ("ALL TESTS PASSED" if not failures
              else f"{len(failures)} FAILURE(S): {failures}"))
sys.exit(1 if failures else 0)
