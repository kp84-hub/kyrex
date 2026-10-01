"""Authenticated Jev gateway contract tests; no live TypeSafe requests."""
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "kyrex_engine"))
sys.path.insert(0, str(HERE))
from jev_router import _required_secret, make_handler  # noqa: E402


class FakeJev:
    def __init__(self):
        self.calls = []

    def decide(self, state, questions):
        self.calls.append((state, questions))
        return {"model": "jev-test", "answers": {"route": {"type": "choice",
                "choice": "engine", "confidence": 0.9, "probabilities": {"engine": 0.9}}},
                "usage": {}}


@pytest.fixture
def gateway():
    fake = FakeJev()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler("s" * 40, fake))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_is_minimal_and_does_not_call_jev(gateway):
    url, fake = gateway
    with urllib.request.urlopen(url + "/health") as response:
        assert response.status == 200
        assert json.load(response) == {"status": "ok"}
    assert fake.calls == []


def test_decision_requires_bearer_token(gateway):
    url, fake = gateway
    req = urllib.request.Request(url + "/v1/decide", data=b'{"state":"x","questions":{}}',
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code == 401
    assert fake.calls == []


def test_authenticated_decision_forwards_payload_and_returns_structured_answer(gateway):
    url, fake = gateway
    payload = {"state": {"request": "open the calendar"}, "questions": {"route": {"type": "choice"}}}
    req = urllib.request.Request(url + "/v1/decide", data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Bearer " + "s" * 40,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        result = json.load(response)
    assert result["model"] == "jev-test"
    assert result["answers"]["route"]["choice"] == "engine"
    assert fake.calls == [(payload["state"], payload["questions"])]


def test_missing_or_short_secrets_fail_startup(monkeypatch):
    monkeypatch.delenv("KYREX_JEV_ROUTER_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        _required_secret()
    monkeypatch.setenv("KYREX_JEV_ROUTER_TOKEN", "short")
    with pytest.raises(RuntimeError):
        _required_secret()
