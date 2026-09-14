"""OpenCode as a selectable provider in Kyrex Chat.

Covers, in order:

1. OpenCode appears in the provider API (and the UI option exists) and is
   selectable via the encrypted provider-profile store.
2. A Bot assigned an OpenCode profile resolves to the exact OpenCode
   endpoint, model, key and headers — and the engine env carries
   KYREX_PROVIDER=opencode with the profile base URL, scrubbing all
   ambient KYREX_* globals (no fallback).
3. OpenRouter behavior is unchanged (plain OpenAI-compatible provider).
4. Two Bots resolve different providers/models independently.
5. No secret (API key or header value) reaches any API view, error,
   registry file, or profile dump.
6. Missing endpoint / key / model fail clearly, the per-conversation
   x-opencode-session id is generated once and stays stable, and a pure-chat
   OpenCode selection without an endpoint fails clearly.
"""

import json
import os
import sys
import tempfile

os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-opencode-tests")
# Fernet key material for the encrypted provider-profile store.
os.environ.setdefault("WEB_SESSION_SECRET", "opencode-test-secret")
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _scoped_provider_env(monkeypatch):
    """Scope provider defaults to each test (pattern of existing suite)."""
    monkeypatch.setenv("KYREX_PROVIDER", "openai")
    monkeypatch.setenv("KYREX_MODEL", "env-default-model")
    monkeypatch.setenv("KYREX_API_KEY", "sk-env-default")
    monkeypatch.setenv("KYREX_BASE_URL", "")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402  (after env setup)
import bots  # noqa: E402
import bot_provider  # noqa: E402
import chat_service  # noqa: E402
import provider_profiles  # noqa: E402
import serve  # noqa: E402

SECRET = "sk-opencode-secret-9931"
OPENCODE_URL = "https://opencode.ai/zen/go/v1"
OPENROUTER_URL = "https://openrouter.ai/api/v1"


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


def _save_profile(user="alice", profile_id="oc1", provider="opencode",
                  base_url=OPENCODE_URL, models=None, headers=None,
                  api_key=SECRET):
    return provider_profiles.save_profile(user, {
        "id": profile_id,
        "name": profile_id.upper(),
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "models": models if models is not None else ["grok-4.6", "glm-5.3"],
        "headers": headers if headers is not None else {"X-Tenant": "tenant-a"},
    })


def _write_profiles_raw(user, profiles):
    """Persist profiles directly, bypassing save_profile validation."""
    provider_profiles._write(user, profiles)


def _rift():
    return tempfile.mkdtemp(prefix="kyrex-opencode-rift-")


def _bot(bot_id="oc-bot", owner="alice", model="grok-4.6", profile="oc1",
         rift=None):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", model, rift or _rift(),
        owner=owner, status="running", provider_profile_id=profile,
    )


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


# ── 1. OpenCode appears in the UI / API ────────────────────────────

def test_opencode_provider_appears_in_api():
    _save_profile()
    with _client() as client:
        providers = client.get("/api/chat/providers").json()["providers"]
        assert any(p["id"] == "oc1" and p["provider"] == "opencode"
                   for p in providers)


def test_opencode_option_exists_in_ui():
    """The ProviderSettings form offers OpenCode with the gateway endpoint."""
    src = os.path.normpath(os.path.join(
        _BACKEND, "..", "..", "..", "kyrex-chat", "src",
        "components", "ProviderSettings.jsx"))
    text = open(src).read()
    assert "'opencode'" in text and OPENCODE_URL in text


def test_public_profile_view_never_carries_secrets():
    _save_profile(headers={"X-Tenant": "tenant-a"})
    with _client() as client:
        profiles = client.get("/api/chat/provider-profiles").json()["profiles"]
    oc = next(p for p in profiles if p["id"] == "oc1")
    flat = json.dumps(oc)
    assert SECRET not in flat
    assert "tenant-a" not in flat
    assert oc["has_api_key"] is True
    assert oc["api_key_last4"] == SECRET[-4:]
    assert oc["header_names"] == ["X-Tenant"]


# ── 2. OpenCode Bot resolution + engine env wiring ─────────────────

