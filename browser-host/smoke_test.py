#!/usr/bin/env python3
"""browser-host/smoke_test.py - harmless Phase-1 smoke test (LOCAL ONLY).

Proves the two things Phase 1 must prove, with NO external network and NO
credentials:

  1. PERSISTENCE - a managed profile (``launch_persistent_context``) keeps a
     cookie across two separate operator runs against the SAME profile dir.
     This is exactly what the old throwaway ``new_context()`` broke.
  2. ISOLATION - a DIFFERENT ``(owner, bot_id)`` profile sees no cookie,
     because it is a different directory and a different Chromium profile.

The page is served by a throwaway 127.0.0.1 HTTP server started in-process, so
the only host ever contacted is loopback. Nothing here talks to Cloud.

Run (inside the image):
    python3 /host/smoke_test.py
Exit code 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profiles  # noqa: E402

OPERATOR = os.environ.get("KYREX_BROWSER_OPERATOR", "/host/browser_operator.py")
COOKIE = "kx_smoke=1"
PAGE = (
    b"<html><head><title>kx smoke</title></head>"
    b"<body>PLACEHOLDER</body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    """Serves a page that reports whether the smoke cookie came back."""

    def do_GET(self):  # noqa: N802 - stdlib interface
        present = COOKIE in (self.headers.get("Cookie") or "")
        token = b"cookie=present" if present else b"cookie=absent"
        body = PAGE.replace(b"PLACEHOLDER", token)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        if not present:
            # Hand the browser the cookie on the first visit only.
            self.send_header("Set-Cookie", COOKIE + "; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence stderr noise
        return None


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _run_operator(url: str, profile_dir: Path, workspace: Path) -> dict:
    """Run one operator process (managed, local Chromium); return its result.

    Verdicts are pre-supplied on stdin: one ALLOW per announced operation
    (navigate, then read), exactly as the host's approval loop would send them.
    """
    env = os.environ.copy()
    env.update({
        "KYREX_BROWSER_DRIVER": "playwright",
        "KYREX_BROWSER_MANAGED": "1",
        "KYREX_BROWSER_SESSION_DIR": str(profile_dir),
        "KYREX_BROWSER_EXECUTABLE": os.environ.get(
            "KYREX_BROWSER_EXECUTABLE", "/usr/bin/chromium"
        ),
        "KYREX_BROWSER_ALLOWLIST": json.dumps(["127.0.0.1"]),
        "KYREX_FS_ROOT": str(workspace),
        "KYREX_BOT_ID": "smoke-bot",
        "KYREX_BOT_OWNER": "smoke-owner",
    })
    task = json.dumps({"url": url})
    proc = subprocess.run(
        [sys.executable, OPERATOR, "--task", task],
        input="ALLOW\nALLOW\n",           # one verdict per operation
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    for line in (proc.stdout or "").splitlines():
        if line.startswith("KYREX_RESULT_JSON:"):
            return json.loads(line[len("KYREX_RESULT_JSON:"):])
    raise RuntimeError(
        "operator produced no result\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def _text(result: dict) -> str:
    return str(result.get("final_response") or "")


def main() -> int:
    server = _serve()
    _, port = server.server_address
    url = f"http://127.0.0.1:{port}/"
    root = profiles.profiles_root()
    workspace = Path(tempfile.mkdtemp(prefix="kx-smoke-ws-"))
    failures: list[str] = []
    try:
        profile_a = profiles.ensure_profile("smoke-owner", "smoke-b1", root=root)
        profile_b = profiles.ensure_profile("smoke-owner", "smoke-b2", root=root)

        first = _run_operator(url, profile_a, workspace)
        second = _run_operator(url, profile_a, workspace)
        other = _run_operator(url, profile_b, workspace)

        if "cookie=absent" not in _text(first):
            failures.append(
                f"run 1 expected cookie=absent, got {_text(first)!r}"
            )
        if "cookie=present" not in _text(second):
            failures.append(
                "cookie did NOT persist across runs in the same managed "
                f"profile, got {_text(second)!r}"
            )
        if "cookie=absent" not in _text(other):
            failures.append(
                "a DIFFERENT profile saw the first profile's cookie - "
                f"isolation broken, got {_text(other)!r}"
            )
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print("SMOKE TEST FAILED")
        for failure in failures:
            print("  -", failure)
        return 1
    print(
        "SMOKE TEST PASSED - managed profile persists across runs; "
        "distinct profiles stay isolated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
