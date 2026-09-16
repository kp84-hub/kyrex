"""Focused tests for enabling the read-only Browser Bot preset from the Chat
Bot Settings surface.

Proves the smallest safe path for a least-privilege Browser Bot against the
EXISTING owner-scoped endpoints — no second policy model, and no bypass of the
ownership / capability / eligibility checks:

  1. the presets surface exposes the browser preset with its effective,
     host-derived permissions: ``browser:navigate`` + ``browser:read`` are
     GRANTED, and every interaction / write / mail / calendar / coordination
     operation stays denied;
  2. enabling the preset configures a Bot through the existing owner-scoped
     ``POST /api/bots/{id}/configure`` endpoint with ``preset: "browser"`` —
     but ONLY when the Bot has a non-empty browser allowlist AND an explicit
     Browser Host binding (both required, fail closed);
  3. the roster (``GET /api/bots``) reflects the server's OWN ``browser_bot``
     state, so the UI badge is refreshed from the server and never guessed
     locally;
  4. non-owners (403) and anonymous callers (401) cannot configure a Bot, and a
     refused attempt leaves the policy unchanged;
  5. a browser-only configuration grants NO write, interaction, mail, calendar,
     or coordination capability — those stay denied and the Bot is not
     writable.

Run: python3 -m pytest test_bot_browser_config.py
"""

import os
import sys
import tempfile

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault(
    "KYREX_DATA_DIR", tempfile.mkdtemp(prefix="kyrex-bot-browser-"))
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-browser-test-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main            # noqa: E402  (seeds the shared app + session map)
import bots            # noqa: E402  — the authoritative registry
import serve           # noqa: E402  — host tier table + browser preset/gate
import browser_hosts as bh  # noqa: E402 — the authoritative host registry


# The two browser operations a Browser Bot IS granted.
_GRANTED_OPS = ("browser:navigate", "browser:read")

# Every capability a least-privilege Browser Bot must NEVER have: the browser
# interaction/write ops, filesystem/repo writes, mail send, calendar create,
# and coordination authority.
_DENIED_OPS = (
    "browser:click", "browser:screenshot", "browser:type", "browser:upload",
    "browser:download", "browser:submit", "browser:delete",
    "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "bot:delegate",
)


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
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"
    bh.enroll_host("alice", "alice-host", name="Alice Host",
                   allowlist=["example.com"])
    bh.enroll_host("bob", "bob-host", name="Bob Host",
                   allowlist=["example.com"])


def teardown_function():
    _reset()


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _bot(bot_id="scout", owner="alice", allowlist=("example.com",),
         status="running"):
    """A running, read-only Bot owned by *owner*."""
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "openai:gpt-test",
        tempfile.mkdtemp(prefix=f"kx-{bot_id}-rift-"),
        policy={"fs:read": 0}, status=status, owner=owner,
        browser_allowlist=list(allowlist),
    )


# ── 1. the presets surface exposes the browser preset ────────────────

def test_presets_endpoint_exposes_browser_effective_permissions():
    r = _client("alice").get("/api/bots/presets")
    assert r.status_code == 200, r.text
    browser = next(p for p in r.json()["presets"] if p["id"] == "browser")
    # The policy is EXACTLY the read-only browser grant.
    assert browser["policy"] == {"browser:navigate": 0, "browser:read": 0}
    perms = browser["permissions"]
    for op in _GRANTED_OPS:
        assert perms[op] == 0, f"{op} must be granted, got {perms[op]!r}"
    for op in _DENIED_OPS:
        assert perms[op] == "deny", f"{op} must be denied, got {perms[op]!r}"


def test_presets_endpoint_requires_a_user():
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/bots/presets").status_code == 401


# ── 2. enabling the preset requires allowlist + explicit host binding ─

def test_browser_preset_refused_without_host_binding():
    _bot("scout", allowlist=["example.com"])       # allowlist, NO binding
    r = _client("alice").post(
        "/api/bots/scout/configure", json={"preset": "browser"})
    assert r.status_code == 409, r.text
    # Nothing was granted: the policy is still the read-only default.
    assert serve.is_browser_bot_policy(bots.get_bot("scout")["policy"]) is False


def test_browser_preset_refused_without_allowlist():
    _bot("scout", allowlist=[])                    # binding, NO allowlist
    bh.bind_bot("alice", "scout", "alice-host")
    r = _client("alice").post(
        "/api/bots/scout/configure", json={"preset": "browser"})
    assert r.status_code == 409, r.text
    assert serve.is_browser_bot_policy(bots.get_bot("scout")["policy"]) is False