def test_opencode_bot_resolves_endpoint_model_headers():
    _save_profile()
    cfg = bot_provider.resolve_bot_provider("alice", _bot())
    assert cfg["provider"] == "opencode"
    assert cfg["base_url"] == OPENCODE_URL
    assert cfg["model"] == "grok-4.6"
    assert cfg["api_key"] == SECRET
    assert cfg["headers"] == {"X-Tenant": "tenant-a"}


def test_opencode_bot_engine_env_no_global_fallback():
    """The engine env carries ONLY the profile's provider/key/endpoint."""
    _save_profile()
    _bot()
    ctx = serve.build_context("oc-bot")
    assert ctx.llm_error == ""
    env = os.environ.copy()
    env["KYREX_API_KEY"] = "sk-ambient-global"
    env["KYREX_MODEL"] = "ambient-model"
    env["KYREX_BASE_URL"] = "https://ambient.invalid/v1"
    env["OPENAI_BASE_URL"] = "https://ambient.invalid/v1"
    serve.apply_bot_identity_env(env, ctx)
    assert env["KYREX_PROVIDER"] == "opencode"
    assert env["KYREX_API_KEY"] == SECRET
    assert env["KYREX_BASE_URL"] == OPENCODE_URL
    assert env["OPENAI_BASE_URL"] == OPENCODE_URL
    assert env["KYREX_MODEL"] == "grok-4.6"
    assert json.loads(env["KYREX_PROVIDER_HEADERS"]) == {"X-Tenant": "tenant-a"}


def test_opencode_bot_session_header_not_substitutable(monkeypatch):
    """A profile header cannot replace the engine-owned session header."""
    from kyrex.opencode import OPENCODE_SESSION_HEADER
    from kyrex.config import ConfigManager
    _save_profile(headers={"X-Tenant": "tenant-a",
                           OPENCODE_SESSION_HEADER: "forged"})
    cfg = bot_provider.resolve_bot_provider("alice", _bot())
    mgr = ConfigManager.__new__(ConfigManager)
    mgr._data = {"headers": {}}
    # The profile headers reach the engine through KYREX_PROVIDER_HEADERS.
    import json as _json
    monkeypatch.setenv("KYREX_PROVIDER_HEADERS", _json.dumps(cfg["headers"]))
    merged = mgr.get_headers()
    assert merged.get("X-Tenant") == "tenant-a"
    assert OPENCODE_SESSION_HEADER not in merged


# ── 3. OpenRouter unchanged / multi-provider Bots ──────────────────

def test_openrouter_bot_behavior_unchanged():
    _save_profile(profile_id="or1", provider="openrouter",
                  base_url=OPENROUTER_URL, models=["deepseek-chat"],
                  headers={"HTTP-Referer": "https://kyrex.dev"})
    bot = _bot(bot_id="or-bot", model="deepseek-chat", profile="or1")
    cfg = bot_provider.resolve_bot_provider("alice", bot)
    assert cfg["provider"] == "openrouter"
    assert cfg["base_url"] == OPENROUTER_URL
    assert cfg["model"] == "deepseek-chat"
    assert cfg["headers"] == {"HTTP-Referer": "https://kyrex.dev"}


