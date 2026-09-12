"""Browser Operator smoke test against a LOCAL deterministic page.

No external website, no real browser: a localhost HTTP server serves a fixed
page and the executor runs with its deterministic ``local`` transport. This
exercises the full stdout/stdin protocol end to end — operation verdicts, the
approval pause/resume handshake, allowlist blocking, and workspace-confined
artifacts — as a spawned process, the way serve.run_task drives it.

Pytest-style and import-safe. Run: python3 -m pytest test_browser_smoke.py
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


def executor_path():
    return Path(__file__).resolve().parent / "browser_operator.py"


def protocol_lines(stdout):
    return [ln for ln in stdout.splitlines() if ln.startswith("KYREX_")]


def parse_result(stdout):
    for line in stdout.splitlines():
        if line.startswith("KYREX_RESULT_JSON:"):
            return json.loads(line[len("KYREX_RESULT_JSON:"):])
    return None


@pytest.fixture
def local_site(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    body = (
        b"<html><head><title>Kyrex Test Page</title></head>"
        b"<body>Hello browser operator</body></html>"
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib interface
            if self.path in ("/", "/index.html", "/f"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # silence stderr noise
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    try:
        yield {"base": f"http://127.0.0.1:{port}", "root": str(root)}
    finally:
        server.shutdown()
        server.server_close()


def run_executor(task_obj, verdicts, root, allowlist=None):
    env = os.environ.copy()
    env["KYREX_FS_ROOT"] = root
    env["KYREX_BROWSER_DRIVER"] = "local"
    env["KYREX_BROWSER_ALLOWLIST"] = json.dumps(
        allowlist if allowlist is not None else ["127.0.0.1"]
    )
    env["KYREX_BOT_ID"] = "smoke-bot"
    env["KYREX_BOT_OWNER"] = "smoke-owner"
    env["KYREX_SMOKE_PASSWORD"] = "topsecretxyz"
    proc = subprocess.run(
        [sys.executable, str(executor_path()), "--task", json.dumps(task_obj)],
        input="".join(f"{v}\n" for v in verdicts),
        capture_output=True, text=True, timeout=30, env=env,
    )
    return proc


def test_smoke_navigate_read_screenshot(local_site):
    task = {
        "actions": [
            {"action": "navigate", "url": local_site["base"] + "/"},
            {"action": "read"},
            {"action": "screenshot"},
        ]
    }
    proc = run_executor(task, ["ALLOW", "ALLOW", "ALLOW"], local_site["root"])
    assert proc.returncode == 0
    # Nothing but protocol lines may appear on stdout.
    for line in proc.stdout.splitlines():
        if line.strip():
            assert line.startswith("KYREX_"), f"stray stdout: {line!r}"
    result = parse_result(proc.stdout)
    assert result is not None, proc.stdout + proc.stderr
    assert result["status"] in ("no_changes", "ok")
    assert "Hello browser operator" in result["final_response"]
    assert "topsecretxyz" not in proc.stdout
    shot = Path(local_site["root"]) / "browser-artifacts" / "screenshot-02.png"
    assert shot.exists()


def test_smoke_blocked_domain_never_runs(local_site):
    task = {"actions": [{"action": "navigate", "url": "http://evil.example.invalid/"}]}
    proc = run_executor(task, ["ALLOW"], local_site["root"])
    result = parse_result(proc.stdout)
    assert result is not None
    assert result["status"] == "error"
    assert "allowlist" in " ".join(result["errors"])
    # Blocked before any operation was announced.
    assert not any(ln.startswith("KYREX_OPERATION:") for ln in proc.stdout.splitlines())


def test_smoke_consequential_click_approval_pauses_and_resumes(local_site):
    task = {
        "actions": [
            {"action": "navigate", "url": local_site["base"] + "/"},
            {"action": "click", "selector": "#buy", "label": "Buy now"},
        ]
    }
    # navigate: ALLOW; click(submit): APPROVE then APPROVED.
    proc = run_executor(
        task, ["ALLOW", "APPROVE", "APPROVED"], local_site["root"]
    )
    assert proc.returncode == 0
    approvals = [ln for ln in proc.stdout.splitlines() if ln.startswith("KYREX_APPROVAL:")]
    assert approvals, proc.stdout
    request = json.loads(approvals[0][len("KYREX_APPROVAL:"):])
    assert request["tier"] == 2
    assert request["token"].startswith("SUBMIT ")
    result = parse_result(proc.stdout)
    assert result["status"] == "ok"


def test_smoke_denied_approval_terminates_cleanly(local_site):
    task = {
        "actions": [
            {"action": "navigate", "url": local_site["base"] + "/"},
            {"action": "click", "selector": "#buy", "label": "Buy now"},
        ]
    }
    proc = run_executor(
        task, ["ALLOW", "APPROVE", "DENIED"], local_site["root"]
    )
    # Must not hang: subprocess.run(timeout=30) would raise otherwise.
    assert proc.returncode == 0
    result = parse_result(proc.stdout)
    assert result is not None
    assert result["status"] == "error"
    assert result["errors"][0] == "browser.submit denied"


def test_smoke_approval_timeout_eof_terminates_cleanly(local_site):
    """No decision for a paused approval (host timeout/cancel writes nothing
    then closes stdin): the executor must treat EOF as DENIED and exit, never
    hang the worker."""
    task = {
        "actions": [
            {"action": "navigate", "url": local_site["base"] + "/"},
            {"action": "click", "selector": "#buy", "label": "Buy now"},
        ]
    }
    # APPROVE registers the approval; no decision line follows → stdin EOF.
    proc = run_executor(task, ["ALLOW", "APPROVE"], local_site["root"])
    assert proc.returncode == 0
    result = parse_result(proc.stdout)
    assert result is not None
    assert result["status"] == "error"
    assert result["errors"][0] == "browser.submit denied"


def test_smoke_empty_allowlist_blocks_everything(local_site):
    task = {"actions": [{"action": "navigate", "url": local_site["base"] + "/"}]}
    proc = run_executor(task, ["ALLOW"], local_site["root"], allowlist=[])
    result = parse_result(proc.stdout)
    assert result["status"] == "error"
    assert "allowlist" in " ".join(result["errors"])
