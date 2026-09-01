#!/usr/bin/env python3
"""Fetch PR #83 metadata, body, and comments from the GitHub API (read-only)."""
import json
import subprocess
import urllib.request

REPO_DIR = "/tmp/kyrex-task-agent-1788268903-continue-pr-83-from-the-current"
API = "https://api.github.com/repos/kp84-hub/kyrex"


def token_from_git() -> str:
    url = subprocess.run(
        ["git", "-C", REPO_DIR, "remote", "get-url", "origin"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # https://x-access-token:TOKEN@github.com/...
    return url.split("://")[1].split("@")[0].split(":", 1)[1]


def get(path: str):
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": "token " + token_from_git(),
                 "User-Agent": "kyrex-agent"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    pr = get("/pulls/83")
    print("TITLE:", pr["title"])
    print("STATE:", pr["state"], "| MERGED:", pr["merged"])
    print("HEAD:", pr["head"]["ref"], pr["head"]["sha"][:9])
    print("BASE:", pr["base"]["ref"])
    print("---BODY---")
    print((pr.get("body") or "")[:6000])
    print("---COMMENTS---")
    for c in get("/issues/83/comments"):
        print(f"[{c['user']['login']} @ {c['created_at']}]")
        print(c["body"][:3000])
        print("-" * 40)
    print("---REVIEW COMMENTS---")
    try:
        for c in get("/pulls/83/comments"):
            print(f"[{c['user']['login']}] {c['path']}: {c['body'][:1500]}")
            print("-" * 40)
    except Exception as e:  # noqa: BLE001
        print("review comments unavailable:", e)


if __name__ == "__main__":
    main()
