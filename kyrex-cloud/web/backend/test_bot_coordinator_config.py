"""Focused tests for enabling coordination from the Chat Bot Settings surface.

Proves the smallest safe path for the coordinator ("Chief of Staff") capability
against the EXISTING owner-scoped endpoints — no second coordinator model, and
no bypass of the ownership/capability checks:

  1. enabling coordination configures a Bot through the existing owner-scoped
     ``POST /api/bots/{id}/configure`` endpoint with ``preset: "coordinator"``;
  2. the roster (``GET /api/bots``) reflects the server's OWN coordinator state,
     so the UI badge is refreshed from the server and never guessed locally;
  3. non-owners (another user -> 403) and anonymous callers (401) cannot
     configure a Bot, and a refused attempt leaves the policy unchanged;
  4. a coordinator-only configuration grants NO write, browser, PR, push,
     delete, mail, or calendar capability — those stay denied and the Bot is
     not writable.

Run: python3 -m pytest test_bot_coordinator_config.py
"""

import os
import sys
import tempfile

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault(
    "KYREX_DATA_DIR", tempfile.mkdtemp(prefix="kyrex-bot-coordinator-"))
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-coordinator-test-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402  (after env setup)
import bots  # noqa: E402
import serve  # noqa: E402


# The unsafe operations a coordinator-only configuration must NEVER grant. A
# coordinator observes and delegates; the delegated TARGET stays authoritative
# for any consequential action.
_DENIED_OPS = (
    "fs:write", "repo:pr", "repo:push", "fs:delete",
    "mail:send", "cal:create", "browser:navigate", "browser:click",
)


def _reset():
    bots.save_bots({})


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _bot(bot_id="chief", owner="alice"):
    """A running, read-only Bot owned by *owner* (never writable)."""
    rift = tempfile.mkdtemp(prefix=f"kyrex-{bot_id}-rift-")
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "openai:gpt-test", rift,
        policy={"fs:read": 0}, status="running", owner=owner,
    )


# ── 1. the presets surface exposes the coordinator preset ───────────

def test_presets_endpoint_exposes_coordinator():
    r = _client("alice").get("/api/bots/presets")
    assert r.status_code == 200, r.text
    coord = next(p for p in r.json()["presets"] if p["id"] == "coordinator")
    assert coord["policy"]["bot:delegate"] == 0
    perms = coord["permissions"]
    assert perms["bot:delegate"] == 0
    assert perms["fs:read"] == 0
    for op in _DENIED_OPS:
        assert perms[op] == "deny", f"{op} must be denied for a coordinator"


# ── 2. enabling coordination via the existing configure endpoint ────

def test_enable_coordination_configures_owned_bot():
    _bot("chief", owner="alice")
    r = _client("alice").post(
        "/api/bots/chief/configure", json={"preset": "coordinator"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["coordinator"] is True
    assert body["writable"] is False
    assert body["policy"].get("bot:delegate") == 0
    assert serve.coordinator_granted(bots.get_bot("chief")) is True


# ── 3. roster refresh reflects the server's own coordinator state ───

def test_roster_reflects_coordinator_state_after_enable():
    _bot("chief", owner="alice")
    c = _client("alice")

    before = {b["id"]: b for b in c.get("/api/bots").json()["bots"]}
    assert before["chief"]["coordinator"] is False

    assert c.post("/api/bots/chief/configure",
                  json={"preset": "coordinator"}).status_code == 200

    after = {b["id"]: b for b in c.get("/api/bots").json()["bots"]}
    assert after["chief"]["coordinator"] is True
    assert after["chief"]["manageable"] is True


# ── 4. owner restriction: only the owner may enable coordination ────

def test_non_owner_and_anonymous_cannot_enable_coordination():
    _bot("chief", owner="alice")

    # Another user: 403, and the Bot's policy is left unchanged.
    r = _client("bob").post(
        "/api/bots/chief/configure", json={"preset": "coordinator"})
    assert r.status_code == 403, r.text
    assert serve.coordinator_granted(bots.get_bot("chief")) is False

    # Anonymous: 401.
    from fastapi.testclient import TestClient
    anon = TestClient(main.app).post(
        "/api/bots/chief/configure", json={"preset": "coordinator"})
    assert anon.status_code == 401

    # Still read-only — nothing was granted.
    assert serve.coordinator_granted(bots.get_bot("chief")) is False


# ── 5. coordinator-only config adds no write / browser capability ────

def test_coordinator_config_adds_no_write_or_browser_access():
    _bot("chief", owner="alice")
    assert _client("alice").post(
        "/api/bots/chief/configure",
        json={"preset": "coordinator"}).status_code == 200

    bot = bots.get_bot("chief")
    # It IS a coordinator...
    assert serve.coordinator_granted(bot) is True
    # ...and it is NOT a developer: no write, no browser, nothing unsafe.
    assert serve.is_writable_bot_policy(bot["policy"]) is False
    perms = serve.effective_permissions(bot["policy"])
    for op in _DENIED_OPS:
        assert perms[op] == "deny", f"{op} must remain denied ({perms[op]})"
    assert perms["bot:delegate"] == 0
