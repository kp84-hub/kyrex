"""Focused tests for the browser OAuth state transaction lifecycle."""

import importlib
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
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-oauth-state-tests")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
main = importlib.import_module("main")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _request():
    return SimpleNamespace(base_url="https://cloud.example/")


def _reset_state():
    with main.oauth_state_lock:
        main.oauth_states.clear()
    main.sessions.clear()


def setup_function():
    _reset_state()


def teardown_function():
    _reset_state()


def test_login_persists_secure_state_and_callback_consumes_it():
    response = main.login(_request())
    location = response.headers["location"]
    state = location.split("state=", 1)[1]

    with main.oauth_state_lock:
        assert state in main.oauth_states
        assert main.oauth_states[state]["consumed"] is False

    with patch.object(
        main.urllib.request,
        "urlopen",
        side_effect=[
            _Response({"access_token": "github-token"}),
            _Response({"login": "allowed-user"}),
        ],
    ):
        callback_response = main.callback("github-code", state, _request())

    assert callback_response.status_code == 307
    assert "session=" in callback_response.headers["set-cookie"]
    with main.oauth_state_lock:
        assert main.oauth_states[state]["consumed"] is True


def test_callback_rejects_missing_state():
    with pytest.raises(TypeError):
        main.callback("github-code", _request())


def test_callback_rejects_unknown_state_before_github_exchange():
    with patch.object(main.urllib.request, "urlopen") as urlopen:
        with pytest.raises(main.HTTPException) as exc:
            main.callback("github-code", "unknown-state", _request())
    assert exc.value.status_code == 400
    assert exc.value.detail == "Invalid OAuth state"
    urlopen.assert_not_called()


def test_callback_rejects_expired_state():
    state = main.create_oauth_state()
    with main.oauth_state_lock:
        main.oauth_states[state]["expires_at"] = 0

    with patch.object(main.urllib.request, "urlopen") as urlopen:
        with pytest.raises(main.HTTPException) as exc:
            main.callback("github-code", state, _request())
    assert exc.value.status_code == 400
    assert exc.value.detail == "OAuth state expired"
    urlopen.assert_not_called()


def test_callback_rejects_reused_state():
    state = main.create_oauth_state()
    with patch.object(
        main.urllib.request,
        "urlopen",
        side_effect=[
            _Response({"access_token": "github-token"}),
            _Response({"login": "allowed-user"}),
        ],
    ):
        main.callback("github-code", state, _request())

    with patch.object(main.urllib.request, "urlopen") as urlopen:
        with pytest.raises(main.HTTPException) as exc:
            main.callback("github-code", state, _request())
    assert exc.value.status_code == 400
    assert exc.value.detail == "OAuth state already consumed"
    urlopen.assert_not_called()


def test_state_is_consumed_even_when_github_username_is_not_allowed():
    state = main.create_oauth_state()
    with patch.object(
        main.urllib.request,
        "urlopen",
        side_effect=[
            _Response({"access_token": "github-token"}),
            _Response({"login": "different-user"}),
        ],
    ):
        with pytest.raises(main.HTTPException) as exc:
            main.callback("github-code", state, _request())
    assert exc.value.status_code == 403

    with main.oauth_state_lock:
        assert main.oauth_states[state]["consumed"] is True

    with pytest.raises(main.HTTPException) as exc:
        main.callback("github-code", state, _request())
    assert exc.value.status_code == 400
    assert exc.value.detail == "OAuth state already consumed"


def test_login_redirect_uri_uses_configured_public_base_url(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://kyrex-public.example")
    response = main.login(SimpleNamespace(base_url="http://internal.cloud.local/"))
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://kyrex-public.example/auth/callback"]


def test_login_redirect_uri_coerces_request_scheme_to_https(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "")
    response = main.login(SimpleNamespace(base_url="http://internal.cloud.local/"))
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://internal.cloud.local/auth/callback"]