def test_enable_browser_preset_configures_owned_bot():
    _bot("scout")
    bh.bind_bot("alice", "scout", "alice-host")
    r = _client("alice").post(
        "/api/bots/scout/configure", json={"preset": "browser"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["browser_bot"] is True
    assert body["writable"] is False
    assert body["policy"] == {"browser:navigate": 0, "browser:read": 0}
    assert serve.is_browser_bot_policy(bots.get_bot("scout")["policy"]) is True


def test_enable_browser_preset_accepts_allowlist_in_same_request():
    _bot("scout", allowlist=[])
    bh.bind_bot("alice", "scout", "alice-host")
    r = _client("alice").post(
        "/api/bots/scout/configure",
        json={"preset": "browser", "browser_allowlist": ["example.com"]})
    assert r.status_code == 200, r.text
    assert bots.get_bot("scout")["browser_allowlist"] == ["example.com"]
    assert r.json()["browser_bot"] is True


# ── 3. roster refresh reflects the server's own browser_bot state ────

def test_roster_reflects_browser_bot_state_after_enable():
    _bot("scout")
    bh.bind_bot("alice", "scout", "alice-host")
    c = _client("alice")

    # Bound + allowlisted, but NOT yet configured ⇒ the badge is False (the
    # capability grant is missing; the server never guesses from the allowlist).
    before = {b["id"]: b for b in c.get("/api/bots").json()["bots"]}
    assert before["scout"]["browser_bot"] is False

    assert c.post("/api/bots/scout/configure",
                  json={"preset": "browser"}).status_code == 200

    after = {b["id"]: b for b in c.get("/api/bots").json()["bots"]}
    assert after["scout"]["browser_bot"] is True
    assert after["scout"]["manageable"] is True


# ── 4. owner restriction: only the owner may enable the browser preset ─

def test_non_owner_and_anonymous_cannot_enable_browser_preset():
    _bot("scout", owner="alice")
    bh.bind_bot("alice", "scout", "alice-host")

    # Another user: 403, and the Bot's policy is left unchanged.
    r = _client("bob").post(
        "/api/bots/scout/configure", json={"preset": "browser"})
    assert r.status_code == 403, r.text
    assert serve.is_browser_bot_policy(bots.get_bot("scout")["policy"]) is False

    # Anonymous: 401.
    from fastapi.testclient import TestClient
    anon = TestClient(main.app).post(
        "/api/bots/scout/configure", json={"preset": "browser"})
    assert anon.status_code == 401

    assert serve.is_browser_bot_policy(bots.get_bot("scout")["policy"]) is False


# ── 5. browser-only config adds NO write / interaction / coordination ─

def test_browser_config_grants_only_navigate_and_read():
    _bot("scout")
    bh.bind_bot("alice", "scout", "alice-host")
    assert _client("alice").post(
        "/api/bots/scout/configure",
        json={"preset": "browser"}).status_code == 200

    bot = bots.get_bot("scout")
    # It IS a least-privilege Browser Bot...
    assert serve.is_browser_bot_policy(bot["policy"]) is True
    # ...and it is NOT a developer, a coordinator, or write-capable.
    assert serve.is_writable_bot_policy(bot["policy"]) is False
    assert serve.coordinator_granted(bot) is False
    perms = serve.effective_permissions(bot["policy"])
    for op in _GRANTED_OPS:
        assert perms[op] == 0, f"{op} must be granted ({perms[op]!r})"
    for op in _DENIED_OPS:
        assert perms[op] == "deny", f"{op} must remain denied ({perms[op]!r})"


# ── 6. the gate predicate itself is least-privilege (unit) ────────────

def test_is_browser_bot_policy_is_least_privilege():
    # The preset, and only the preset-shaped policy, qualifies.
    assert serve.is_browser_bot_policy(serve.BROWSER_PRESET) is True
    for other in ({}, {"fs:read": 0}, serve.DEVELOPER_PRESET,
                  serve.COORDINATOR_PRESET):
        assert serve.is_browser_bot_policy(other) is False
    # A wildcard that also grants an interaction op is NOT a Browser Bot.
    assert serve.is_browser_bot_policy({"browser:*": 0}) is False
    # Adding any denied capability disqualifies it.
    plus_click = dict(serve.BROWSER_PRESET)
    plus_click["browser:click"] = 0
    assert serve.is_browser_bot_policy(plus_click) is False
    plus_write = dict(serve.BROWSER_PRESET)
    plus_write["fs:write"] = 1
    assert serve.is_browser_bot_policy(plus_write) is False
    # A malformed policy fails closed.
    assert serve.is_browser_bot_policy({"browser:read": "yes"}) is False
