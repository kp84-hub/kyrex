"""Small authenticated Jev decision gateway for the OVH VPS.

Kyrex Cloud calls this service only for bounded route recommendations. Bot
execution, policy checks, and approval decisions stay in Kyrex Cloud.
"""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kyrex.decision import JevClient

MAX_BODY = 65_536
MAX_RESPONSE = 1_048_576


def _required_secret() -> str:
    value = os.environ.get("KYREX_JEV_ROUTER_TOKEN", "").strip()
    if len(value) < 32 or any(char.isspace() for char in value):
        raise RuntimeError("KYREX_JEV_ROUTER_TOKEN must be at least 32 characters")
    return value


def make_handler(secret: str, client=None):
    decision_client = client or JevClient(timeout=10.0)

    class Handler(BaseHTTPRequestHandler):
        server_version = "KyrexJevRouter/1"

        def log_message(self, _format, *_args):
            # Request bodies and headers can contain private data. Keep logs quiet.
            return

        def _send(self, status, payload):
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/v1/decide":
                self._send(404, {"error": "not_found"})
                return
            auth = self.headers.get("Authorization", "")
            supplied = auth[7:] if auth.startswith("Bearer ") else ""
            if not hmac.compare_digest(supplied, secret):
                self._send(401, {"error": "unauthorized"})
                return
            if self.headers.get_content_type() != "application/json":
                self._send(415, {"error": "json_required"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            if length < 1 or length > MAX_BODY:
                self._send(413, {"error": "invalid_body_size"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError
                result = decision_client.decide(payload.get("state"), payload.get("questions"))
                print("[jev-router] decision completed", flush=True)
                raw = json.dumps(result, separators=(",", ":")).encode("utf-8")
                if len(raw) > MAX_RESPONSE:
                    self._send(502, {"error": "invalid_jev_response"})
                    return
                self._send(200, result)
            except Exception as exc:
                # The upstream exception may contain private prompt data or URLs.
                print(f"[jev-router] decision failed: {type(exc).__name__}", flush=True)
                self._send(502, {"error": "decision_unavailable"})

    return Handler


def main():
    secret = _required_secret()
    host = os.environ.get("KYREX_JEV_ROUTER_BIND", "0.0.0.0")
    port = int(os.environ.get("KYREX_JEV_ROUTER_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), make_handler(secret))
    server.daemon_threads = True
    print("[jev-router] ready", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
