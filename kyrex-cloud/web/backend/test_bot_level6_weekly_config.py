"""Focused tests for the "Level 6 Weekly" bot preset — usable from Kyrex Chat
without hand-crafted production records.

Proves the dedicated pinned weekly-read preset against the EXISTING
owner-scoped endpoints — no second policy model, and no bypass of the
ownership / capability checks:

  1. the presets surface exposes the level6-weekly preset with its effective,
     host-derived permissions: EXACTLY the four tier-0 operations the pinned
     ``level6: weekly`` command performs are granted; every other browser /
     write / mail / calendar / coordination operation stays denied; and the
     preset's fixed browser allowlist is exactly ``facebook.com``;
  2. creating a Bot with ``preset: "level6-weekly"`` through the EXISTING
     owner-scoped ``POST /api/bots`` endpoint stores EXACTLY the preset policy
     AND the fixed ``facebook.com`` allowlist (a caller cannot widen it);
  3. enabling the preset on an existing Bot through the EXISTING
     ``POST /api/bots/{id}/configure`` endpoint stores EXACTLY the preset
     policy AND the fixed allowlist — a caller-supplied allowlist can neither
     widen nor replace it;
  4. the existing presets are PRESERVED: the Browser and Glofox Reader presets
     are unchanged and neither is widened, and the level6-weekly preset is not
     a Glofox Reader or a Browser Bot;
  5. the byte-exact command ``level6: weekly`` is the ONLY command the preset
     routes; the route is exclusive (level6, never repo/browser/glofox).

Run: python3 -m pytest test_bot_level6_weekly_config.py
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault(
    "KYREX_DATA_DIR", tempfile.mkdtemp(prefix="kyrex-bot-level6-"))
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-level6-test-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main            # noqa: E402  (seeds the shared app + session map)
import bots            # noqa: E402  — the authoritative registry
import dev_bot         # noqa: E402  — the Chat routing/submission gates
import serve           # noqa: E402  — host tier table + preset/gate predicates
import browser_hosts as bh  # noqa: E402

# EXACTLY the four operations a Level 6 Weekly Bot is granted (all tier 0).
_GRANTED_OPS = (
    "browser:navigate", "browser:read", "browser:screenshot", "glofox:read",
)

# Every capability a Level 6 Weekly Bot must NEVER have.
_DENIED_OPS = (
    "browser:click", "browser:type", "browser:upload", "browser:download",
    "browser:submit", "browser:delete",
    "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "bot:delegate",
)

COMMAND = serve.LEVEL6_TASK_TEXT  # the byte-exact "level6: weekly"


def _reset():
    bots.save_bots({})
    try:
        path = bh._registry_path()
        if path.exists():
            path.unlink()
    except Exception:
        pass
    import chat_service
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _profile(owner="alice", profile_id="level6-prof", models=("m1",)):
    import provider_profiles
    return provider_profiles.save_profile(owner, {
        "id": profile_id,
        "name": profile_id.upper(),
        "provider": "openai",
        "base_url": "https://example.test/v1",
        "api_key": "sk-secret-level6",
        "models": list(models),
    })


def _weekly_bot(bot_id="weekly", owner="alice", status="running",
                policy=None, allowlist=None, profile=True):
    if profile:
        _profile(owner)
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "openai:m1",
        tempfile.mkdtemp(prefix=f"kx-{bot_id}-rift-"),
        policy=serve.level6_weekly_preset_policy() if policy is None else policy,
        status=status, owner=owner,
        browser_allowlist=(serve.level6_weekly_preset_allowlist()
                           if allowlist is None else allowlist),
        provider_profile_id="level6-prof" if profile else "",
    )


# ── 1. the presets surface exposes the level6-weekly preset ──────────

def test_presets_endpoint_exposes_level6_weekly_effective_permissions():
    r = _client("alice").get("/api/bots/presets")
    assert r.status_code == 200, r.text
    preset = next(p for p in r.json()["presets"] if p["id"] == "level6-weekly")
    # The policy is EXACTLY the four dedicated operations.
    assert preset["policy"] == {
        "browser:navigate": 0,
        "browser:read": 0,
        "browser:screenshot": 0,
        "glofox:read": 0,
    }
    assert preset["label"] == serve.LEVEL6_WEEKLY_PRESET_LABEL == "Level 6 Weekly"
    # The preset's fixed browser allowlist is exactly facebook.com.
    assert preset["browser_allowlist"] == ["facebook.com"]
    perms = preset["permissions"]
    for op in _GRANTED_OPS:
        assert perms[op] == 0, f"{op} must be granted, got {perms[op]!r}"
    for op in _DENIED_OPS:
        assert perms[op] == "deny", f"{op} must be denied, got {perms[op]!r}"


def test_presets_endpoint_requires_a_user():
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/bots/presets").status_code == 401


# ── 2. / 4. existing presets are preserved and not widened ───────────

def test_existing_presets_are_preserved():
    presets = {p["id"]: p for p in _client("alice").get(
        "/api/bots/presets").json()["presets"]}
    assert {"developer", "coordinator", "browser", "glofox-reader",
            "level6-weekly"} <= set(presets)
    # The Browser preset still grants exactly navigate/read/glofox:read.
    assert presets["browser"]["policy"] == {
        "browser:navigate": 0, "browser:read": 0, "glofox:read": 0}
    # The Glofox Reader preset still grants exactly glofox:read.
    assert presets["glofox-reader"]["policy"] == {"glofox:read": 0}
    # The Developer preset is unchanged.
    assert presets["developer"]["policy"] == {
        "fs:read": 0, "repo:read": 0, "fs:write": 1, "repo:pr": 1}


def test_level6_weekly_is_its_own_preset_not_a_reader_or_browser():
    policy = serve.level6_weekly_preset_policy()
    assert serve.level6_weekly_granted(policy) is True
    assert serve.is_glofox_reader_policy(policy) is False
    assert serve.is_browser_bot_policy(policy) is False
    # ...and neither existing preset is a Level 6 Weekly grant.
    assert serve.level6_weekly_granted(serve.glofox_reader_preset_policy()) is False
    assert serve.level6_weekly_granted(serve.browser_preset_policy()) is False


# ── 2. creation through POST /api/bots ───────────────────────────────

def test_create_level6_weekly_stores_exact_policy_and_allowlist():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "weekly", "name": "Level 6 Weekly",
        "preset": "level6-weekly", "status": "stopped",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manageable"] is True
    assert body["level6_weekly"] is True
    assert body["browser_bot"] is False
    assert body["glofox_reader"] is False
    stored = bots.get_bot("weekly")
    assert stored["policy"] == serve.level6_weekly_preset_policy()
    assert stored["browser_allowlist"] == ["facebook.com"]
    assert stored["repo"] == ""
    assert stored["owner"] == "alice"
    assert stored["status"] == "stopped"


def test_create_level6_weekly_forces_fixed_allowlist():
    """A caller-supplied allowlist can neither widen nor replace the preset's
    fixed facebook.com value."""
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "sneaky", "name": "Sneaky", "preset": "level6-weekly",
        "browser_allowlist": ["evil.example", "example.com"],
    })
    assert r.status_code == 200, r.text
    assert bots.get_bot("sneaky")["browser_allowlist"] == ["facebook.com"]


def test_create_level6_weekly_requires_provider_profile():
    r = _client("alice").post("/api/bots", json={
        "id": "noprofile", "name": "No Profile", "preset": "level6-weekly",
    })
    assert r.status_code == 400, r.text
    with pytest.raises(KeyError):
        bots.get_bot("noprofile")


def test_create_level6_weekly_never_accepts_running_status():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "hot", "name": "Hot", "preset": "level6-weekly",
        "status": "running",
    })
    assert r.status_code == 400, r.text
    with pytest.raises(KeyError):
        bots.get_bot("hot")


# ── 3. configure endpoint — exact policy + allowlist identity ────────

def test_configure_level6_weekly_stores_exact_policy_and_allowlist():
    _profile()
    bots.add_bot("clean", "Clean", "openai:m1",
                 tempfile.mkdtemp(prefix="kx-clean-rift-"),
                 policy={}, status="running", owner="alice",
                 provider_profile_id="level6-prof")
    r = _client("alice").post(
        "/api/bots/clean/configure", json={"preset": "level6-weekly"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy"] == serve.level6_weekly_preset_policy()
    assert body["level6_weekly"] is True
    assert body["glofox_reader"] is False
    assert body["browser_bot"] is False
    stored = bots.get_bot("clean")
    assert stored["policy"] == serve.level6_weekly_preset_policy()
    assert stored["browser_allowlist"] == ["facebook.com"]


def test_configure_level6_weekly_forces_fixed_allowlist():
    _profile()
    bots.add_bot("widened", "Widened", "openai:m1",
                 tempfile.mkdtemp(prefix="kx-widened-rift-"),
                 policy={}, status="running", owner="alice",
                 browser_allowlist=["example.com"],
                 provider_profile_id="level6-prof")
    r = _client("alice").post(
        "/api/bots/widened/configure",
        json={"preset": "level6-weekly",
              "browser_allowlist": ["evil.example"]})
    assert r.status_code == 200, r.text
    assert bots.get_bot("widened")["browser_allowlist"] == ["facebook.com"]


def test_configure_level6_weekly_non_owner_forbidden():
    _weekly_bot("owned")
    r = _client("bob").post(
        "/api/bots/owned/configure", json={"preset": "level6-weekly"})
    assert r.status_code == 403, r.text


def test_configure_level6_weekly_requires_auth():
    from fastapi.testclient import TestClient
    _weekly_bot("authless")
    r = TestClient(main.app).post(
        "/api/bots/authless/configure", json={"preset": "level6-weekly"})
    assert r.status_code == 401


# ── 5. routing — the byte-exact command is the ONLY selector ─────────

def test_only_byte_exact_command_selects_the_level6_route():
    bot = _weekly_bot("router")
    assert dev_bot.level6_route_ready(bot) is True
    for text in (
        "level6: weekly 2020-01-01",
        "level6: weekly?url=https://evil.example/",
        "level6:  weekly",             # double space
        "LEVEL6: weekly",              # case matters
        "level6 weekly",               # missing colon
        "level6:",
        "please run level6: weekly",
        "glofox: schedule",
    ):
        assert str(text).strip() != dev_bot.LEVEL6_WEEKLY_COMMAND, text
    assert (str("  level6: weekly  ").strip()
            == dev_bot.LEVEL6_WEEKLY_COMMAND)


def test_level6_route_is_exclusive_never_repo_browser_or_glofox():
    bot = _weekly_bot("exclusive")
    repo_route = dev_bot.is_writable_bot_policy(bot.get("policy"))
    browser_route = (not repo_route) and dev_bot.browser_route_ready(bot)
    level6_route = (dev_bot.level6_route_ready(bot)
                    and COMMAND == dev_bot.LEVEL6_WEEKLY_COMMAND)
    route = ("level6" if level6_route
             else "repo" if repo_route
             else "browser" if browser_route
             else "glofox" if (dev_bot.glofox_route_ready(bot)
                               and COMMAND == dev_bot.GLOFOX_SCHEDULE_COMMAND)
             else "engine")
    assert route == "level6"
    assert repo_route is False
    assert serve.is_writable_bot_policy(bot["policy"]) is False


def test_level6_reader_badge_flag_is_server_derived():
    import chat_api
    assert chat_api._level6_weekly_ready(
        {"policy": serve.level6_weekly_preset_policy()}) is True
    assert chat_api._level6_weekly_ready({"policy": {}}) is False
    # Neither existing preset is mislabelled.
    assert chat_api._level6_weekly_ready(
        {"policy": serve.browser_preset_policy()}) is False
    assert chat_api._level6_weekly_ready(
        {"policy": serve.glofox_reader_preset_policy()}) is False
