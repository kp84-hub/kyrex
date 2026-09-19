"""Focused tests for the "Calendar Reader" bot preset — usable from Kyrex
Chat without hand-crafted production records.

Proves the least-privilege Google Calendar read preset against the EXISTING
owner-scoped endpoints — no second policy model, and no bypass of the
ownership / capability checks:

  1. the presets surface exposes the calendar-reader preset with its effective,
     host-derived permissions: EXACTLY the pinned ``cal:list`` read is granted;
     every browser / write / mail / glofox / calendar-create / coordination
     operation stays denied;
  2. creating a Bot with ``preset: "calendar-reader"`` through the EXISTING
     owner-scoped ``POST /api/bots`` endpoint stores EXACTLY the preset policy
     and REFUSES a browser domain allowlist (a Calendar Reader has no browser
     surface);
  3. enabling the preset on an existing Bot through the EXISTING
     ``POST /api/bots/{id}/configure`` endpoint stores EXACTLY the preset policy
     and refuses a non-empty allowlist OR a Browser Host binding;
  4. the existing presets are PRESERVED: no preset is widened, and the
     calendar-reader preset is not a Glofox Reader, Browser, or Level 6 Weekly
     Bot;
  5. routing — the byte-exact commands ``calendar: today|tomorrow|week`` route
     to the calendar reader ONLY for a configured Calendar Reader Bot; an
     ordinary Bot (write-capable, browser, or unconfigured) can NEVER invoke it,
     and any other ``calendar:`` text fails closed.

Run: python3 -m pytest test_bot_calendar_reader_config.py
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault(
    "KYREX_DATA_DIR", tempfile.mkdtemp(prefix="kyrex-bot-calendar-"))
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-calendar-test-secret")

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

# EXACTLY the one operation a Calendar Reader is granted.
_GRANTED_OPS = ("cal:list",)

# Every capability a Calendar Reader must NEVER have.
_DENIED_OPS = (
    "browser:navigate", "browser:read", "browser:click", "browser:type",
    "browser:upload", "browser:download", "browser:submit", "browser:delete",
    "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "glofox:read", "bot:delegate",
)

COMMAND = serve.CALENDAR_TASK_TODAY            # the byte-exact "calendar: today"


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


def _profile(owner="alice", profile_id="cal-prof", models=("m1",)):
    import provider_profiles
    return provider_profiles.save_profile(owner, {
        "id": profile_id,
        "name": profile_id.upper(),
        "provider": "openai",
        "base_url": "https://example.test/v1",
        "api_key": "sk-secret-calendar",
        "models": list(models),
    })


def _reader_bot(bot_id="reader", owner="alice", status="running",
                policy=None, allowlist=(), profile=True):
    """A Calendar Reader as the preset would create it."""
    if profile:
        _profile(owner)
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "openai:m1",
        tempfile.mkdtemp(prefix=f"kx-{bot_id}-rift-"),
        policy=serve.calendar_reader_preset_policy() if policy is None
        else policy,
        status=status, owner=owner,
        browser_allowlist=list(allowlist),
        provider_profile_id="cal-prof" if profile else "",
    )


def _route_of(bot, text):
    """Mirror chat_service's route precedence for a Calendar Reader message."""
    repo_route = dev_bot.is_writable_bot_policy(bot.get("policy"))
    browser_route = (not repo_route) and dev_bot.browser_route_ready(bot)
    glofox_route = (not repo_route and not browser_route
                    and dev_bot.glofox_route_ready(bot)
                    and str(text).strip() == dev_bot.GLOFOX_SCHEDULE_COMMAND)
    level6_route = dev_bot.level6_route_ready(bot) \
        and str(text).strip() == dev_bot.LEVEL6_WEEKLY_COMMAND
    calendar_route = (dev_bot.calendar_route_ready(bot)
                      and str(text).strip() in dev_bot.CALENDAR_COMMANDS)
    calendar_unsupported = (str(text).strip().lower().startswith("calendar:")
                            and str(text).strip()
                            not in dev_bot.CALENDAR_COMMANDS)
    return ("calendar" if calendar_route
            else "calendar_unsupported" if calendar_unsupported
            else "level6" if level6_route
            else "repo" if repo_route
            else "browser" if browser_route
            else "glofox" if glofox_route else "engine")


# ── 1. the presets surface exposes the calendar-reader preset ────────

