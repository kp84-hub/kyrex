"""Focused API/UI-policy tests for the Calendar Editor (cal:delete) preset.

Proves the Calendar Editor is USER-REACHABLE and least-privilege:

  1. DISCOVERABLE in ``GET /api/bots/presets`` with EXACTLY ``{cal:delete: 2}``;
  2. CREATABLE via ``POST /api/bots {preset: "calendar-editor"}``;
  3. CONFIGURABLE via ``POST /api/bots/{id}/configure {preset: "calendar-editor"}``;
  4. it never inherits the Reader (``cal:list``) or Writer (``cal:create``)
     grants, and it is offered as a primary Bot role;
  5. a delete still requires the owner's Google event-WRITE connection (a
     read-only Reader token can never delete).

Run: python3 -m pytest test_calendar_editor_config.py
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR",
                      tempfile.mkdtemp(prefix="kyrex-cal-editor-cfg-"))
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "cal-editor-cfg-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main            # noqa: E402  (seeds the shared app + session map)
import bots            # noqa: E402
import dev_bot         # noqa: E402
import serve           # noqa: E402
import connectors as C  # noqa: E402

_GRANTED = ("cal:delete",)
_DENIED = (
    "cal:list", "cal:create", "glofox:read", "browser:navigate", "browser:read",
    "browser:click", "browser:type", "browser:submit", "fs:write", "fs:delete",
    "repo:pr", "repo:push", "mail:send", "bot:delegate",
)


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _profile(owner="alice", profile_id="ed-prof"):
    import provider_profiles
    return provider_profiles.save_profile(owner, {
        "id": profile_id, "name": profile_id.upper(), "provider": "openai",
        "base_url": "https://example.test/v1", "api_key": "sk-secret-editor",
        "models": ["m1"],
    })


# ── 1. discoverable ────────────────────────────────────────────────────

def test_presets_endpoint_exposes_calendar_editor():
    r = _client("alice").get("/api/bots/presets")
    assert r.status_code == 200, r.text
    preset = next(p for p in r.json()["presets"] if p["id"] == "calendar-editor")
    assert preset["policy"] == {"cal:delete": 2}
    assert preset["label"] == serve.CALENDAR_EDITOR_PRESET_LABEL == \
        "Calendar Editor"
    perms = preset["permissions"]
    for op in _GRANTED:
        assert perms[op] == 2, f"{op} must be tier 2, got {perms[op]!r}"
    for op in _DENIED:
        assert perms[op] == "deny", f"{op} must be denied, got {perms[op]!r}"
    assert preset.get("write_capability") is True
    assert preset.get("destructive_capability") is True
    assert not preset.get("browser_allowlist")


def test_presets_endpoint_requires_a_user():
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/bots/presets").status_code == 401


# ── 2. creatable ───────────────────────────────────────────────────────

def test_create_calendar_editor_stores_exact_policy():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "deleter", "name": "Calendar Editor",
        "preset": "calendar-editor", "status": "stopped",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manageable"] is True
    assert body["calendar_editor"] is True
    assert body["calendar_reader"] is False
    assert body["calendar_writer"] is False
    stored = bots.get_bot("deleter")
    assert stored["policy"] == {"cal:delete": 2}
    assert not stored.get("browser_allowlist")
    assert stored["repo"] == ""
    assert stored["owner"] == "alice"
    assert stored["status"] == "stopped"


# ── 3. configurable ────────────────────────────────────────────────────

def test_configure_calendar_editor_stores_exact_policy():
    _profile()
    bots.add_bot("clean2", "Clean2", "openai:m1",
                 tempfile.mkdtemp(prefix="kx-clean2-rift-"),
                 policy={}, status="running", owner="alice",
                 provider_profile_id="ed-prof")
    r = _client("alice").post("/api/bots/clean2/configure",
                              json={"preset": "calendar-editor"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy"] == {"cal:delete": 2}
    assert body["calendar_editor"] is True
    assert body["calendar_reader"] is False
    assert body["calendar_writer"] is False
    assert bots.get_bot("clean2")["policy"] == {"cal:delete": 2}


# ── 4. no Reader/Writer inheritance; offered as a primary role ─────────

def test_editor_preset_never_inherits_reader_or_writer_grants():
    policy = serve.calendar_editor_preset_policy()
    assert policy == {"cal:delete": 2}
    assert serve.is_calendar_editor_policy(policy) is True
    assert serve.calendar_editor_granted(policy) is True
    for other in (serve.CALENDAR_READER_PRESET, serve.CALENDAR_WRITER_PRESET,
                  serve.CALENDAR_PRESET, serve.GLOFOX_READER_PRESET):
        assert serve.calendar_editor_granted(other) is False, other
    assert serve.is_calendar_reader_policy(policy) is False
    assert serve.is_calendar_writer_policy(policy) is False
    assert serve.is_calendar_bot_policy(policy) is False
    assert serve.calendar_writer_granted(policy) is False
    assert serve.cal_list_granted(policy) is False

    import bot_roles
    assert bot_roles.role_for_policy(policy) == "calendar-editor"
    assert "calendar-editor" in bot_roles.PRIMARY_ROLE_IDS
    opts = {o["id"]: o for o in bot_roles.capability_options()}
    assert opts["calendar-editor"]["preset"] == "calendar-editor"
    view = bot_roles.role_view(policy)
    assert view["id"] == "calendar-editor" and view["primary"] is True


# ── 5. a delete still needs the owner's event-WRITE connection ─────────

class _FakeStore:
    def __init__(self, scopes):
        self._scopes = list(scopes)

    def route_capability(self, owner, cap, provider="google"):
        role = C.CAPABILITY_ROUTING[cap]
        return {"capability": cap, "connector": provider, "bot_role": role,
                "read_only": C.CAPABILITY_DECLARATIONS[role]["read_only"],
                "available": True}

    def status(self, owner, provider="google"):
        return {"connected": True, "scopes": list(self._scopes)}

    def access_token(self, owner, provider="google"):
        return "fake-token"

    def preferred_calendar(self, owner, provider="google"):
        return "primary"


def test_delete_requires_the_google_event_write_connection():
    read_only = _FakeStore([C.GOOGLE_CALENDAR_READ_SCOPE])
    with pytest.raises(C.ConnectorUnavailable):
        C.CalendarEdit(read_only, "alice",
                       transport=lambda *a, **k: {}).delete_event("l6evtABC12345.xyz")

    write = _FakeStore([C.GOOGLE_CALENDAR_WRITE_SCOPE])
    seen = {}
    C.CalendarEdit(
        write, "alice",
        transport=lambda m, u, t, p=None, b=None: seen.update(m=m, u=u),
    ).delete_event("l6evtABC12345.xyz")
    assert seen["m"] == "DELETE"
    assert seen["u"].endswith("/events/l6evtABC12345.xyz")



def _reset():
    bots.save_bots({})


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()
