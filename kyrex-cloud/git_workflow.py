#!/usr/bin/env python3
"""
git_workflow.py — Kyrex Cloud Agent, Phase 2.

Wraps Phase 0's HeadlessAgent with a real git workflow:
  1. Get an isolated working copy on a fresh branch (worktree off a local
     clone, or a full clone from a URL — either way, always cut from the
     latest fetched base branch, never a dirty leftover directory).
  2. Run the task through the same engine/protocol as Phase 0.
  3. If the agent produced changes: commit them, push the branch.
  4. Open a real PR via the GitHub REST API (skipped gracefully if no token).
  5. Write the result JSON *outside* the repo that was touched — Phase 0's
     "diff swallows last run's result file" bug is fixed by construction here:
     every run gets a brand-new branch off a freshly-fetched base, so there's
     nothing stale in the tree to pick up, and the summary never lands inside
     the repo it just described.

This intentionally reuses headless_agent.py's HeadlessAgent + find_bridge_script
rather than re-implementing the NDJSON protocol — same reasoning as not vendoring
a second copy of kyrex_engine: one source of truth for "how we talk to the engine."
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import shutil
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from headless_agent import HeadlessAgent, find_bridge_script  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def slugify(text: str, max_words: int = 6) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())[:max_words]
    slug = "-".join(words) or "task"
    return slug[:60]


def _no_prompt_env():
    """GIT_TERMINAL_PROMPT=0 makes git fail immediately with a clear stderr
    message when it would otherwise block on a username/password prompt —
    critical once this runs with no TTY attached (webhook/cron/Phase 3)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_git(repo_dir, *args, check=True):
    return subprocess.run(["git", "-C", str(repo_dir), *args],
                           capture_output=True, text=True, check=check,
                           env=_no_prompt_env())


def with_token(remote_url: str, token: str | None) -> str:
    """Embed a token into an https:// GitHub URL for a single authenticated
    operation, without ever writing it into a persisted remote config."""
    if not token or not remote_url.startswith("https://"):
        return remote_url
    host_part = remote_url.split("//", 1)[1].split("/", 1)[0]
    if "@" in host_part:
        return remote_url  # already has credentials embedded
    return remote_url.replace("https://", f"https://x-access-token:{token}@", 1)


def parse_owner_repo(remote_url: str):
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$", remote_url.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def get_diff_since_base(workdir: Path, base: str) -> str:
    result = run_git(workdir, "diff", f"origin/{base}..HEAD", check=False)
    return result.stdout