def test_presets_endpoint_exposes_calendar_reader_effective_permissions():
    r = _client("alice").get("/api/bots/presets")
    assert r.status_code == 200, r.text
    preset = next(p for p in r.json()["presets"] if p["id"] == "calendar-reader")
    assert preset["policy"] == {"cal:list": 0}
    assert preset["label"] == serve.CALENDAR_READER_PRESET_LABEL == \
        "Calendar Reader"
    perms = preset["permissions"]
    for op in _GRANTED_OPS:
        assert perms[op] == 0, f"{op} must be granted, got {perms[op]!r}"
    for op in _DENIED_OPS:
        assert perms[op] == "deny", f"{op} must be denied, got {perms[op]!r}"
    # A Calendar Reader has NO fixed browser allowlist.
    assert not preset.get("browser_allowlist")


def test_presets_endpoint_requires_a_user():
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/bots/presets").status_code == 401


# ── 4. existing presets are preserved and not widened ────────────────

def test_existing_presets_are_preserved():
    presets = {p["id"]: p for p in _client("alice").get(
        "/api/bots/presets").json()["presets"]}
    assert {"developer", "coordinator", "browser", "glofox-reader",
            "calendar-reader", "level6-weekly"} <= set(presets)
    assert presets["browser"]["policy"] == {
        "browser:navigate": 0, "browser:read": 0, "glofox:read": 0}
    assert presets["glofox-reader"]["policy"] == {"glofox:read": 0}
    assert presets["developer"]["policy"] == {
        "fs:read": 0, "repo:read": 0, "fs:write": 1, "repo:pr": 1}
    assert presets["level6-weekly"]["policy"] == {
        "browser:navigate": 0, "browser:read": 0, "browser:screenshot": 0,
        "glofox:read": 0}


def test_calendar_reader_is_its_own_preset():
    policy = serve.calendar_reader_preset_policy()
    assert serve.is_calendar_reader_policy(policy) is True
    assert serve.is_glofox_reader_policy(policy) is False
    assert serve.is_browser_bot_policy(policy) is False
    assert serve.level6_weekly_granted(policy) is False
    # ...and no other preset is a Calendar Reader.
    assert serve.is_calendar_reader_policy(
        serve.glofox_reader_preset_policy()) is False
    assert serve.is_calendar_reader_policy(
        serve.browser_preset_policy()) is False
    assert serve.is_calendar_reader_policy({}) is False


# ── 2. creation through POST /api/bots ───────────────────────────────

def test_create_calendar_reader_stores_exact_policy():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "cal", "name": "Calendar Reader",
        "preset": "calendar-reader", "status": "stopped",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manageable"] is True
    assert body["calendar_reader"] is True
    assert body["glofox_reader"] is False
    assert body["browser_bot"] is False
    assert body["level6_weekly"] is False
    stored = bots.get_bot("cal")
    assert stored["policy"] == serve.calendar_reader_preset_policy()
    assert not stored.get("browser_allowlist")
    assert stored["repo"] == ""
    assert stored["owner"] == "alice"
    assert stored["status"] == "stopped"


def test_create_calendar_reader_refuses_a_browser_allowlist():
    """A Calendar Reader has NO browser surface: a caller-supplied allowlist
    fails closed before any record is written."""
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "sneaky", "name": "Sneaky", "preset": "calendar-reader",
        "browser_allowlist": ["example.com"],
    })
    assert r.status_code == 409, r.text
    with pytest.raises(KeyError):
        bots.get_bot("sneaky")


def test_create_calendar_reader_requires_provider_profile():
    r = _client("alice").post("/api/bots", json={
        "id": "noprofile", "name": "No Profile", "preset": "calendar-reader",
    })
    assert r.status_code == 400, r.text
    with pytest.raises(KeyError):
        bots.get_bot("noprofile")


def test_create_calendar_reader_never_accepts_running_status():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "hot", "name": "Hot", "preset": "calendar-reader",
        "status": "running",
    })
    assert r.status_code == 400, r.text
    with pytest.raises(KeyError):
        bots.get_bot("hot")


# ── 3. configure endpoint ────────────────────────────────────────────

