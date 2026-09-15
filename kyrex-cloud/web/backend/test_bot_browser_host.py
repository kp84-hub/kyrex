"""Endpoint tests for the Browser Host binding + Bot allowlist surface
(``chat_api.py``) — the owner-scoped control that lets a Browser Bot run on a
Browser Host.

Covered:
  * GET/POST/DELETE /api/bots/{id}/browser-host  explicit bind/unbind/status
  * the Bot view exposes the REDACTED ``browser_allowlist``
  * PATCH /api/bots/{id} and POST /api/bots/{id}/configure edit the allowlist
  * owner isolation, foreign-host rejection, unknown-host rejection

Run: python3 -m pytest test_bot_browser_host.py
"""

import os
import sys
import tempfile

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bot-browser-host-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-browser-host-test-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main            # noqa: E402  (seeds the shared app + session map)
import bots            # noqa: E402  — the authoritative registry
import browser_hosts as bh  # noqa: E402  — the authoritative host registry


def _reset():
    bots.save_bots({})
    try:
        path = bh._registry_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass


def setup_function():
    _reset()
    main.sessions["sess-owner"] = "owner"
    main.sessions["sess-other"] = "other"
    bh.enroll_host("owner", "ovh-ny-01", name="NY", allowlist=["example.com"])
    bh.enroll_host("other", "other-host", name="Other", allowlist=["example.com"])


def teardown_function():
    _reset()


def _client(user="owner"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _bot(bot_id="b1", owner="owner", allowlist=None):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "anthropic:claude-test",
        tempfile.mkdtemp(prefix="kx-bh-rift-"), owner=owner,
        browser_allowlist=allowlist if allowlist is not None else ["example.com"])


# ── host status / explicit bind / unbind ──────────────────────────────

def test_unbound_bot_reports_no_host_but_lists_eligible_hosts():
    _bot("b1")
    r = _client("owner").get("/api/bots/b1/browser-host")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bound_host_id"] == ""
    assert body["host"] is None
    # Only the owner's own hosts are offered, and only redacted views.
    assert {h["host_id"] for h in body["hosts"]} == {"ovh-ny-01"}
    assert "sealed" not in str(body)


def test_explicit_bind_then_unbind():
    _bot("b1")
    c = _client("owner")
    bound = c.post("/api/bots/b1/browser-host", json={"host_id": "ovh-ny-01"})
    assert bound.status_code == 200, bound.text
    assert bound.json()["bound_host_id"] == "ovh-ny-01"
    assert bh.binding_for("owner", "b1") == "ovh-ny-01"

    got = c.get("/api/bots/b1/browser-host").json()
    assert got["bound_host_id"] == "ovh-ny-01"
    assert got["host"]["host_id"] == "ovh-ny-01"

    removed = c.delete("/api/bots/b1/browser-host")
    assert removed.status_code == 200
    assert removed.json()["unbound"] is True
    assert bh.binding_for("owner", "b1") == ""


def test_bind_requires_host_id():
    _bot("b1")
    r = _client("owner").post("/api/bots/b1/browser-host", json={})
    assert r.status_code == 400


# ── owner isolation / foreign-host rejection ──────────────────────────

def test_foreign_host_rejected():
    _bot("b1", owner="owner")
    r = _client("owner").post("/api/bots/b1/browser-host",
                              json={"host_id": "other-host"})
    assert r.status_code == 409, r.text
    assert bh.binding_for("owner", "b1") == ""


def test_unknown_host_rejected():
    _bot("b1", owner="owner")
    r = _client("owner").post("/api/bots/b1/browser-host",
                              json={"host_id": "ghost"})
    assert r.status_code == 409, r.text


def test_foreign_bot_denied_for_every_method():
    _bot("b1", owner="owner")
    c = _client("other")
    assert c.get("/api/bots/b1/browser-host").status_code == 403
    assert c.post("/api/bots/b1/browser-host",
                  json={"host_id": "other-host"}).status_code == 403
    assert c.delete("/api/bots/b1/browser-host").status_code == 403


def test_unknown_bot_is_404():
    assert _client("owner").get(
        "/api/bots/ghost/browser-host").status_code == 404


def test_anonymous_is_401():
    from fastapi.testclient import TestClient
    anon = TestClient(main.app)
    assert anon.get("/api/bots/b1/browser-host").status_code == 401


# ── allowlist view (redacted) + edit through the config paths ──────────

def test_bot_view_exposes_redacted_allowlist():
    _bot("b1", allowlist=["Example.com", "sub.example.com"])
    r = _client("owner").get("/api/bots")
    assert r.status_code == 200, r.text
    bot = next(b for b in r.json()["bots"] if b["id"] == "b1")
    assert bot["browser_allowlist"] == ["example.com", "sub.example.com"]


def test_patch_updates_allowlist():
    _bot("b1", allowlist=["example.com"])
    r = _client("owner").patch("/api/bots/b1",
                               json={"browser_allowlist": ["docs.example.com"]})
    assert r.status_code == 200, r.text
    assert r.json()["browser_allowlist"] == ["docs.example.com"]
    assert bots.get_bot("b1")["browser_allowlist"] == ["docs.example.com"]


def test_patch_rejects_a_url_entry():
    _bot("b1")
    r = _client("owner").patch(
        "/api/bots/b1",
        json={"browser_allowlist": ["https://example.com/path"]})
    assert r.status_code == 400


def test_configure_updates_allowlist():
    _bot("b1", allowlist=["example.com"])
    r = _client("owner").post(
        "/api/bots/b1/configure",
        json={"browser_allowlist": ["a.example.com", "b.example.com"]})
    assert r.status_code == 200, r.text
    assert bots.get_bot("b1")["browser_allowlist"] == [
        "a.example.com", "b.example.com"]