def review_diff(task: str, diff_text: str) -> dict:
    """Second-pass check: does the diff actually do what the task asked?

    Reuses the same provider config already set up for the main task
    (KYREX_PROVIDER / KYREX_API_KEY / KYREX_MODEL / OPENAI_BASE_URL) — no
    separate setup needed. Fails OPEN: if the review call itself can't
    complete (bad config, network hiccup), that's reported as unavailable,
    not as a failed review — a broken review step should never become the
    reason a real PR doesn't open.
    """
    provider = os.environ.get("KYREX_PROVIDER", "openai")
    model = os.environ.get("KYREX_MODEL")
    api_key = os.environ.get("KYREX_API_KEY")
    if not api_key or not model:
        return {"available": False, "reason": "no KYREX_API_KEY/KYREX_MODEL configured"}

    prompt = (
        "You are reviewing a code change made by an autonomous coding agent. "
        "Given the task it was asked to do and the actual diff it produced, "
        "judge ONLY whether the diff accomplishes what the task asked — not "
        "code style, not whether it's the best approach, just whether it matches. "
        "Respond with ONLY a JSON object, no other text, no markdown fences: "
        '{"matches_task": true or false, "reasoning": "one or two sentences"}\n\n'
        f"TASK:\n{task.strip()}\n\nDIFF:\n{diff_text[:15000]}"
    )

    try:
        if provider == "anthropic":
            base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
            req = urllib.request.Request(
                f"{base_url}/v1/messages",
                data=json.dumps({
                    "model": model, "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                }).encode(),
                method="POST",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            text = "".join(b.get("text", "") for b in data.get("content", []))
        else:
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            req = urllib.request.Request(
                f"{base_url.rstrip('/')}/chat/completions",
                data=json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                }).encode(),
                method="POST",
                headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"]

        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        verdict = json.loads(text)
        return {
            "available": True,
            "matches_task": bool(verdict.get("matches_task")),
            "reasoning": str(verdict.get("reasoning", "")),
        }
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}


def prepare_workspace(args, branch: str):
    """Returns (workdir: Path, remote_url: str, cleanup_fn: callable)."""
    if args.local_repo:
        local_repo = Path(args.local_repo).expanduser().resolve()
        remote_url = run_git(local_repo, "remote", "get-url", "origin").stdout.strip()
        run_git(local_repo, "fetch", "origin", args.base)
        workdir = Path(args.workdir_root).expanduser().resolve() / f"kyrex-task-{branch.split('/')[-1]}"
        if workdir.exists():
            shutil.rmtree(workdir)
        run_git(local_repo, "worktree", "add", "-b", branch, str(workdir), f"origin/{args.base}")

        def cleanup():
            if args.keep_workdir:
                return
            run_git(local_repo, "worktree", "remove", str(workdir), "--force", check=False)

        return workdir, remote_url, cleanup

    # Fresh clone from a URL — no local repo assumed.
    workdir = Path(args.workdir_root).expanduser().resolve() / f"kyrex-task-{branch.split('/')[-1]}"
    if workdir.exists():
        shutil.rmtree(workdir)
    clone_url = with_token(args.repo_url, args.token)
    subprocess.run(["git", "clone", clone_url, str(workdir)], check=True,
                    capture_output=True, text=True, env=_no_prompt_env())
    run_git(workdir, "checkout", "-b", branch, f"origin/{args.base}")

    def cleanup():
        if args.keep_workdir:
            return
        shutil.rmtree(workdir, ignore_errors=True)

    return workdir, args.repo_url, cleanup


def commit_and_push(workdir: Path, branch: str, task: str, remote_url: str, token: str | None) -> bool:
    """Returns True if there were changes to commit.

    Pushes to an explicit (optionally token-embedded) URL rather than the
    'origin' shorthand. In worktree mode, 'origin' is shared .git/config with
    the user's real local checkout — pushing by URL means we never write a
    token into that persisted config, and never depend on whatever ambient
    credential helper (or lack of one) is configured there.
    """
    run_git(workdir, "add", "-A")
    status = run_git(workdir, "status", "--porcelain").stdout
    if not status.strip():
        return False
    message = (
        f"{task.strip()[:72]}\n\n"
        f"Task: {task.strip()}\n\n"
        f"Generated by Kyrex Cloud Agent (Phase 2)."
    )
    subprocess.run(
        ["git", "-C", str(workdir),
         "-c", "user.name=Kyrex Cloud Agent",
         "-c", "user.email=kyrex-cloud-agent@users.noreply.github.com",
         "commit", "-m", message],
        check=True, capture_output=True, text=True, env=_no_prompt_env(),
    )
    push_url = with_token(remote_url, token)
    subprocess.run(["git", "-C", str(workdir), "push", push_url, f"HEAD:refs/heads/{branch}"],
                   check=True, capture_output=True, text=True, env=_no_prompt_env())
    return True


def open_pull_request(remote_url, branch, base, task, final_response, token, review=None):
    owner_repo = parse_owner_repo(remote_url)
    if not owner_repo:
        return {"skipped": True, "reason": f"could not parse owner/repo from remote '{remote_url}'"}
    if not token:
        return {"skipped": True, "reason": "no GitHub token (set GITHUB_TOKEN or pass --token)"}

    owner, repo = owner_repo
    review_line = ""
    if review and review.get("available"):
        verdict = "✅ matches task" if review.get("matches_task") else "⚠️ possible mismatch"
        review_line = f"\n**Self-review:** {verdict} — {review.get('reasoning', '')}\n"
    body = (
        f"**Task:**\n{task.strip()}\n\n"
        f"**Agent response:**\n{final_response.strip()}\n"
        f"{review_line}\n"
        f"---\n_Opened automatically by Kyrex Cloud Agent (Phase 2/4). Review before merging._"
    )
    payload = json.dumps({
        "title": task.strip()[:72],
        "head": branch,
        "base": base,
        "body": body,
    }).encode()

    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return {"skipped": False, "url": data.get("html_url"), "number": data.get("number")}
    except urllib.error.HTTPError as e:
        return {"skipped": True, "reason": f"GitHub API error {e.code}: {e.read().decode()[:300]}"}


def main():
    ap = argparse.ArgumentParser(description="Kyrex Cloud Agent — Phase 2 git workflow")
    ap.add_argument("--task", required=True)
    repo_group = ap.add_mutually_exclusive_group(required=True)
    repo_group.add_argument("--local-repo", help="path to an existing local clone (uses git worktree)")
    repo_group.add_argument("--repo-url", help="remote URL to clone fresh")
    ap.add_argument("--base", default="main", help="base branch to branch off / target for the PR")
    ap.add_argument("--branch", default=None, help="override the auto-generated branch name")
    ap.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="GitHub token (default: $GITHUB_TOKEN)")
    ap.add_argument("--skip-pr", action="store_true", help="push the branch but don't open a PR")
    ap.add_argument("--workdir-root", default="/tmp", help="where to create the isolated workspace")
    ap.add_argument("--keep-workdir", action="store_true", help="don't delete/remove the workspace afterward")
    ap.add_argument("--bridge", default=None)
    ap.add_argument("--python", default="python3")
    ap.add_argument("--startup-timeout", type=int, default=60)
    ap.add_argument("--idle-timeout", type=int, default=300)
    ap.add_argument("--overall-timeout", type=int, default=1800)
    ap.add_argument("--no-review", action="store_true", help="skip the self-review pass before opening a PR")
    args = ap.parse_args()

    branch = args.branch or f"kyrex/agent-{int(time.time())}-{slugify(args.task)}"
    bridge = find_bridge_script(args.bridge)

    def progress(msg):
        """Streamed to stdout for a caller (telegram_bot.py) to relay live —
        deliberately terse, one line per interesting event, flushed immediately."""
        t = msg.get("type")
        note = None
        if t == "tool_start":
            note = {"tool": msg.get("name")}
        elif t == "propose_edit":
            note = {"edit": Path(msg.get("filePath", "")).name}
        elif t == "confirm_request":
            note = {"confirm": msg.get("value")}
        if note:
            print(f"KYREX_PROGRESS:{json.dumps(note)}", flush=True)

    result = {
        "task": args.task,
        "branch": branch,
        "base": args.base,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "errors": [],
    }
    cleanup = lambda: None  # noqa: E731 — overwritten once prepare_workspace succeeds

    try:
        workdir, remote_url, cleanup = prepare_workspace(args, branch)
        result["workdir"] = str(workdir)

        agent = HeadlessAgent(
            bridge, workdir, python=args.python,
            startup_timeout=args.startup_timeout,
            idle_timeout=args.idle_timeout,
            overall_timeout=args.overall_timeout,
            on_event=progress,
        )
        if agent.start(args.task):
            agent.run()

        result.update({
            "chat_done_seen": agent.chat_done_seen,
            "final_response": agent.final_response,
            "approvals": agent.approvals,
            "tool_calls": agent.tool_calls,
            "errors": agent.errors,
        })

        if not agent.chat_done_seen:
            result["status"] = "agent_failed"
        else:
            has_changes = commit_and_push(workdir, branch, args.task, remote_url, args.token)
            result["has_changes"] = has_changes
            if not has_changes:
                result["status"] = "no_changes"
            elif args.skip_pr:
                result["status"] = "pushed_no_pr"
            else:
                review = None
                if not args.no_review:
                    diff_text = get_diff_since_base(workdir, args.base)
                    review = review_diff(args.task, diff_text)
                    result["review"] = review

                if review and review.get("available") and not review.get("matches_task"):
                    result["status"] = "review_flagged"
                    # Branch is pushed and safe either way — just not auto-PR'd.
                    # A human can open the PR manually after reading the reasoning.
                else:
                    pr = open_pull_request(remote_url, branch, args.base, args.task,
                                            agent.final_response, args.token, review=review)
                    result["pull_request"] = pr
                    result["status"] = "pr_opened" if not pr.get("skipped") else "pushed_pr_skipped"

        # Detect truncation due to recursion cap — the engine emits this literal
        # text when KYREX_MAX_RECURSION is exceeded.
        truncation_marker = "[!] Max recursion depth reached."
        if truncation_marker in (agent.final_response or ""):
            result["status"] = "truncated"
            result["final_response"] = (
                "The task was cut short because the agent hit its recursion limit. "
                "The summary above may be incomplete."
            )
    except subprocess.CalledProcessError as e:
        result["status"] = "git_failed"
        result["errors"].append((e.stderr or str(e)).strip())
    except Exception as e:
        result["status"] = "error"
        result["errors"].append(f"{type(e).__name__}: {e}")
    finally:
        cleanup()

    result["finished_at"] = datetime.now(timezone.utc).isoformat()

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{branch.replace('/', '_')}.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[git_workflow] status={result['status']} summary={out_path}")
    if result.get("errors"):
        print(f"[git_workflow] last error: {result['errors'][-1][:300]}")
    if result.get("pull_request", {}).get("url"):
        print(f"[git_workflow] PR: {result['pull_request']['url']}")
    print(f"KYREX_RESULT_JSON:{json.dumps(result)}", flush=True)


if __name__ == "__main__":
    main()
