"""Per-Bot LLM configuration: a Bot references an encrypted provider profile.

Proves the contract end to end:

  1. The Bot registry stores ONLY a profile reference + the exact model —
     never a copy of an API key or header value.
  2. Resolution (``bot_provider.resolve_bot_provider``) uses the referenced
     profile's provider / base URL / key / headers / model.
  3. Fail-closed resolution: an unconfigured Bot, a missing/foreign profile,
     a model outside the profile, or a keyless profile all raise — never a
     silent fall back to the global KYREX_* provider.
  4. The executor path (``serve.build_context`` + ``apply_bot_identity_env``)
     is driven by the profile and strips the globals when a configured Bot's
     profile cannot be resolved.
  5. Read views expose only non-secret data: profile name/provider/base
     URL/model list/last-four and header NAMES — never a key or header value.
  6. The Bot create/update APIs accept and validate the reference
     (owner-scoped; the model must belong to the profile).

Run: python3 -m pytest test_bot_provider_profile.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bot-provider-tests")
# Fernet key material for the encrypted provider-profile store.
os.environ.setdefault("WEB_SESSION_SECRET", "bot-provider-test-secret")


@pytest.fixture(autouse=True)
def _scoped_provider_env(monkeypatch):
    """Scope the provider defaults to THIS module's tests.

    Import-time ``os.environ.setdefault`` leaked a whole-session KYREX_MODEL
    default into sibling test modules collected later, starving their own
    defaults (collection order silently decided the winner). monkeypatch
    scopes the values to each test and restores the prior environment, so
    running this file alongside the rest of the suite is deterministic.
    """
    monkeypatch.setenv("KYREX_PROVIDER", "openai")
    monkeypatch.setenv("KYREX_MODEL", "env-default-model")
    monkeypatch.setenv("KYREX_API_KEY", "sk-env-default")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402  (after env setup)
import bots  # noqa: E402
import provider_profiles  # noqa: E402
import bot_provider  # noqa: E402
import serve  # noqa: E402
import chat_service  # noqa: E402


SECRET = "sk-secret-1234"


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    bots.save_bots({})


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()


def _profile(user="alice", profile_id="prof-a", models=None, headers=None):
    return provider_profiles.save_profile(user, {
        "id": profile_id,
        "name": profile_id.upper(),
        "provider": "openai",
        "base_url": "https://example.test/v1",
        "api_key": SECRET,
        "models": models if models is not None else ["m1", "m2"],
        "headers": headers if headers is not None else {"X-Extra": "shh-value"},
    })


def _rift():
    return tempfile.mkdtemp(prefix="kyrex-botprovider-rift-")


def _bot(bot_id="b1", owner="alice", model="m1", profile="prof-a", rift=None):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", model, rift or _rift(),
        owner=owner, status="running", provider_profile_id=profile,
    )


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


# ── 1. registry stores a reference, never a secret ─────────────────

def test_registry_stores_only_a_reference_and_model():
    _profile()
    bot = _bot()
    assert bot["provider_profile_id"] == "prof-a"
    assert bot["model"] == "m1"
    # The persisted registry must not contain the secret anywhere.
    raw = Path(bots.BOTS_FILE).read_text()
    assert SECRET not in raw
    assert "shh-value" not in raw


def test_registry_validates_profile_reference():
    # A non-slug reference is rejected at the registry boundary.
    with pytest.raises(ValueError):
        _bot(bot_id="bad", profile="../escape")
    with pytest.raises(ValueError):
        _bot(bot_id="bad2", profile="has space")
    # An empty reference is allowed (an unconfigured Bot).
    assert bots.add_bot("plain", "Plain", "m1", _rift())["provider_profile_id"] == ""


# ── 2. resolution uses the referenced profile ──────────────────────

def test_resolution_uses_profile_credentials():
    _profile()
    bot = _bot()
    cfg = bot_provider.resolve_bot_provider("alice", bot)
    assert cfg["provider"] == "openai"
    assert cfg["api_key"] == SECRET
    assert cfg["base_url"] == "https://example.test/v1"
    assert cfg["model"] == "m1"
    assert cfg["headers"] == {"X-Extra": "shh-value"}
    assert cfg["profile_id"] == "prof-a"


def test_resolution_accepts_provider_prefixed_model():
    _profile()
    bot = _bot(model="openai:m2")
    assert bot_provider.resolve_bot_provider("alice", bot)["model"] == "m2"


# ── 3. fail-closed resolution ──────────────────────────────────────

def test_unconfigured_bot_fails_closed():
    bot = bots.add_bot("plain", "Plain", "m1", _rift(), owner="alice")
    with pytest.raises(bot_provider.BotProviderError) as exc:
        bot_provider.resolve_bot_provider("alice", bot)
    assert "no provider profile" in str(exc.value).lower()


def test_missing_profile_fails_closed():
    bot = _bot(profile="ghost")
    with pytest.raises(bot_provider.BotProviderError) as exc:
        bot_provider.resolve_bot_provider("alice", bot)
    assert "ghost" in str(exc.value)


def test_foreign_profile_fails_closed():
    # The profile belongs to bob; alice's Bot referencing it must not resolve.
    _profile(user="bob", profile_id="prof-a")
    bot = _bot(owner="alice", profile="prof-a")
    with pytest.raises(bot_provider.BotProviderError):
        bot_provider.resolve_bot_provider("alice", bot)


def test_model_outside_profile_fails_closed():
    _profile(models=["m1", "m2"])
    bot = _bot(model="not-in-profile")
    with pytest.raises(bot_provider.BotProviderError) as exc:
        bot_provider.resolve_bot_provider("alice", bot)
    assert "not-in-profile" in str(exc.value)


def test_keyless_profile_fails_closed():
    # Create a profile, then blank its key by writing a fresh value-less one
    # through the internal store.
    _profile()
    raw = provider_profiles._read("alice")
    raw[0]["api_key"] = ""
    provider_profiles._write("alice", raw)
    bot = _bot()
    with pytest.raises(bot_provider.BotProviderError):
        bot_provider.resolve_bot_provider("alice", bot)


# ── 4. executor path uses the profile, never the globals ───────────

def test_build_context_resolves_profile_and_overrides_globals():
    _profile()
    _bot(bot_id="cc")
    ctx = serve.build_context("cc")
    assert ctx.llm_profile_id == "prof-a"
    assert ctx.llm_api_key == SECRET
    assert ctx.llm_error == ""

    env = os.environ.copy()
    env["KYREX_API_KEY"] = "sk-global-should-lose"
    env["KYREX_MODEL"] = "global-model"
    serve.apply_bot_identity_env(env, ctx)
    assert env["KYREX_API_KEY"] == SECRET            # profile key wins
    assert env["KYREX_MODEL"] == "m1"                # exact configured model
    assert env["KYREX_PROVIDER"] == "openai"
    assert env["KYREX_BASE_URL"] == "https://example.test/v1"
    assert json.loads(env["KYREX_PROVIDER_HEADERS"]) == {"X-Extra": "shh-value"}


def test_build_context_strips_globals_when_profile_missing():
    _bot(bot_id="dd", profile="ghost")
    ctx = serve.build_context("dd")
    assert ctx.llm_error and ctx.llm_api_key == ""
    env = os.environ.copy()
    env["KYREX_API_KEY"] = "sk-global"
    env["KYREX_MODEL"] = "global-model"
    serve.apply_bot_identity_env(env, ctx)
    assert env["KYREX_API_KEY"] == ""                # no global fallback
    assert env["KYREX_MODEL"] == ""
    assert env.get("KYREX_PROVIDER_ERROR")


def test_two_bots_same_owner_use_distinct_configs():
    # Two Bots owned by ONE user, on different profiles with different
    # provider, base URL, model AND headers. Each turn's env must carry
    # exactly its own Bot's configuration — never the other's, never a
    # global.
    provider_profiles.save_profile("alice", {
        "id": "prof-x", "name": "Prof X", "provider": "openai",
        "base_url": "https://x.example/v1", "api_key": "sk-x-0001",
        "models": ["mx"], "headers": {"X-Tenant": "x-tenant"},
    })
    provider_profiles.save_profile("alice", {
        "id": "prof-y", "name": "Prof Y", "provider": "anthropic",
        "base_url": "https://y.example", "api_key": "sk-y-0002",
        "models": ["my"], "headers": {"X-Tenant": "y-tenant"},
    })
    bots.add_bot("bx", "Bot X", "mx", _rift(), owner="alice",
                 status="running", provider_profile_id="prof-x")
    bots.add_bot("by", "Bot Y", "my", _rift(), owner="alice",
                 status="running", provider_profile_id="prof-y")

    ctx_x = serve.build_context("bx")
    ctx_y = serve.build_context("by")
    assert (ctx_x.llm_profile_id, ctx_y.llm_profile_id) == ("prof-x", "prof-y")

    base = os.environ.copy()
    for var in ("KYREX_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
        base.pop(var, None)
    env_x = dict(base)
    env_y = dict(base)
    serve.apply_bot_identity_env(env_x, ctx_x)
    serve.apply_bot_identity_env(env_y, ctx_y)

    # Distinct profile, provider, base URL, model and headers per Bot.
    assert env_x["KYREX_PROVIDER"] == "openai"
    assert env_y["KYREX_PROVIDER"] == "anthropic"
    assert env_x["KYREX_API_KEY"] == "sk-x-0001"
    assert env_y["KYREX_API_KEY"] == "sk-y-0002"
    assert env_x["KYREX_BASE_URL"] == "https://x.example/v1"
    assert env_y["ANTHROPIC_BASE_URL"] == "https://y.example"
    assert env_x["KYREX_MODEL"] == "mx"
    assert env_y["KYREX_MODEL"] == "my"
    assert json.loads(env_x["KYREX_PROVIDER_HEADERS"]) == {"X-Tenant": "x-tenant"}
    assert json.loads(env_y["KYREX_PROVIDER_HEADERS"]) == {"X-Tenant": "y-tenant"}
    # No cross-contamination between the two turns.
    assert env_x["KYREX_API_KEY"] != env_y["KYREX_API_KEY"]
    assert env_x.get("ANTHROPIC_BASE_URL") is None
    assert env_y.get("KYREX_BASE_URL") is None


def test_secrets_absent_from_registry_env_and_errors():
    _profile()
    bot = _bot()
    # The registry (and its dump) never carries the key or a header value.
    raw = Path(bots.BOTS_FILE).read_text()
    assert SECRET not in raw and "shh-value" not in raw

    # A fail-closed error names the profile, never the secret.
    ghost = _bot(bot_id="ghost-bot", profile="ghost")
    with pytest.raises(bot_provider.BotProviderError) as exc:
        bot_provider.resolve_bot_provider("alice", ghost)
    assert SECRET not in str(exc.value)

    # A model outside the profile also reports the model, not the key.
    off = _bot(bot_id="off-model", model="nope")
    with pytest.raises(bot_provider.BotProviderError) as exc2:
        bot_provider.resolve_bot_provider("alice", off)
    assert SECRET not in str(exc2.value)

    # A foreign viewer gets an unconfigured view — no profile, no secret.
    foreign = bot_provider.bot_provider_view("bob", bot)
    assert foreign["configured"] is False
    assert SECRET not in json.dumps(foreign)


def test_unconfigured_bot_keeps_legacy_env_behaviour():
    # A Bot with no profile reference is unchanged: the existing model-prefix
    # behaviour still applies (no regression for pre-feature Bots).
    bots.add_bot("legacy", "Legacy", "anthropic:claude-x", _rift(), owner="alice")
    ctx = serve.build_context("legacy")
    assert ctx.llm_api_key == "" and ctx.llm_error == ""
    env = os.environ.copy()
    serve.apply_bot_identity_env(env, ctx)
    assert env["KYREX_PROVIDER"] == "anthropic"
    assert env["KYREX_MODEL"] == "claude-x"
    assert env["KYREX_API_KEY"] == os.environ.get("KYREX_API_KEY")


# ── 5. read views never leak secrets ───────────────────────────────

def test_public_views_never_leak_secrets():
    _profile()
    bot = _bot()

    listed = provider_profiles.list_profiles("alice")
    blob = json.dumps(listed)
    assert SECRET not in blob and "shh-value" not in blob
    assert listed[0]["api_key_last4"] == "1234"
    assert listed[0]["header_names"] == ["X-Extra"]

    view = bot_provider.bot_provider_view("alice", bot)
    vblob = json.dumps(view)
    assert SECRET not in vblob and "shh-value" not in vblob
    assert view["configured"] is True
    assert view["profile"]["api_key_last4"] == "1234"
    assert view["profile"]["name"] == "PROF-A"
    assert view["model"] == "m1"


def test_view_reports_unconfigured_and_missing():
    plain = bots.add_bot("plain", "Plain", "m1", _rift(), owner="alice")
    view = bot_provider.bot_provider_view("alice", plain)
    assert view == {"configured": False, "profile": None, "model": "m1"}

    missing = _bot(bot_id="miss", profile="ghost")
    view2 = bot_provider.bot_provider_view("alice", missing)
    assert view2["configured"] is False
    assert view2["profile"]["missing"] is True


# ── 6. API accepts + validates the reference ───────────────────────

def test_api_create_bot_with_profile_validates():
    _profile(profile_id="prof-b", models=["m9"])

    ok = _client("alice").post("/api/bots", json={
        "id": "withprof", "name": "With Profile", "model": "m9",
        "provider_profile_id": "prof-b",
    })
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["provider_profile_id"] == "prof-b"
    assert body["provider"]["configured"] is True
    assert body["provider"]["profile"]["api_key_last4"] == "1234"
    assert SECRET not in json.dumps(body)

    # A model outside the profile is rejected before anything is written.
    bad_model = _client("alice").post("/api/bots", json={
        "id": "badmodel", "name": "Bad", "model": "nope",
        "provider_profile_id": "prof-b",
    })
    assert bad_model.status_code == 400, bad_model.text
    assert "not available" in bad_model.json()["detail"]

    # An unknown profile reference is rejected.
    bad_prof = _client("alice").post("/api/bots", json={
        "id": "badprof", "name": "Bad", "model": "m9",
        "provider_profile_id": "ghost",
    })
    assert bad_prof.status_code == 400, bad_prof.text
    assert "not configured" in bad_prof.json()["detail"]


def test_api_configure_and_patch_accept_profile_reference():
    _profile(profile_id="prof-c", models=["m1"])
    _bot(bot_id="cfg", owner="alice", model="m1", profile="")

    r = _client("alice").post("/api/bots/cfg/configure", json={
        "provider_profile_id": "prof-c", "model": "m1",
    })
    assert r.status_code == 200, r.text
    assert r.json()["provider_profile_id"] == "prof-c"
    assert bots.get_bot("cfg")["provider_profile_id"] == "prof-c"

    # PATCH can also update the pair.
    _profile(profile_id="prof-d", models=["m2"])
    p = _client("alice").patch("/api/bots/cfg", json={
        "provider_profile_id": "prof-d", "model": "m2",
    })
    assert p.status_code == 200, p.text
    assert p.json()["provider_profile_id"] == "prof-d"
    assert bots.get_bot("cfg")["model"] == "m2"

    # A model that does not belong to the profile is a 400.
    bad = _client("alice").patch("/api/bots/cfg", json={
        "provider_profile_id": "prof-d", "model": "m1",
    })
    assert bad.status_code == 400, bad.text


def test_api_profile_reference_is_owner_scoped():
    _profile(user="alice", profile_id="prof-a", models=["m1"])
    _bot(bot_id="bobs", owner="bob", model="m1", profile="")

    # bob cannot reference alice's profile id on his own Bot.
    r = _client("bob").post("/api/bots/bobs/configure", json={
        "provider_profile_id": "prof-a", "model": "m1",
    })
    assert r.status_code == 400, r.text
    assert "not configured" in r.json()["detail"]
