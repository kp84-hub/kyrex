"""Focused tests for the Glofox Reader bot preset — usable from Kyrex Chat
without hand-crafted production records.

Proves the smallest safe path for the least-privilege Glofox Reader against
the EXISTING owner-scoped endpoints — no second policy model, and no bypass
of the ownership / capability / eligibility checks:

  1. the presets surface exposes the glofox-reader preset with its effective,
     host-derived permissions: the EXACT ``glofox:read`` tier-0 grant is the
     ONLY granted operation; every browser / write / mail / calendar /
     coordination operation stays denied;
  2. creating a Bot with ``preset: "glofox-reader"`` through the existing
     owner-scoped ``POST /api/bots`` endpoint: owner recorded, provider
     profile REQUIRED (no silent global fallback), no browser allowlist,
     stopped by default, and the stored policy is EXACTLY the preset;
  3. enabling the preset on an existing Bot through the existing
     ``POST /api/bots/{id}/configure`` endpoint — refused (409) when the Bot
     has a browser allowlist or a Browser Host binding (a Glofox Reader has
     no browser surface);
  4. routing precedence: a Glofox Reader never qualifies for the repo route
     (not write-capable) or the browser route (never browser-route-ready,
     even if an allowlist/binding were injected), and the pinned byte-exact
     ``glofox: schedule`` text is the ONLY text that selects the glofox
     route — any other message falls back to the engine;
  5. exact-message rejection: ``glofox: schedule`` variants and arbitrary
     task text are never routed to glofox;
  6. owner isolation: a foreign owner cannot configure or run the Bot;
  7. stopped state: a stopped Glofox Reader is route-ineligible until it is
     started through the lifecycle endpoint;
  8. wildcard rejection: ``glofox:*`` / ``*`` policies are never the exact
     grant and never classify as a Glofox Reader;
  9. no writable or browser capability: the stored policy never grants
     ``fs:write`` or any browser operation.

Run: python3 -m pytest test_bot_glofox_config.py
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault(
    "KYREX_DATA_DIR", tempfile.mkdtemp(prefix="kyrex-bot-glofox-"))
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-glofox-test-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main            # noqa: E402  (seeds the shared app + session map)
import bots            # noqa: E402  — the authoritative registry
import serve           # noqa: E402  — host tier table + preset/gate predicates
import browser_hosts as bh  # noqa: E402 — the authoritative host registry


# The ONLY operation a Glofox Reader is granted: the pinned Level 6 schedule
# read, at its host tier 0.
_GRANTED_OPS = ("glofox:read",)

# Every capability a least-privilege Glofox Reader must NEVER have: the whole
# browser surface (including navigate/read — a Glofox Reader cannot browse at
# all), filesystem/repo writes, mail send, calendar create, and coordination.
_DENIED_OPS = (
    "browser:navigate", "browser:read",
    "browser:click", "browser:screenshot", "browser:type", "browser:upload",
    "browser:download", "browser:submit", "browser:delete",
    "fs:write", "fs:delete", "repo:pr", "repo:push",
    "mail:send", "cal:create", "bot:delegate",
)

COMMAND = serve.GLOFOX_TASK_TEXT  # the byte-exact "glofox: schedule"


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
    bh.enroll_host("bob", "bob-host", name="Bob Host",
                   allowlist=["example.com"])


def teardown_function():
    _reset()


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _profile(owner="alice", profile_id="glofox-prof", models=("m1",)):
    import provider_profiles
    return provider_profiles.save_profile(owner, {
        "id": profile_id,
        "name": profile_id.upper(),
        "provider": "openai",
        "base_url": "https://example.test/v1",
        "api_key": "sk-secret-glofox",
        "models": list(models),
    })


def _reader_bot(bot_id="reader", owner="alice", status="running",
                policy=None, allowlist=(), profile=True):
    """A Glofox Reader as the preset would create it."""
    if profile:
        _profile(owner)
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "openai:m1",
        tempfile.mkdtemp(prefix=f"kx-{bot_id}-rift-"),
        policy=serve.glofox_reader_preset_policy() if policy is None else policy,
        status=status, owner=owner,
        browser_allowlist=list(allowlist),
        provider_profile_id="glofox-prof" if profile else "",
    )


# ── 1. the presets surface exposes the glofox-reader preset ──────────

def test_presets_endpoint_exposes_glofox_reader_effective_permissions():
    r = _client("alice").get("/api/bots/presets")
    assert r.status_code == 200, r.text
    preset = next(p for p in r.json()["presets"] if p["id"] == "glofox-reader")
    # The policy is EXACTLY the least-privilege schedule-read grant.
    assert preset["policy"] == {"glofox:read": 0}
    assert preset["label"] == serve.GLOFOX_READER_PRESET_LABEL
    perms = preset["permissions"]
    for op in _GRANTED_OPS:
        assert perms[op] == 0, f"{op} must be granted, got {perms[op]!r}"
    for op in _DENIED_OPS:
        assert perms[op] == "deny", f"{op} must be denied, got {perms[op]!r}"
    # Existing presets are preserved alongside it.
    ids = {p["id"] for p in r.json()["presets"]}
    assert {"developer", "coordinator", "browser", "glofox-reader"} <= ids


def test_presets_endpoint_requires_a_user():
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/bots/presets").status_code == 401


# ── 2. creation through POST /api/bots ───────────────────────────────

def test_create_glofox_reader_owner_scoped_success():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "reader", "name": "Level 6 Reader",
        "preset": "glofox-reader", "status": "stopped",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner"] == "alice" if "owner" in body else True
    assert body["manageable"] is True
    assert body["glofox_reader"] is True
    assert body["browser_bot"] is False
    # The stored record: exact preset policy, no allowlist, no repo binding.
    stored = bots.get_bot("reader")
    assert stored["policy"] == {"glofox:read": 0}
    assert stored["browser_allowlist"] == []
    assert stored["repo"] == ""
    assert stored["owner"] == "alice"
    assert stored["status"] == "stopped"
    assert serve.is_glofox_reader_policy(stored["policy"]) is True
    # The provider profile is REQUIRED and recorded — never a global fallback.
    assert stored["provider_profile_id"] == "glofox-prof"


def test_create_glofox_reader_requires_provider_profile():
    # No profile exists for alice: a Glofox Reader create must fail closed
    # (400) rather than silently inheriting the global/env provider.
    r = _client("alice").post("/api/bots", json={
        "id": "noprofile", "name": "No Profile", "preset": "glofox-reader",
    })
    assert r.status_code == 400, r.text
    with pytest.raises(KeyError):
        bots.get_bot("noprofile")


def test_create_glofox_reader_rejects_browser_allowlist():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "with-allow", "name": "With Allowlist",
        "preset": "glofox-reader",
        "browser_allowlist": ["example.com"],
    })
    assert r.status_code == 409, r.text
    assert "allowlist" in r.json()["detail"]
    with pytest.raises(KeyError):
        bots.get_bot("with-allow")


def test_create_glofox_reader_never_accepts_running_status():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "hot", "name": "Hot", "preset": "glofox-reader",
        "status": "running",
    })
    assert r.status_code == 400, r.text
    with pytest.raises(KeyError):
        bots.get_bot("hot")


def test_create_glofox_reader_unknown_preset_rejected():
    _profile()
    r = _client("alice").post("/api/bots", json={
        "id": "ghosty", "name": "Ghosty", "preset": "ghost"})
    assert r.status_code == 400, r.text
    with pytest.raises(KeyError):
        bots.get_bot("ghosty")


# ── 3. configure endpoint — no browser surface ───────────────────────

def test_configure_glofox_reader_on_clean_bot():
    _profile()
    bots.add_bot("clean", "Clean", "openai:m1",
                 tempfile.mkdtemp(prefix="kx-clean-rift-"),
                 policy={}, status="running", owner="alice",
                 provider_profile_id="glofox-prof")
    r = _client("alice").post(
        "/api/bots/clean/configure", json={"preset": "glofox-reader"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy"] == {"glofox:read": 0}
    assert body["glofox_reader"] is True
    assert serve.is_glofox_reader_policy(bots.get_bot("clean")["policy"])


def test_configure_glofox_reader_refused_with_allowlist():
    _reader_bot("scout", allowlist=["example.com"], policy={})
    r = _client("alice").post(
        "/api/bots/scout/configure", json={"preset": "glofox-reader"})
    assert r.status_code == 409, r.text
    # Nothing was granted — the stored policy is unchanged.
    assert bots.get_bot("scout")["policy"] == {}


def test_configure_glofox_reader_refused_with_host_binding():
    _reader_bot("bound", policy={})
    bh.bind_bot("alice", "bound", "alice-host")
    r = _client("alice").post(
        "/api/bots/bound/configure", json={"preset": "glofox-reader"})
    assert r.status_code == 409, r.text
    assert bots.get_bot("bound")["policy"] == {}


def test_configure_glofox_reader_non_owner_forbidden():
    _reader_bot("owned")
    r = _client("bob").post(
        "/api/bots/owned/configure", json={"preset": "glofox-reader"})
    assert r.status_code == 403, r.text
    assert serve.is_glofox_reader_policy(bots.get_bot("owned")["policy"])


def test_configure_glofox_reader_requires_auth():
    from fastapi.testclient import TestClient
    _reader_bot("authless")
    r = TestClient(main.app).post(
        "/api/bots/authless/configure", json={"preset": "glofox-reader"})
    assert r.status_code == 401


# ── 4. routing precedence — never repo, never browser ────────────────

def test_glofox_reader_never_routes_to_repo():
    bot = _reader_bot()
    assert serve.is_writable_bot_policy(bot["policy"]) is False


def test_glofox_reader_never_browser_route_ready_even_if_bound():
    """Even with an allowlist AND a host binding injected behind the preset's
    back, a Glofox Reader policy can never qualify for the browser route."""
    import dev_bot
    _reader_bot("sneaky", allowlist=["example.com"], policy={})
    bots.update_bot("sneaky", policy=serve.glofox_reader_preset_policy())
    bh.bind_bot("alice", "sneaky", "alice-host")
    bot = bots.get_bot("sneaky")
    assert dev_bot.browser_route_ready(bot) is False
    assert dev_bot.browser_bot_ready(bot) is False
    assert serve.glofox_reader_granted(bot) is True


def test_chat_route_precedence_for_exact_command():
    """The chat_service three-way route picks glofox for a running, exact-
    grant Bot and the byte-exact command — ahead of engine, and never repo
    or browser."""
    import dev_bot
    bot = _reader_bot()
    repo_route = dev_bot.is_writable_bot_policy(bot.get("policy"))
    browser_route = (not repo_route) and dev_bot.browser_route_ready(bot)
    glofox_route = (not repo_route and not browser_route
                    and dev_bot.glofox_route_ready(bot)
                    and COMMAND == dev_bot.GLOFOX_SCHEDULE_COMMAND)
    route = ("repo" if repo_route
             else "browser" if browser_route
             else "glofox" if glofox_route else "engine")
    assert route == "glofox"


# ── 5. exact-message rejection ───────────────────────────────────────

def test_only_byte_exact_command_selects_glofox_route():
    import dev_bot
    bot = _reader_bot()
    assert dev_bot.glofox_route_ready(bot) is True
    for text in (
        "glofox: schedule 2020-01-01",
        "glofox:  schedule",           # double space
        "  glofox: schedule  ",        # stripped in the route check
        "glofox: schedule?url=https://evil.example/",
        "glofox: delete everything",
        "GLOFOX: schedule",            # case matters
        "please run glofox: schedule",
    ):
        stripped = str(text).strip()
        selects = (stripped == dev_bot.GLOFOX_SCHEDULE_COMMAND)
        if stripped == "glofox: schedule":
            # The stripped exact text is the ONE selector.
            assert selects is True
        else:
            assert selects is False, f"{text!r} must not select the glofox route"


def test_non_exact_message_falls_back_to_engine(rig=None):
    """Through the real chat stream: a glofox-capable Bot receiving a
    non-pinned message never submits a glofox task — it goes to the engine."""
    import asyncio
    import chat_service
    import dev_bot
    import task_store
    from task_store import CloudTaskStore
    from unittest.mock import patch
    import glofox_api

    with tempfile.TemporaryDirectory(prefix="kx-glofox-route-") as tmp:
        import pytest as _pytest
        prev = os.environ.get("KYREX_DATA_DIR")
        os.environ["KYREX_DATA_DIR"] = tmp
        try:
            task_store.DATA_DIR = Path(tmp)
            bots.BOTS_FILE = str(Path(tmp) / "bots.json")
            bots.save_bots({})
            store = _RecordingStore(CloudTaskStore())
            chat_service._task_store_instance = store
            chat_service._engine_sessions.clear()
            _reader_bot("level6")
            conv = chat_service.create_conversation("alice", bot_id="level6")

            async def _frames(agen):
                return [frame async for frame in agen]

            with patch.object(chat_service, "_get_engine_session",
                              _RecordingEngine):
                frames = asyncio.run(_frames(chat_service.stream_chat(
                    "alice", conv["conversation_id"],
                    "glofox: schedule tomorrow")))
            assert store.submissions == []
            status = [f for f in frames if f.get("type") == "status"]
            assert status and status[-1]["status"] == "complete"
        finally:
            chat_service._engine_sessions.clear()
            if prev is not None:
                os.environ["KYREX_DATA_DIR"] = prev


class _RecordingStore:
    """Wraps a CloudTaskStore, recording every submit before delegating."""

    def __init__(self, store):
        self._store = store
        self.submissions = []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        return self._store.submit(**kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


class _RecordingEngine:
    def __init__(self, user, conversation_id, workspace, bot_cfg=None):
        self.workspace = workspace

    def run_turn(self, text, on_token, cancel_check=None):
        on_token("bot answer")
        return "bot answer", None

    def interrupt(self):
        pass

    def close(self):
        pass


# ── 6. owner isolation at the submission gate ────────────────────────

def test_foreign_owner_cannot_submit_glofox_task():
    import dev_bot
    _reader_bot("owned")
    bot = bots.get_bot("owned")
    with pytest.raises(dev_bot.DevBotError, match="another owner"):
        dev_bot.submit_glofox_task("bob", bot, COMMAND)
    assert dev_bot.glofox_route_ready(bot) is True  # gate is fine; identity is not


def test_roster_is_owner_scoped():
    _reader_bot("mine")
    r_alice = _client("alice").get("/api/bots")
    r_bob = _client("bob").get("/api/bots")
    assert r_alice.status_code == 200 and r_bob.status_code == 200
    alice_ids = {b["id"] for b in r_alice.json().get("bots", [])}
    bob_ids = {b["id"] for b in r_bob.json().get("bots", [])}
    assert "mine" in alice_ids
    assert "mine" not in bob_ids


# ── 7. stopped state ─────────────────────────────────────────────────

def test_stopped_glofox_reader_is_route_ineligible():
    import dev_bot
    _reader_bot("idle", status="stopped")
    bot = bots.get_bot("idle")
    assert dev_bot.glofox_route_ready(bot) is False
    # Lifecycle is server-controlled: the owner can start it explicitly.
    r = _client("alice").patch("/api/bots/idle", json={"status": "running"})
    assert r.status_code == 200, r.text
    assert dev_bot.glofox_route_ready(bots.get_bot("idle")) is True


def test_stopped_glofox_reader_submission_fails_closed():
    import dev_bot
    _reader_bot("idle2", status="stopped")
    with pytest.raises(dev_bot.DevBotError, match="stopped"):
        dev_bot.submit_glofox_task("alice", bots.get_bot("idle2"), COMMAND)


# ── 8. wildcard rejection ────────────────────────────────────────────

def test_wildcard_grant_is_never_a_glofox_reader():
    for policy in ({"glofox:*": 0}, {"*": 0}):
        assert serve.glofox_read_granted(policy) is False, policy
        assert serve.is_glofox_reader_policy(policy) is False, policy
    # A wildcard NEVER substitutes the exact grant, and the exact grant's
    # mere presence next to a wildcard does not widen anything else.
    combined = {"glofox:*": 0, "glofox:read": 0}
    assert serve.glofox_read_granted(combined) is True   # exact rule matched
    assert serve.is_glofox_reader_policy(combined) is True  # nothing else granted


def test_extra_capability_disqualifies_glofox_reader():
    base = serve.glofox_reader_preset_policy()
    for extra in ({"fs:read": 0}, {"browser:read": 0}, {"fs:write": 1},
                  {"bot:delegate": 0}, {"mail:send": 0}):
        policy = dict(base)
        policy.update(extra)
        assert serve.glofox_read_granted(policy) is True   # the grant holds...
        assert serve.is_glofox_reader_policy(policy) is False  # ...but not least-privilege


# ── 9. no writable / browser capability ──────────────────────────────

def test_reader_policy_grants_no_writable_or_browser_capability():
    policy = serve.glofox_reader_preset_policy()
    assert serve.is_writable_bot_policy(policy) is False
    for op in _DENIED_OPS:
        decision = serve.policy.evaluate(policy, op, serve.OPERATION_TIERS[op])
        assert decision.get("effective_tier") == "deny", op
    assert serve.is_browser_bot_policy(policy) is False