def test_configure_calendar_reader_stores_exact_policy():
    _profile()
    bots.add_bot("clean", "Clean", "openai:m1",
                 tempfile.mkdtemp(prefix="kx-clean-rift-"),
                 policy={}, status="running", owner="alice",
                 provider_profile_id="cal-prof")
    r = _client("alice").post(
        "/api/bots/clean/configure", json={"preset": "calendar-reader"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy"] == serve.calendar_reader_preset_policy()
    assert body["calendar_reader"] is True
    assert body["glofox_reader"] is False
    assert body["browser_bot"] is False
    assert bots.get_bot("clean")["policy"] == \
        serve.calendar_reader_preset_policy()


def test_configure_calendar_reader_refuses_browser_allowlist():
    _profile()
    bots.add_bot("widened", "Widened", "openai:m1",
                 tempfile.mkdtemp(prefix="kx-widened-rift-"),
                 policy={}, status="running", owner="alice",
                 browser_allowlist=["example.com"],
                 provider_profile_id="cal-prof")
    r = _client("alice").post(
        "/api/bots/widened/configure",
        json={"preset": "calendar-reader"})
    assert r.status_code == 409, r.text
    assert bots.get_bot("widened")["policy"] == {}


def test_configure_calendar_reader_refuses_browser_host_binding():
    _profile()
    bots.add_bot("bound", "Bound", "openai:m1",
                 tempfile.mkdtemp(prefix="kx-bound-rift-"),
                 policy={}, status="running", owner="alice",
                 provider_profile_id="cal-prof")
    bh.enroll_host("alice", "alice-host", name="Alice Host",
                   allowlist=["example.com"])
    bh.bind_bot("alice", "bound", "alice-host")
    r = _client("alice").post(
        "/api/bots/bound/configure", json={"preset": "calendar-reader"})
    assert r.status_code == 409, r.text
    assert bots.get_bot("bound")["policy"] == {}


def test_configure_calendar_reader_non_owner_forbidden():
    _reader_bot("owned")
    r = _client("bob").post(
        "/api/bots/owned/configure", json={"preset": "calendar-reader"})
    assert r.status_code == 403, r.text


def test_configure_calendar_reader_requires_auth():
    from fastapi.testclient import TestClient
    _reader_bot("authless")
    r = TestClient(main.app).post(
        "/api/bots/authless/configure", json={"preset": "calendar-reader"})
    assert r.status_code == 401


# ── 5. routing — configured readers only; ordinary bots never ────────

def test_only_a_configured_reader_routes_the_calendar_commands():
    reader = _reader_bot("router")
    assert dev_bot.calendar_route_ready(reader) is True
    for command in dev_bot.CALENDAR_COMMANDS:
        assert _route_of(reader, command) == "calendar"


def test_ordinary_bots_cannot_invoke_the_calendar_reader():
    developer = _reader_bot("dev", policy=serve.developer_preset_policy())
    browser = _reader_bot("brw", policy=serve.browser_preset_policy())
    empty = _reader_bot("none", policy={})
    wildcard = _reader_bot("star", policy={"*": 0})
    for bot in (developer, browser, empty, wildcard):
        assert dev_bot.calendar_route_ready(bot) is False, bot["id"]
        # "calendar: today" is a valid command but the Bot is not a reader,
        # so it must NEVER reach the calendar route.
        assert _route_of(bot, COMMAND) != "calendar", bot["id"]
    # A plain (no-grant) Bot stays on the ordinary engine path -- the reported
    # bug when the preset was unreachable from Bot Settings.
    assert _route_of(empty, COMMAND) == "engine"
    assert _route_of(browser, COMMAND) == "engine"


def test_unsupported_calendar_text_fails_closed_on_a_reader():
    reader = _reader_bot("reader2")
    for text in ("calendar: yesterday", "calendar:today", "calendar: x",
                 "Calendar: today", "calendar: today please"):
        assert _route_of(reader, text) == "calendar_unsupported", text


def test_route_readiness_requires_running_and_the_exact_grant():
    stopped = _reader_bot("stopped", status="stopped")
    assert dev_bot.calendar_route_ready(stopped) is False
    # A raised tier (cal:list handled without approval) is NOT the grant.
    raised = _reader_bot("raised", policy={"cal:list": 1})
    assert dev_bot.calendar_route_ready(raised) is False


def test_submit_calendar_task_rejects_ordinary_and_accepts_reader():
    reader = _reader_bot("submit-reader")
    task_id = dev_bot.submit_calendar_task("alice", reader, COMMAND)
    assert isinstance(task_id, str) and task_id.strip()

    ordinary = _reader_bot("submit-ordinary", policy={})
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_calendar_task("alice", ordinary, COMMAND)
    # An unsupported command is refused even for a real reader.
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_calendar_task("alice", reader, "calendar: yesterday")


# ── badge flag is server-derived ─────────────────────────────────────

def test_calendar_reader_badge_flag_is_server_derived():
    import chat_api
    assert chat_api._calendar_reader_ready(
        {"policy": serve.calendar_reader_preset_policy()}) is True
    assert chat_api._calendar_reader_ready({"policy": {}}) is False
    # No other preset is mislabelled as a Calendar Reader.
    assert chat_api._calendar_reader_ready(
        {"policy": serve.glofox_reader_preset_policy()}) is False
    assert chat_api._calendar_reader_ready(
        {"policy": serve.browser_preset_policy()}) is False
    assert chat_api._calendar_reader_ready(
        {"policy": serve.level6_weekly_preset_policy()}) is False
