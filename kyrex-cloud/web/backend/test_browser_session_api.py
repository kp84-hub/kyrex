"""Endpoint tests for the managed Browser Session API (``chat_api.py``).

A lifecycle-only surface — it never runs a browser action and never accepts a
client-supplied command:

  GET  /api/bots/{id}/browser-session            -> {"session": view|null}
  POST /api/bots/{id}/browser-session/reconnect  -> {"session": view}
  POST /api/bots/{id}/browser-session/end        -> {"ended": bool}

Owner-scoping is delegated to ``_owned_bot``, so this suite proves: the owner
may inspect/reconnect/end; an ownerless Bot, a foreign Bot and an unknown Bot
are refused (403/403/404); anonymous is 401; a missing session reads as null;
reconnect starts one; end is idempotent; and no response ever serializes the
sealed credential.

Run: python3 -m pytest test_browser_session_api.py
"""

import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-browser-session-api-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "browser-session-api-test-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main            # noqa: E402  (seeds the shared app + session map)
import chat_service    # noqa: E402
import bots            # noqa: E402  — the authoritative registry
import browser_sessions as bs  # noqa: E402

PLAINTEXT = "PARKED-APPROVAL-REF-api"

# (method, path) for every managed-session endpoint.
_ENDPOINTS = (
    ("GET", "/api/bots/{bot}/browser-session"),
    ("POST", "/api/bots/{bot}/browser-session/reconnect"),
    ("POST", "/api/bots/{bot}/browser-session/end"),
)


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    """Fresh chat store, Bot registry, and browser-session storage."""
    root = chat_service._chat_root()
    for p in list(root.rglob("*.json")) + list(root.rglob("*.json.tmp")):
        p.unlink()
    bots.save_bots({})
    broot = bs._root()
    for p in list(broot.rglob("*.json")) + list(broot.rglob("*.json.tmp")):
        p.unlink()
    for d in broot.glob("sess-*"):
        shutil.rmtree(d, ignore_errors=True)


def setup_function():
    _reset()
    main.sessions["sess-owner"] = "owner"
    main.sessions["sess-other"] = "other"


def teardown_function():
    _reset()


def _client(user="owner"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _bot(bot_id="b1", owner="owner"):
    return bots.add_bot(bot_id, f"Bot {bot_id}", "anthropic:claude-test",
                        tempfile.mkdtemp(prefix="kx-bs-rift-"), owner=owner)


# ── happy path ─────────────────────────────────────────────────────

def test_get_returns_null_when_no_session():
    _bot("b1")
    r = _client().get("/api/bots/b1/browser-session")
    assert r.status_code == 200
    assert r.json() == {"session": None}


def test_reconnect_starts_then_reuses_the_session():
    _bot("b1")
    s1 = _client().post("/api/bots/b1/browser-session/reconnect").json()["session"]
    assert s1["state"] in (bs.STATE_STARTING, bs.STATE_CONNECTED)

    s2 = _client().post("/api/bots/b1/browser-session/reconnect").json()["session"]
    assert s2["session_id"] == s1["session_id"]   # reused, not replaced
    assert s2["state"] == bs.STATE_CONNECTED


def test_get_reports_the_created_session():
    _bot("b1")
    created = _client().post(
        "/api/bots/b1/browser-session/reconnect").json()["session"]
    got = _client().get("/api/bots/b1/browser-session").json()["session"]
    assert got["session_id"] == created["session_id"]


def test_end_is_idempotent_and_missing_is_false():
    _bot("b1")
    # No session yet -> ended False.
    assert _client().post("/api/bots/b1/browser-session/end").json() == {
        "ended": False}
    _client().post("/api/bots/b1/browser-session/reconnect")
    assert _client().post("/api/bots/b1/browser-session/end").json() == {
        "ended": True}
    assert _client().post("/api/bots/b1/browser-session/end").json() == {
        "ended": False}


# ── owner authorization / denial ───────────────────────────────────

def test_owner_may_use_every_endpoint():
    _bot("b1", owner="owner")
    for method, path in _ENDPOINTS:
        r = _client("owner").request(method, path.format(bot="b1"))
        assert r.status_code == 200, (method, path, r.text)


def test_foreign_bot_denied_for_every_endpoint():
    _bot("b1", owner="owner")
    for method, path in _ENDPOINTS:
        r = _client("other").request(method, path.format(bot="b1"))
        assert r.status_code == 403, (method, path, r.text)


def test_ownerless_bot_denied_for_every_endpoint():
    _bot("legacy", owner="")
    for method, path in _ENDPOINTS:
        r = _client("owner").request(method, path.format(bot="legacy"))
        assert r.status_code == 403, (method, path, r.text)


def test_unknown_bot_is_404():
    for method, path in _ENDPOINTS:
        r = _client().request(method, path.format(bot="ghost"))
        assert r.status_code == 404, (method, path, r.text)


def test_anonymous_is_401():
    from fastapi.testclient import TestClient
    _bot("b1", owner="owner")
    anon = TestClient(main.app)
    for method, path in _ENDPOINTS:
        assert anon.request(method, path.format(bot="b1")).status_code == 401


# ── secret-free responses ──────────────────────────────────────────

def test_responses_are_secret_free():
    _bot("b1")
    # Seed a session carrying a parked-approval credential.
    bs.create_session("owner", "b1", metadata={"approval": PLAINTEXT})

    body = _client().get("/api/bots/b1/browser-session").json()
    assert PLAINTEXT not in json.dumps(body)
    assert "sealed" not in body["session"]
    assert "metadata" not in body["session"]
    assert body["session"]["has_credential"] is True

    r = _client().post("/api/bots/b1/browser-session/reconnect")
    assert PLAINTEXT not in r.text
    assert "sealed" not in r.json()["session"]

    end = _client().post("/api/bots/b1/browser-session/end")
    assert end.json() == {"ended": True}
    assert PLAINTEXT not in end.text
