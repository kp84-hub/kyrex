"""Authenticated, owner-bound inbound Google Messages trigger tests."""
import os
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "owner")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-google-messages-inbound-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "messages-inbound-test-secret")

BACKEND = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.dirname(os.path.dirname(BACKEND))
for path in (BACKEND, CLOUD):
    if path not in sys.path:
        sys.path.insert(0, path)

import bots  # noqa: E402
import browser_host_api as api  # noqa: E402
import browser_hosts as hosts  # noqa: E402
import main  # noqa: E402
import serve  # noqa: E402

HOST = "windows-pc"
OWNER = "owner"
BOT = "calendar"


def setup_function():
    root = Path(os.environ["KYREX_DATA_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    for name in ("browser_hosts.json", "bots.json"):
        (root / name).unlink(missing_ok=True)
    api._trigger_nonces.clear()


def _configured():
    secret = hosts.enroll_host(OWNER, HOST, allowlist=["messages.google.com"])["secret"]
    bots.add_bot(BOT, "Calendar Bot", "m", "/tmp/calendar-rift",
                 policy=serve.calendar_preset_policy(), status="running",
                 owner=OWNER)
    hosts.bind_bot(OWNER, BOT, HOST)
    return secret


def _body(secret, trigger=None, nonce=None):
    nonce = nonce or f"{int(time.time())}.{uuid.uuid4().hex}"
    trigger = trigger or ("a" * 64)
    return {"host_id": HOST, "nonce": nonce,
            "proof": hosts.proof_for(secret, HOST, nonce),
            "trigger_id": trigger}


def test_authenticated_bound_host_queues_exact_internal_task(monkeypatch):
    secret = _configured()
    submitted = []
    monkeypatch.setattr(main.store, "submit",
                        lambda **kw: submitted.append(kw) or kw["task_id"])
    from fastapi.testclient import TestClient
    response = TestClient(main.app).post(
        "/api/browser-hosts/google-messages-trigger", json=_body(secret))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert submitted[0]["task_text"] == serve.LEVEL6_MESSAGE_REQUEST
    assert submitted[0]["executor_prefix"] == "level6"
    assert submitted[0]["bot_id"] == BOT


def test_bad_proof_replay_and_unbound_host_fail_closed(monkeypatch):
    secret = _configured()
    monkeypatch.setattr(main.store, "submit", lambda **kw: kw["task_id"])
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    bad = _body(secret); bad["proof"] = "0" * 64
    assert client.post("/api/browser-hosts/google-messages-trigger", json=bad).status_code == 401
    body = _body(secret)
    assert client.post("/api/browser-hosts/google-messages-trigger", json=body).status_code == 200
    assert client.post("/api/browser-hosts/google-messages-trigger", json=body).status_code == 409
    hosts.unbind_bot(OWNER, BOT)
    assert client.post("/api/browser-hosts/google-messages-trigger",
                       json=_body(secret, trigger="b" * 64)).status_code == 409


def test_invalid_or_stale_trigger_fails_before_submission(monkeypatch):
    secret = _configured()
    monkeypatch.setattr(main.store, "submit",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("submitted")))
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    assert client.post("/api/browser-hosts/google-messages-trigger",
                       json=_body(secret, trigger="#L6Workout")).status_code == 400
    stale = f"{int(time.time()) - 1000}.abc"
    assert client.post("/api/browser-hosts/google-messages-trigger",
                       json=_body(secret, nonce=stale)).status_code == 409
