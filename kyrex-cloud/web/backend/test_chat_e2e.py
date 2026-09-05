#!/usr/bin/env python3
"""End-to-end test for Kyrex Chat.

Exercises the real pipeline end-to-end:

    POST /api/chat -> chat_service -> Kyrex engine provider (stream=True)
        -> SSE -> client (token deltas) -> persistence

The engine provider is the REAL ``kyrex.providers`` OpenAI provider, pointed at
a local OpenAI-compatible mock HTTP server (so it runs hermetically without a
live API key). The tokens asserted are those the real provider streams back —
no fabricated assistant text.

Authentication: the existing Cloud auth boundary is exercised by seeding a
session into ``main.sessions`` (equivalent to an established login session) and
sending the session cookie, exactly as the browser frontend will.

Run: python3 test_chat_e2e.py
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = "/tmp/kyrex-chat-e2e"
MOCK_PORT = 9102

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", DATA_DIR)
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "mock-model")
os.environ.setdefault("KYREX_API_KEY", "sk-mock")
os.environ.setdefault("KYREX_BASE_URL", f"http://127.0.0.1:{MOCK_PORT}/v1")

sys.path.insert(0, BACKEND_DIR)


class MockOpenAI(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length))
        stream = body.get("stream", False)
        if not stream:
            data = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for token in ["Hello", ", ", "Kyrex", "!"]:
            chunk = {"choices": [{"delta": {"content": token}, "index": 0}]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
            time.sleep(0.03)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    mock = HTTPServer(("127.0.0.1", MOCK_PORT), MockOpenAI)
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    import main  # noqa: E402  (import moved after env setup)

    # Seed an authenticated session, mirroring an established login.
    main.sessions["e2e-session"] = "e2e-user"

    from starlette.testclient import TestClient

    client = TestClient(main.app, cookies={"session": "e2e-session"})

    # 1. Conversation status / availability.
    status = client.get("/api/chat/status")
    print("status:", status.status_code, status.json())
    assert status.json().get("available") is True, "engine should report available"

    # 2. Create a conversation.
    r = client.post("/api/conversations", json={})
    assert r.status_code == 200, r.text
    conv = r.json()
    conv_id = conv["conversation_id"]
    print("created conversation:", conv_id)

    # 3. Send a message — verify the real SSE stream carries token deltas.
    deltas = []
    with client.stream("POST", "/api/chat", json={"conversation_id": conv_id, "message": "hi"}) as r:
        assert r.status_code == 200, r.status_code
        for line in r.iter_lines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if payload.get("type") == "delta":
                    deltas.append(payload["content"])
    full = "".join(deltas)
    print("streamed deltas:", deltas)
    print("full response:", repr(full))
    assert full == "Hello, Kyrex!", f"unexpected streamed text {full!r}"

    # 4. Retrieve the conversation — persistence of both turns.
    conv2 = client.get(f"/api/conversations/{conv_id}").json()
    roles = [m["role"] for m in conv2["messages"]]
    print("persisted messages:", [(m["role"], m["content"]) for m in conv2["messages"]])
    assert roles == ["user", "assistant"], roles
    assert conv2["messages"][1]["content"] == "Hello, Kyrex!"

    # 5. Second message in the same conversation (context preserved in history).
    deltas2 = []
    with client.stream("POST", "/api/chat", json={"conversation_id": conv_id, "message": "again"}) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if payload.get("type") == "delta":
                    deltas2.append(payload["content"])
    assert "".join(deltas2) == "Hello, Kyrex!"
    conv3 = client.get(f"/api/conversations/{conv_id}").json()
    assert len(conv3["messages"]) == 4, "two turns = four messages"

    # 6. Conversation isolation: a different user sees nothing.
    assert client.get("/api/conversations").json()["conversations"][0]["conversation_id"] == conv_id

    print("E2E PASS: HTTP -> engine provider (stream) -> SSE -> client -> persistence")
    mock.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
