"""Focused desktop OAuth handoff tests."""
import base64
import hashlib
import json
import os
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-desktop-oauth-tests")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main

class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return json.dumps(self.payload).encode()

def verifier_pair():
    verifier = "verifier-value-which-is-long-enough"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge

def reset():
    with main.oauth_state_lock: main.oauth_states.clear()
    with main.desktop_lock:
        main.desktop_transactions.clear(); main.desktop_handoffs.clear()

def setup_function(): reset()
def teardown_function(): reset()

def start():
    verifier, challenge = verifier_pair()
    response = main.desktop_start(
        "ide-state", main.DESKTOP_REDIRECT_URI, challenge,
        "S256", verifier, SimpleNamespace(base_url="https://cloud.example/"),
    )
    return verifier, response

def cloud_state_from(response):
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]

def test_valid_start_and_redirect_allowlist():
    verifier, challenge = verifier_pair()
    expected_state = "expected-cloud-state"
    with patch.object(main.secrets, "token_urlsafe", return_value=expected_state):
        response = main.desktop_start(
            "ide-state", main.DESKTOP_REDIRECT_URI, challenge,
            "S256", verifier, SimpleNamespace(base_url="https://cloud.example/"),
        )
    assert response.status_code == 307
    location = response.headers["location"]
    assert "github.com/login/oauth/authorize" in location
    query = parse_qs(urlparse(location).query)
    assert query["state"] == [expected_state]
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert main.desktop_transactions[expected_state]["code_verifier"] == verifier

def test_invalid_redirect_and_missing_pkce_rejected():
    with pytest.raises(main.HTTPException):
        main.desktop_start("s", "https://evil.example/callback", "x", "S256", "verifier")
    with pytest.raises(main.HTTPException):
        main.desktop_start("s", main.DESKTOP_REDIRECT_URI, "", "S256", "verifier")
    with pytest.raises(main.HTTPException):
        main.desktop_start("s", main.DESKTOP_REDIRECT_URI, "challenge", "S256", "")
    _, challenge = verifier_pair()
    with pytest.raises(main.HTTPException):
        main.desktop_start("s", main.DESKTOP_REDIRECT_URI, challenge, "S256", "incorrect-verifier")

def test_invalid_expired_and_replayed_cloud_state_rejected_before_github():
    _, response = start()
    cloud_state = cloud_state_from(response)
    with main.oauth_state_lock: main.oauth_states[cloud_state]["expires_at"] = 0
    with patch.object(main.urllib.request, "urlopen") as call:
        with pytest.raises(main.HTTPException): main.desktop_callback("code", cloud_state, SimpleNamespace(base_url="https://cloud.example/"))
        call.assert_not_called()

def test_authorized_callback_sends_exact_pkce_verifier_to_github_token_endpoint():
    verifier, response = start()
    cloud_state = cloud_state_from(response)
    captured = []

    def fake_urlopen(request, timeout=15):
        captured.append(request)
        return Response({"access_token": "token"}) if len(captured) == 1 else Response({"login": "allowed-user"})

    with patch.object(main.urllib.request, "urlopen", side_effect=fake_urlopen):
        callback = main.desktop_callback("github-code", cloud_state, SimpleNamespace(base_url="https://cloud.example/"))

    token_request = captured[0]
    form = parse_qs(token_request.data.decode())
    assert form["code_verifier"] == [verifier]
    assert base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode() == main.desktop_transactions.get(cloud_state, {}).get("code_challenge", "")


def test_authorized_callback_creates_handoff_and_exchange_is_one_time():
    verifier, response = start()
    cloud_state = cloud_state_from(response)
    with patch.object(main.urllib.request, "urlopen", side_effect=[Response({"access_token": "token"}), Response({"login": "allowed-user"})]):
        callback = main.desktop_callback("github-code", cloud_state, SimpleNamespace(base_url="https://cloud.example/"))
    location = callback.headers["location"]
    code = location.split("code=", 1)[1].split("&", 1)[0]
    import asyncio
    request = SimpleNamespace(json=lambda: None)
    async def body(): return {"code": code, "redirect_uri": main.DESKTOP_REDIRECT_URI, "code_verifier": verifier}
    request.json = body
    result = asyncio.run(main.desktop_exchange(request))
    assert result["username"] == "allowed-user"
    with pytest.raises(main.HTTPException): asyncio.run(main.desktop_exchange(request))

def test_wrong_verifier_and_redirect_are_rejected():
    verifier, response = start(); cloud_state = cloud_state_from(response)
    with patch.object(main.urllib.request, "urlopen", side_effect=[Response({"access_token": "token"}), Response({"login": "allowed-user"})]):
        callback = main.desktop_callback("code", cloud_state, SimpleNamespace(base_url="https://cloud.example/"))
    code = callback.headers["location"].split("code=", 1)[1].split("&", 1)[0]
    import asyncio
    async def exchange(verifier_value, redirect):
        request = SimpleNamespace(json=lambda: None)
        async def body(): return {"code": code, "redirect_uri": redirect, "code_verifier": verifier_value}
        request.json = body
        return await main.desktop_exchange(request)
    with pytest.raises(main.HTTPException): asyncio.run(exchange("wrong", main.DESKTOP_REDIRECT_URI))
    with pytest.raises(main.HTTPException): asyncio.run(exchange(verifier, "kyrex://evil/callback"))


def test_desktop_start_redirect_uri_uses_configured_public_base_url(monkeypatch):
    """GitHub-facing redirect_uri must use KYREX_PUBLIC_BASE_URL, never request.base_url."""
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://kyrex-public.example")
    verifier, challenge = verifier_pair()
    response = main.desktop_start(
        "ide-state", main.DESKTOP_REDIRECT_URI, challenge, "S256", verifier,
        SimpleNamespace(base_url="http://internal.cloud.local/"),
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://kyrex-public.example/auth/desktop/callback"]


def test_desktop_start_redirect_uri_coerces_request_scheme_to_https(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "")
    verifier, challenge = verifier_pair()
    # http-only internal base (the live failure) must be upgraded to https.
    response = main.desktop_start(
        "ide-state", main.DESKTOP_REDIRECT_URI, challenge, "S256", verifier,
        SimpleNamespace(base_url="http://kyrex-production.up.railway.app/"),
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://kyrex-production.up.railway.app/auth/desktop/callback"]
