"""End-to-end proof: a Bot's configured provider/model reaches its execution.

The sibling suites each cover ONE link of the chain:

  * test_bot_provider_profile.py — the registry stores only a profile REFERENCE
    plus the exact model, and serve.build_context / apply_bot_identity_env
    translate it (the EXECUTOR path); the Bot config APIs validate the pair.
  * test_chat_bot_execution.py   — a Bot-bound turn's model/system_prompt reach
    the engine-session BOUNDARY (a recording stand-in).

What was never asserted is the ONE thing the feature promises: the REAL engine
spawn env of a Bot-bound Kyrex Chat turn. This module drives the whole path —

    Bot config API (the UI surface)
      -> Bot registry (provider_profile_id + model)
      -> serve.build_context (owner-scoped profile resolution)
      -> chat_service.stream_chat (the turn)
      -> EngineSession spawn env (what the engine process actually receives)

— with the Bot configured on a provider/model DIFFERENT from Kyrex Chat, and
proves the two never bleed into each other (checks 1-5 in the task).

Run: python3 -m pytest test_bot_provider_end_to_end.py
"""

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bot-provider-e2e-tests")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-provider-e2e-secret")
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402  (after env setup)
import bots  # noqa: E402
import chat_service  # noqa: E402
import provider_profiles  # noqa: E402
import serve  # noqa: E402


# Kyrex Chat's OWN provider — deliberately different from the Bot's in every
# dimension (provider, model, key, endpoint).
CHAT_PROVIDER = "openai"
CHAT_MODEL = "gpt-chat-default"
CHAT_KEY = "sk-CHAT-global"
CHAT_BASE = "https://chat.example/v1"

# The one Bot under test: a distinct provider, model, key, endpoint, headers.
BOT_PROFILE_ID = "bot-prof"
BOT_PROVIDER = "anthropic"
BOT_MODEL = "claude-bot-e2e"
BOT_KEY = "sk-BOT-profile-key"
BOT_BASE = "https://bot.example/anthropic"
BOT_HEADERS = {"X-Bot-Tenant": "bot-tenant"}


@pytest.fixture(autouse=True)
def _scoped_chat_provider(monkeypatch):
    """Scope Kyrex Chat's provider env to each test.

    Module-level ``os.environ`` writes leak across a whole pytest session (a
    sibling module's own defaults would then win by collection order). Pin the
    Chat config here and restore afterwards.
    """
    monkeypatch.setenv("KYREX_PROVIDER", CHAT_PROVIDER)
    monkeypatch.setenv("KYREX_MODEL", CHAT_MODEL)
    monkeypatch.setenv("KYREX_API_KEY", CHAT_KEY)
    monkeypatch.setenv("KYREX_BASE_URL", CHAT_BASE)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("KYREX_PROVIDER_HEADERS", raising=False)


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    bots.save_bots({})
    chat_service._engine_sessions.clear()


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


def _profile(user="alice", profile_id=BOT_PROFILE_ID, provider=BOT_PROVIDER,
             base_url=BOT_BASE, models=None, api_key=BOT_KEY, headers=None):
    return provider_profiles.save_profile(user, {
        "id": profile_id,
        "name": profile_id.upper(),
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "models": models if models is not None else [BOT_MODEL],
        "headers": headers if headers is not None else dict(BOT_HEADERS),
    })


def _create_bot(user, bot_id, model=BOT_MODEL, profile=BOT_PROFILE_ID):
    """Create + start a Bot through the real config API (the UI surface)."""
    client = _client(user)
    r = client.post("/api/bots", json={
        "id": bot_id, "name": f"Bot {bot_id}",
        "model": model, "provider_profile_id": profile,
    })
    assert r.status_code == 200, r.text
    started = client.patch(f"/api/bots/{bot_id}", json={"status": "running"})
    assert started.status_code == 200, started.text
    return bots.get_bot(bot_id)


def _env_for_ctx(ctx):
    """The executor env a bound Bot's context produces (serve.py path)."""
    env = os.environ.copy()
    serve.apply_bot_identity_env(env, ctx)
    return env


