#!/usr/bin/env python3
"""
headless_agent.py — Kyrex Cloud Agent, Phase 0.

Spawns kyrex_engine/core_bridge.py the same way the VS Code extension does
(KYREX_VSCODE=1), sends one task over the NDJSON stdio protocol, auto-approves
every propose_edit AND confirm_request the engine emits, waits for chat_done,
then writes a JSON summary (task, response, git diff, tool calls, errors) to disk.

No GUI, no manual approval step, fully unattended.

Protocol notes (confirmed by reading core_bridge.py / toolbox.py / extension.ts):
  - Setting KYREX_VSCODE=1 makes toolbox._is_interactive() return True, which
    routes file writes/edits through {"type": "propose_edit", "editId", "filePath",
    "content"} instead of the TUI's confirm_request diff gate. We reply with
    {"type": "edit_decision", "editId": ..., "accepted": true}.
  - Deletions (rm/rmdir/unlink/find -delete) ALWAYS go through a separate
    {"type": "confirm_request", "id", "value": "deletion", ...} regardless of
    KYREX_VSCODE — this is the same message type race mode's Go auto-approver
    replies to. We reply with {"type": "confirm_response", "id": ..., "approved": true}.
    Both message types must be handled — propose_edit alone is not sufficient.
  - KNOWN GAP (not fixed by this script): commands containing "sudo" or a pipe
    to sh/bash trigger a raw stderr prompt + builtin input() call in
    toolbox.run_command(), NOT the JSON protocol. That call reads the same
    stdin fd the engine's own stdin-reader thread already owns, so it cannot be
    satisfied by writing JSON — a task whose model tries `sudo ...` or
    `... | bash` will hang until idle-timeout kills it. Flagging as a known
    upstream issue for core_bridge.py rather than working around it here.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# kyrex-cloud/headless_agent.py sits next to kyrex_engine/ in the monorepo
# (kyrex/kyrex-cloud/, kyrex/kyrex_engine/) — resolve relative to this file
# first, since $HOME won't reliably be ~/kyrex once this deploys to a VPS.
REPO_RELATIVE_BRIDGE = Path(__file__).resolve().parent.parent / "kyrex_engine" / "core_bridge.py"
HOME_BRIDGE = Path.home() / "kyrex" / "kyrex_engine" / "core_bridge.py"


def find_bridge_script(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.exists():
            sys.exit(f"[headless_agent] bridge script not found: {p}")
        return p
    if REPO_RELATIVE_BRIDGE.exists():
        return REPO_RELATIVE_BRIDGE
    if HOME_BRIDGE.exists():
        return HOME_BRIDGE
    sys.exit(
        "[headless_agent] could not locate core_bridge.py at "
        f"{REPO_RELATIVE_BRIDGE} or {HOME_BRIDGE}. "
        "Pass --bridge /path/to/kyrex_engine/core_bridge.py"
    )


def git_diff(repo_dir: Path) -> str:
    """Unified diff of everything the agent changed, including new/untracked
    files. Stages, diffs against the index, then unstages again so the working
    tree is left exactly as the agent left it (just no longer staged)."""
    if not (repo_dir / ".git").exists():
        return "[not a git repository — diff unavailable]"
    try:
        subprocess.run(["git", "-C", str(repo_dir), "add", "-A"],
                        check=True, capture_output=True, text=True)
        diff = subprocess.run(["git", "-C", str(repo_dir), "diff", "--cached"],
                               check=True, capture_output=True, text=True).stdout
        subprocess.run(["git", "-C", str(repo_dir), "reset"], capture_output=True)
        return diff
    except subprocess.CalledProcessError as e:
        return f"[git diff failed: {e.stderr.strip()}]"


class HeadlessAgent:
    def __init__(self, bridge: Path, repo_dir: Path, python: str = "python3",
                 startup_timeout: int = 60, idle_timeout: int = 300,
                 overall_timeout: int = 1800, on_event=None, read_only: bool = False):
        self.bridge = bridge
        self.repo_dir = repo_dir
        self.python = python
        self.startup_timeout = startup_timeout
        self.idle_timeout = idle_timeout        # max silence between NDJSON lines
        self.overall_timeout = overall_timeout  # hard ceiling for the whole run
        self.on_event = on_event  # optional callback(msg: dict), called for every parsed NDJSON message
        self.read_only = read_only
        self.proc: subprocess.Popen | None = None
        self.out_q: "queue.Queue[tuple[str, str | None]]" = queue.Queue()
        self.approvals: list[dict] = []
        self.tool_calls: list[dict] = []
        self.errors: list[str] = []
        self.final_response = ""
        self.reasoning = ""
        self.chat_done_seen = False

    # ── stdio plumbing ──────────────────────────────────────────────
    def _stdout_reader(self):
        for line in self.proc.stdout:
            self.out_q.put(("stdout", line.rstrip("\n")))
        self.out_q.put(("stdout_closed", None))

    def _stderr_reader(self):
        for line in self.proc.stderr:
            self.out_q.put(("stderr", line.rstrip("\n")))

    @staticmethod
    def _parse(line: str):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    def _send(self, payload: dict):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self, task: str) -> bool:
        env = os.environ.copy()
        env["KYREX_VSCODE"] = "1"          # routes writes through propose_edit
        env["KYREX_SURFACE"] = "cloud"      # gives Kyrex an accurate self-description (see core.py)
        env["WORKSPACE_ROOT"] = str(self.repo_dir)
        env["PROJECT_SOURCE_ROOT"] = str(self.repo_dir)
        if self.read_only:
            env.pop("GITHUB_TOKEN", None)
            env["KYREX_READ_ONLY_REPO"] = "1"

        self.proc = subprocess.Popen(
            [self.python, str(self.bridge)],
            cwd=str(self.repo_dir),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._stdout_reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()

        # Wait for the startup handshake (session_state then phase:IDLE) before
        # sending the task — this doubles as confirmation config/API key loaded.
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            try:
                kind, line = self.out_q.get(timeout=1)
            except queue.Empty:
                continue
            if kind == "stdout_closed":
                self.errors.append("engine exited during startup (bad config/API key?)")
                return False
            if kind == "stderr":
                self.errors.append(line)
                continue
            msg = self._parse(line)
            if msg and msg.get("type") == "phase" and msg.get("value") == "IDLE":
                self._send({"type": "chat", "content": task})
                return True

        self.errors.append("engine did not reach IDLE within startup_timeout")
        self._shutdown()
        return False

    def run(self):
        """Drain events until chat_done + a short trailing quiet period, or timeout."""
        start = time.time()
        grace_deadline = None  # set once chat_done arrives
        while True:
            if time.time() - start > self.overall_timeout:
                self.errors.append("overall_timeout exceeded — killing engine")
                break
            timeout = 2 if grace_deadline else self.idle_timeout
            try:
                kind, line = self.out_q.get(timeout=timeout)
            except queue.Empty:
                if grace_deadline:
                    break  # trailing frames drained, wrap up
                self.errors.append(f"no output for {self.idle_timeout}s — killing engine")
                break

            if kind == "stderr":
                self.errors.append(line)
                continue
            if kind == "stdout_closed":
                break

            msg = self._parse(line)
            if not msg:
                continue
            t = msg.get("type")

            if self.on_event:
                try:
                    self.on_event(msg)
                except Exception:
                    pass  # a broken callback must never take down the actual run

            if t == "propose_edit":
                self._send({"type": "edit_decision", "editId": msg.get("editId"), "accepted": True})
                self.approvals.append({"kind": "edit", "path": msg.get("filePath")})

            elif t == "confirm_request":
                self._send({"type": "confirm_response", "id": msg.get("id"), "approved": True})
                self.approvals.append({"kind": msg.get("value", "confirm"), "path": msg.get("path")})

            elif t == "tool_start":
                self.tool_calls.append({"name": msg.get("name"), "args": msg.get("args")})

            elif t == "tool_result":
                if self.tool_calls:
                    self.tool_calls[-1]["result"] = msg.get("result")

            elif t == "error":
                self.errors.append(msg.get("content") or msg.get("message") or line)

            elif t == "chat_done":
                self.final_response = msg.get("content", "")
                self.reasoning = msg.get("reasoning", "")
                self.chat_done_seen = True
                grace_deadline = time.time() + 3  # drain trailing usage_stats/phase frames

        self._shutdown()

    def _shutdown(self):
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def main():
    ap = argparse.ArgumentParser(description="Kyrex Cloud Agent — Phase 0 headless auto-approve script")
    ap.add_argument("--task", required=True, help="task text to send to the engine")
    ap.add_argument("--repo", default=".", help="target repo/workspace directory")
    ap.add_argument("--bridge", default=None, help="path to core_bridge.py (default: ~/kyrex/kyrex_engine/core_bridge.py)")
    ap.add_argument("--python", default="python3")
    ap.add_argument("--output", default="kyrex_headless_result.json")
    ap.add_argument("--startup-timeout", type=int, default=60)
    ap.add_argument("--idle-timeout", type=int, default=300)
    ap.add_argument("--overall-timeout", type=int, default=1800)
    args = ap.parse_args()

    repo_dir = Path(args.repo).expanduser().resolve()
    bridge = find_bridge_script(args.bridge)

    agent = HeadlessAgent(
        bridge, repo_dir, python=args.python,
        startup_timeout=args.startup_timeout,
        idle_timeout=args.idle_timeout,
        overall_timeout=args.overall_timeout,
    )

    started_at = datetime.now(timezone.utc).isoformat()
    if agent.start(args.task):
        agent.run()
    finished_at = datetime.now(timezone.utc).isoformat()

    summary = {
        "task": args.task,
        "repo": str(repo_dir),
        "started_at": started_at,
        "finished_at": finished_at,
        "chat_done_seen": agent.chat_done_seen,
        "final_response": agent.final_response,
        "reasoning": agent.reasoning,
        "approvals": agent.approvals,
        "tool_calls": agent.tool_calls,
        "errors": agent.errors,
        "diff": git_diff(repo_dir),
    }

    out_path = Path(args.output).expanduser().resolve()
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"[headless_agent] summary written to {out_path}")
    print(f"[headless_agent] chat_done_seen={agent.chat_done_seen} errors={len(agent.errors)}")


if __name__ == "__main__":
    main()
