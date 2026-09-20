"""Focused tests for the "Level 6 Calendar" bot preset — usable from Kyrex
Chat without hand-crafted production records.

Proves the dedicated pinned calendar-week preset against the EXISTING
owner-scoped endpoints — no second policy model, and no bypass of the
ownership / capability checks:

  1. the presets surface exposes the level6-calendar preset with its
     effective, host-derived permissions: EXACTLY the two tier-0 operations
     the pinned ``level6: calendar`` command performs (``cal:list`` +
     ``glofox:read``) are granted; every other browser / write / mail /
     calendar-create / coordination operation stays denied; and the preset
     exposes NO browser allowlist (it has no browser surface);
  2. creating a Bot with ``preset: "level6-calendar"`` through the EXISTING
     owner-scoped ``POST /api/bots`` endpoint stores EXACTLY the preset
     policy, and the server exposes the ``level6_calendar`` flag;
  3. enabling the preset on an existing Bot through the EXISTING
     ``POST /api/bots/{id}/configure`` endpoint stores EXACTLY the preset
     policy; the configure endpoint REFUSES (409) a browser allowlist or a
     Browser Host binding on a Level 6 Calendar Bot;
  4. the existing presets are PRESERVED: the Calendar Reader, Glofox Reader
     and Level 6 Weekly presets are unchanged and none is widened, and the
     level6-calendar preset is not a Calendar Reader, a Glofox Reader, or a
     Level 6 Weekly Bot;
  5. the byte-exact command ``level6: calendar`` is the ONLY command the
     preset routes; the route is exclusive (level6, never repo/browser/
     glofox/calendar).

Run: python3 -m pytest test_bot_level6_calendar_config.py
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault(
    "KYREX_DATA_DIR", tempfile.mkdtemp(prefix="kyrex-bot-l6cal-"))
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-l6cal-test-secret")

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

# EXACTLY the two operations a Level 6 Calendar Bot is granted (all tier 0).
_GRANTED_OPS = ("cal:list", "glofox:read")

# Every capability a Level 6 Calendar Bot must NEVER have.
_DENIED_OPS = (
    "browser:navigate", "browser:read", "browser:click", "browser:type",
    "browser:upload", "browser:download", "browser:screenshot",
    "browser:submit", "browser:delete",
    "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "bot:delegate",
)

COMMAND = serve.LEVEL6_CALENDAR_TASK_TEXT  # the byte-exact "level6: calendar"


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
    bh.enroll_host("alice", "alice-host", name="Alice Host",
                   allowlist=["example.com"])


def teardown_function():
    _reset()


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _profile(owner="alice", profile_id="l6cal-prof", models=("m1",)):
    import provider_profiles
    return provider_profiles.save_profile(owner, {
        "id": profile_id,
        "name": profile_id.upper(),
        "provider": "openai",
        "base_url": "https://example.test/v1",
        "api_key": "sk-secret-l6cal",
        "models": list(models),
    })


def _l6cal_bot(bot_id="l6cal", owner="alice", status="running",
               policy=None, allowlist=None, profile=True):
    if profile:
        _profile(owner)
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "openai:m1",
        tempfile.mkdtemp(prefix=f"kx-{bot_id}-rift-"),
        policy=serve.level6_calendar_preset_policy() if policy is None
        else policy,
        status=status, owner=owner,
        browser_allowlist=allowlist or [],
        provider_profile_id="l6cal-prof" if profile else "",
    )


# ── 1. the presets surface exposes the level6-calendar preset ──────────

def test_presets_endpoint_exposes_level6_calendar_effective_permissions():
    r = _client("alice").get("/api/bots/presets")
    assert r.status_code == 200, r.text
    preset = next(p for p in r.json()["presets"] if p["id"] == "level6-calendar")
    # The policy is EXACTLY the two dedicated operations.
    assert preset["policy"] == {
        "cal:list": 0,
        "glofox:read": 0,
    }
    assert preset["label"] == serve.LEVEL6_CALENDAR_PRESET_LABEL == \
        "Level 6 Calendar"
    # The preset has NO browser surface — no allowlist key at all.
    assert "browser_allowlist" not in preset
    perms = preset["permissions"]
    for op in _GRANTED_OPS:
        assert perms[op] == 0, f"{op} must be granted, got {perms[op]!r}"
    for op in _DENIED_OPS:
        assert perms[op] == "deny", f"{op} must be denied, got {perms[op]!r}"


def test_presets_endpoint_requires_a_user():
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/bots/presets").status_code == 401


# ── 4. existing presets are preserved and not widened ──────────────────

def test_existing_presets_are_preserved():
    presets = {p["id"]: p for p in _client("alice").get(
        "/api/bots/presets").json()["presets"]}
    assert {"developer", "coordinator", "browser", "glofox-reader",
            "calendar-reader", "level6-weekly", "level6-calendar"} \
        <= set(presets)
    # The Calendar Reader preset still grants exactly cal:list.
    assert presets["calendar-reader"]["policy"] == {"cal:list": 0}
    # The Glofox Reader preset still grants exactly glofox:read.
    assert presets["glofox-reader"]["policy"] == {"glofox:read": 0}
    # The Level 6 Weekly preset still grants the four capture ops.
    assert presets["level6-weekly"]["policy"] == {
        "browser:navigate": 0, "browser:read": 0,
        "browser:screenshot": 0, "glofox:read": 0}
    # The Developer preset is unchanged.
    assert presets["developer"]["policy"] == {
        "fs:read": 0, "repo:read": 0, "fs:write": 1, "repo:pr": 1}


def test_level6_calendar_is_its_own_preset_not_a_reader_or_glofox():
    policy = serve.level6_calendar_preset_policy()
    assert serve.level6_calendar_granted(policy) is True
    assert serve.is_calendar_reader_policy(policy) is False
    assert serve.is_glofox_reader_policy(policy) is False
    assert serve.level6_weekly_granted(policy) is False
    # ...and neither existing preset is a Level 6 Calendar grant.
    assert serve.level6_calendar_granted(
        serve.calendar_reader_preset_policy()) is False
    assert serve.level6_calendar_granted(
        serve.glofox_reader_preset_policy()) is False
    assert serve.level6_calendar_granted(
        serve.level6_weekly_preset_policy()) is False


# ── 2. creation through POST /api/bots ─────────────────────────────────

def test_create_level6_calendar_stores_exact_policy():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "l6cal", "name": "Level 6 Calendar",
        "preset": "level6-calendar", "status": "stopped",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manageable"] is True
    assert body["level6_calendar"] is True
    assert body["calendar_reader"] is False
    assert body["glofox_reader"] is False
    assert body["level6_weekly"] is False
    stored = bots.get_bot("l6cal")
    assert stored["policy"] == serve.LEVEL6_CALENDAR_PRESET
    assert stored["browser_allowlist"] == []


# ── 3. configure through POST /api/bots/{id}/configure ─────────────────

def test_configure_level6_calendar_stores_exact_policy():
    _l6cal_bot(bot_id="l6cal", policy={"fs:read": 0})
    _client("alice").post("/api/bots/l6cal/configure",
                          json={"preset": "level6-calendar"})
    stored = bots.get_bot("l6cal")
    assert stored["policy"] == serve.LEVEL6_CALENDAR_PRESET
    assert stored["browser_allowlist"] == []


def test_configure_level6_calendar_refuses_a_browser_allowlist():
    _l6cal_bot(bot_id="l6cal", policy={"fs:read": 0}, allowlist=["facebook.com"])
    r = _client("alice").post("/api/bots/l6cal/configure",
                              json={"preset": "level6-calendar"})
    assert r.status_code == 409, r.text
    assert "no browser domain allowlist" in r.text
    stored = bots.get_bot("l6cal")
    assert stored["policy"] == {"fs:read": 0}   # nothing written


def test_configure_level6_calendar_refuses_a_browser_host_binding():
    _l6cal_bot(bot_id="l6cal", policy={"fs:read": 0})
    bh.bind_bot("alice", "l6cal", "alice-host")
    try:
        r = _client("alice").post("/api/bots/l6cal/configure",
                                  json={"preset": "level6-calendar"})
        assert r.status_code == 409, r.text
        assert "no Browser Host binding" in r.text
        stored = bots.get_bot("l6cal")
        assert stored["policy"] == {"fs:read": 0}   # nothing written
    finally:
        bh.unbind_bot("alice", "l6cal")


def test_configure_unknown_preset_rejected():
    _l6cal_bot(bot_id="l6cal")
    r = _client("alice").post("/api/bots/l6cal/configure",
                              json={"preset": "level6-calendarx"})
    assert r.status_code == 400, r.text


def test_configure_other_owner_forbidden():
    _l6cal_bot(bot_id="l6cal", owner="bob")
    r = _client("alice").post("/api/bots/l6cal/configure",
                              json={"preset": "level6-calendar"})
    assert r.status_code == 403, r.text


# ── 5. the byte-exact command is the ONLY selector ─────────────────────

def test_level6_calendar_routes_exactly_and_exclusively():
    bot = _l6cal_bot()
    assert dev_bot.level6_calendar_route_ready(bot) is True
    # A CalendaarReader or Glofox Reader alone is NOT route-ready for it.
    reader = bots.add_bot(
        "reader", "Reader", "openai:m1", tempfile.mkdtemp(prefix="kx-r-"),
        policy=serve.calendar_reader_preset_policy(), status="running",
        owner="alice", browser_allowlist=[],
        provider_profile_id="l6cal-prof")
    assert dev_bot.level6_calendar_route_ready(reader) is False
    glofox = bots.add_bot(
        "glofox", "Glofox", "openai:m1", tempfile.mkdtemp(prefix="kx-g-"),
        policy=serve.glofox_reader_preset_policy(), status="running",
        owner="alice", browser_allowlist=[],
        provider_profile_id="l6cal-prof")
    assert dev_bot.level6_calendar_route_ready(glofox) is False
    # The weekly grant never routes the calendar command.
    weekly = bots.add_bot(
        "weekly", "Weekly", "openai:m1", tempfile.mkdtemp(prefix="kx-w-"),
        policy=serve.level6_weekly_preset_policy(), status="running",
        owner="alice",
        browser_allowlist=serve.level6_weekly_preset_allowlist(),
        provider_profile_id="l6cal-prof")
    assert dev_bot.level6_calendar_route_ready(weekly) is False
    # A stopped bot is never route-ready.
    stopped = _l6cal_bot(bot_id="l6cal-s", status="stopped")
    assert dev_bot.level6_calendar_route_ready(stopped) is False
    # The submission gate requires the byte-exact command text.
    import pytest as _pytest
    from dev_bot import DevBotError
    with _pytest.raises(DevBotError):
        dev_bot.submit_level6_calendar_task(
            "alice", bot, "level6: calendar 2020-01-01", store=None)