def _capture_bot_spawn(user, conversation_id):
    """Run the REAL turn path and capture the engine spawn env.

    subprocess.Popen is stubbed (the process never really starts, so the
    handshake times out) — the env dict is captured before that, which is
    exactly the boundary the engine process receives.
    """
    captured = {}

    def fake_popen(*args, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.stdout = io.StringIO()
        proc.stderr = io.StringIO()
        proc.stdin = io.StringIO()
        return proc

    async def _run():
        try:
            async for _frame in chat_service.stream_chat(user, conversation_id, "hi"):
                pass
        except Exception:  # noqa: BLE001 — the stub never handshakes; env captured
            pass

    with patch("subprocess.Popen", side_effect=fake_popen), \
         patch.object(chat_service, "ENGINE_HANDSHAKE_TIMEOUT", 0.3):
        asyncio.run(_run())
    return captured.get("env") or {}


def _bot_turn_env(user, bot_id):
    conv = chat_service.create_conversation(user, bot_id=bot_id)
    chat_service._engine_sessions.clear()
    return _capture_bot_spawn(user, conv["conversation_id"])


# ── 1. the Bot config API writes the reference + model to the registry ──

def test_bot_config_api_writes_profile_and_model_to_registry():
    _profile()
    _create_bot("alice", "reg-bot")

    stored = bots.get_bot("reg-bot")
    assert stored["provider_profile_id"] == BOT_PROFILE_ID
    assert stored["model"] == BOT_MODEL
    assert stored["owner"] == "alice"

    raw = Path(bots.BOTS_FILE).read_text()
    assert BOT_PROFILE_ID in raw and BOT_MODEL in raw
    # The registry holds a REFERENCE, never the secret or a header value.
    assert BOT_KEY not in raw
    assert "bot-tenant" not in raw


# ── 2. build_context resolves the Bot's profile ─────────────────────

def test_build_context_resolves_the_bot_profile():
    _profile()
    _create_bot("alice", "ctx-bot")

    ctx = serve.build_context("ctx-bot")
    assert ctx.llm_error == ""
    assert ctx.llm_profile_id == BOT_PROFILE_ID
    assert ctx.llm_provider == BOT_PROVIDER
    assert ctx.llm_base_url == BOT_BASE
    assert ctx.llm_api_key == BOT_KEY
    assert ctx.llm_headers == BOT_HEADERS

    env = _env_for_ctx(ctx)
    assert env["KYREX_PROVIDER"] == BOT_PROVIDER
    assert env["KYREX_MODEL"] == BOT_MODEL
    assert env["KYREX_API_KEY"] == BOT_KEY
    assert env["ANTHROPIC_BASE_URL"] == BOT_BASE
    assert json.loads(env["KYREX_PROVIDER_HEADERS"]) == BOT_HEADERS


# ── 3. the spawned Bot receives the profile, not Kyrex Chat ─────────

def test_bot_turn_spawn_env_carries_the_profile():
    _profile()
    _create_bot("alice", "spawn-bot")

    env = _bot_turn_env("alice", "spawn-bot")
    assert env, "a Bot-bound turn must spawn an engine process"

    assert env["KYREX_PROVIDER"] == BOT_PROVIDER
    assert env["KYREX_MODEL"] == BOT_MODEL
    assert env["KYREX_API_KEY"] == BOT_KEY
    assert env["ANTHROPIC_BASE_URL"] == BOT_BASE
    assert json.loads(env["KYREX_PROVIDER_HEADERS"]) == BOT_HEADERS

    # Kyrex Chat's provider, model and key must NOT reach the Bot process.
    assert env.get("KYREX_API_KEY") != CHAT_KEY
    assert env.get("KYREX_MODEL") != CHAT_MODEL
    assert env.get("KYREX_PROVIDER") != CHAT_PROVIDER
    # The Bot's endpoint is the PROFILE's, never the host's Chat endpoint.
    assert env["ANTHROPIC_BASE_URL"] == BOT_BASE
    assert env["ANTHROPIC_BASE_URL"] != CHAT_BASE


# ── 4. changing Kyrex Chat's provider does not alter the Bot ────────

def test_chat_provider_change_does_not_alter_the_bot(monkeypatch):
    _profile()
    _create_bot("alice", "iso-bot")
    conv = chat_service.create_conversation("alice", bot_id="iso-bot")

    chat_service._engine_sessions.clear()
    before = _capture_bot_spawn("alice", conv["conversation_id"])
    assert before["KYREX_PROVIDER"] == BOT_PROVIDER

    # Move Kyrex Chat to a completely different provider/model/key/endpoint.
    monkeypatch.setenv("KYREX_PROVIDER", "openrouter")
    monkeypatch.setenv("KYREX_MODEL", "chat-moved-model")
    monkeypatch.setenv("KYREX_API_KEY", "sk-CHAT-moved")
    monkeypatch.setenv("KYREX_BASE_URL", "https://moved.example/v1")

    chat_service._engine_sessions.clear()
    after = _capture_bot_spawn("alice", conv["conversation_id"])

    # The EFFECTIVE configuration the engine resolves from is unchanged.
    for key in ("KYREX_PROVIDER", "KYREX_MODEL", "KYREX_API_KEY",
                "ANTHROPIC_BASE_URL", "KYREX_PROVIDER_HEADERS"):
        assert after.get(key) == before.get(key), (key, before.get(key), after.get(key))
    assert after["ANTHROPIC_BASE_URL"] == BOT_BASE
    assert "sk-CHAT-moved" not in json.dumps(after)
    # NOTE — residual, deliberately INERT. The host's ambient KYREX_BASE_URL /
    # OPENAI_BASE_URL are still inherited into a Bot's env, but they cannot
    # influence the wrong provider branch:
    #   * anthropic Bot — kyrex_engine/kyrex/providers/get_provider returns the
    #     AnthropicProvider BEFORE its `base_url or KYREX_BASE_URL` fallback,
    #     and config.py reads OPENAI_BASE_URL only when provider == "openai";
    #     the endpoint comes from ANTHROPIC_BASE_URL (set from the profile).
    #   * openai-family Bot — EngineSession's non-anthropic branch OVERWRITES
    #     KYREX_BASE_URL and OPENAI_BASE_URL from the profile, and bot_provider
    #     guarantees a non-empty base_url, so the inherited value never wins.
    # Left as-is (documented) rather than changed.

    # The Chat knob itself DID move — so the check above is meaningful.
    assert chat_service._resolve_provider()["model"] == "chat-moved-model"


# ── 5. changing one Bot alters neither Kyrex Chat nor another Bot ───

def test_changing_one_bot_leaves_chat_and_other_bots_untouched():
    _profile(profile_id="prof-a", provider="anthropic", base_url="https://a.example",
             models=["m-a"], api_key="sk-a", headers={"X-A": "a"})
    _profile(profile_id="prof-b", provider="openai", base_url="https://b.example/v1",
             models=["m-b"], api_key="sk-b", headers={"X-B": "b"})
    _profile(profile_id="prof-c", provider="openai", base_url="https://c.example/v1",
             models=["m-c"], api_key="sk-c", headers={"X-C": "c"})

    _create_bot("alice", "bot-a", model="m-a", profile="prof-a")
    _create_bot("alice", "bot-b", model="m-b", profile="prof-b")

    chat_before = chat_service._resolve_provider()
    ctx_b_before = serve.build_context("bot-b")
    env_b_before = _env_for_ctx(ctx_b_before)

    # Retarget bot-a onto a third profile/model.
    r = _client("alice").patch("/api/bots/bot-a", json={
        "provider_profile_id": "prof-c", "model": "m-c"})
    assert r.status_code == 200, r.text

    ctx_a_after = serve.build_context("bot-a")
    assert ctx_a_after.llm_profile_id == "prof-c"
    assert ctx_a_after.llm_api_key == "sk-c"

    # Kyrex Chat is untouched.
    assert chat_service._resolve_provider() == chat_before
    # bot-b is untouched — context AND env.
    ctx_b_after = serve.build_context("bot-b")
    assert ctx_b_after.llm_profile_id == "prof-b"
    assert ctx_b_after.llm_api_key == "sk-b"
    assert _env_for_ctx(ctx_b_after) == env_b_before

    # A Bot-bound conversation can never be retargeted through the Chat
    # provider selector.
    conv = chat_service.create_conversation("alice", bot_id="bot-a")
    with pytest.raises(ValueError):
        chat_service.set_conversation_provider(
            "alice", conv["conversation_id"], "openai", "m-b")