def test_two_bots_use_different_providers_and_models():
    _save_profile()
    _save_profile(profile_id="or1", provider="openrouter",
                  base_url=OPENROUTER_URL, models=["deepseek-chat"],
                  api_key="sk-openrouter-distinct-4242")
    _bot()  # oc-bot on the OpenCode profile
    bots.add_bot("or-bot", "Bot or", "deepseek-chat", _rift(), owner="alice",
                 status="running", provider_profile_id="or1")
    ctx_a, ctx_b = serve.build_context("oc-bot"), serve.build_context("or-bot")
    assert (ctx_a.llm_profile_id, ctx_b.llm_profile_id) == ("oc1", "or1")
    base = os.environ.copy()
    for v in ("KYREX_BASE_URL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
        base.pop(v, None)
    env_a, env_b = dict(base), dict(base)
    serve.apply_bot_identity_env(env_a, ctx_a)
    serve.apply_bot_identity_env(env_b, ctx_b)
    assert env_a["KYREX_PROVIDER"] == "opencode"
    assert env_b["KYREX_PROVIDER"] == "openrouter"
    assert env_a["KYREX_API_KEY"] != env_b["KYREX_API_KEY"]
    assert env_a["KYREX_BASE_URL"] == OPENCODE_URL
    assert env_b["KYREX_BASE_URL"] == OPENROUTER_URL
    assert env_a["KYREX_MODEL"] == "grok-4.6"
    assert env_b["KYREX_MODEL"] == "deepseek-chat"


# ── 4. Clear fail-closed validation ────────────────────────────────

def test_missing_endpoint_fails_clearly():
    _save_profile()
    profiles = provider_profiles._read("alice")
    profiles[0]["base_url"] = ""
    provider_profiles._write("alice", profiles)
    with pytest.raises(bot_provider.BotProviderError) as exc:
        bot_provider.resolve_bot_provider("alice", _bot())
    assert "endpoint" in str(exc.value)


def test_missing_key_fails_clearly():
    provider_profiles._write("alice", [{
        "id": "oc1", "name": "OC1", "provider": "opencode",
        "base_url": OPENCODE_URL, "models": ["grok-4.6"],
        "api_key": "", "headers": {},
    }])
    with pytest.raises(bot_provider.BotProviderError) as exc:
        bot_provider.resolve_bot_provider("alice", _bot())
    assert "no API key" in str(exc.value)


def test_model_outside_profile_fails_clearly():
    _save_profile(models=["glm-5.3"])
    with pytest.raises(bot_provider.BotProviderError) as exc:
        bot_provider.resolve_bot_provider("alice",
                                          _bot(model="grok-4.6"))
    assert "not available" in str(exc.value)


def test_unconfigured_bot_never_borrows_globals():
    _save_profile()
    bots.add_bot("plain", "Plain", "m1", _rift())  # no profile reference
    bot = {"id": "plain", "owner": "alice", "provider_profile_id": "",
           "model": "m1"}
    with pytest.raises(bot_provider.BotProviderError):
        bot_provider.resolve_bot_provider("alice", bot)


def test_pure_chat_opencode_without_endpoint_fails_clearly():
    """A pure-chat OpenCode selection with no endpoint fails clearly."""
    # A (legacy/bad) stored OpenCode profile without a base_url.
    provider_profiles._write("alice", [{
        "id": "oc-direct", "name": "OC Direct", "provider": "opencode",
        "base_url": "", "models": ["grok-4.6"], "api_key": SECRET,
        "headers": {},
    }])
    monkey = pytest.MonkeyPatch()
    monkey.delenv("KYREX_BASE_URL", raising=False)
    monkey.delenv("OPENAI_BASE_URL", raising=False)
    try:
        with pytest.raises(chat_service.ChatUnavailable) as exc:
            chat_service._resolve_provider("oc-direct", "grok-4.6",
                                           user="alice")
        assert "endpoint" in str(exc.value)
    finally:
        monkey.undo()


# ── 5. Per-conversation x-opencode-session generation ──────────────

def test_per_conversation_session_id_stable_and_unique():
    for conv_id in ("conv-a", "conv-b"):
        chat_service._write("alice", {
            "conversation_id": conv_id, "title": "t", "messages": []})
    conv_a = chat_service.get_conversation("alice", "conv-a")
    sid_a1 = chat_service._opencode_session_for("alice", conv_a)
    assert chat_service._opencode_session_for(
        "alice", chat_service.get_conversation("alice", "conv-a")) == sid_a1
    sid_b = chat_service._opencode_session_for(
        "alice", chat_service.get_conversation("alice", "conv-b"))
    assert sid_a1 != sid_b
    assert sid_a1 and sid_b


# ── 6. Secrets in errors / registry / views ────────────────────────

def test_secrets_absent_from_registry_and_errors():
    _save_profile()
    bot = _bot()
    raw = open(bots.BOTS_FILE).read()
    assert SECRET not in raw and "tenant-a" not in raw

    ghost = _bot(bot_id="ghost-bot", profile="ghost")
    with pytest.raises(bot_provider.BotProviderError) as exc:
        bot_provider.resolve_bot_provider("alice", ghost)
    assert SECRET not in str(exc.value)

    view = bot_provider.bot_provider_view("bob", bot)
    assert SECRET not in json.dumps(view)
