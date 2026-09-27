"""Engine-session cache identity must include the Bot's effective provider.

A live EngineSession is cached per (user, conversation_id) and reused across
turns. Its reuse identity already covered the workspace, the Bot id, the
effective capability set and the per-conversation session directory — but NOT
the provider configuration. So editing a Bot's provider profile or model would
hand the next turn a session spawned on the PREVIOUS provider, endpoint,
credentials, headers or model.

This module proves the fix:

  1. a Bot session is created with provider/profile A;
  2. the Bot's configuration is changed to provider/profile B;
  3. the next turn does NOT reuse the stale A session;
  4. the new session runs B's provider/model/endpoint/headers;
  5. an UNCHANGED Bot configuration still reuses its session normally;
  6. Kyrex Chat (non-Bot) session reuse is unchanged.

It also proves the identity is stable and carries no secret material.

Run: python3 -m pytest test_bot_provider_session_cache.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bot-provider-cache-tests")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-provider-cache-secret")
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


CHAT_PROVIDER = "openai"
CHAT_MODEL = "gpt-chat-default"
CHAT_KEY = "sk-CHAT-global"
CHAT_BASE = "https://chat.example/v1"


@pytest.fixture(autouse=True)
def _scoped_chat_provider(monkeypatch):
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
    _FakeSession.built.clear()


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"


def teardown_function():
    _reset()


class _FakeSession:
    """Records construction and mimics the reuse identity the factory reads.

    Stands in for EngineSession so a turn can complete without a real process,
    letting the test observe EXACTLY when a new session is (or is not) built.
    """

    built: list = []

    def __init__(self, workspace, provider_cfg, bot_cfg=None):
        bot_cfg = dict(bot_cfg or {})
        self.workspace = Path(workspace)
        self.provider_cfg = dict(provider_cfg or {})
        self.bot_id = (bot_cfg.get("bot_id") or "").strip() or None
        self.allowed_tools = chat_service._effective_caps(bot_cfg)
        self.session_dir = bot_cfg.get("session_dir")
        self._closed = False
        self._proc = MagicMock()
        self._proc.poll.return_value = None  # alive
        _FakeSession.built.append(self)

    def run_turn(self, text, on_token, cancel_check=None):
        on_token("ok")
        return "ok", None

    def interrupt(self):
        pass

    def close(self):
        self._closed = True


def _drain(agen):
    async def _go():
        return [f async for f in agen]
    return asyncio.run(_go())


def _profile(profile_id, provider, model, base_url, api_key, headers):
    return provider_profiles.save_profile("alice", {
        "id": profile_id, "name": profile_id.upper(), "provider": provider,
        "base_url": base_url, "api_key": api_key, "models": [model],
        "headers": dict(headers),
    })


def _provider_cfg(profile_id, provider, model, base_url, api_key, headers):
    """The shape stream_chat builds from a Bot's resolved profile."""
    return {"provider": provider, "profile": profile_id, "model": model,
            "api_key": api_key, "base_url": base_url, "headers": dict(headers),
            "bot_profile": True}


def _bot_cfg(bot_id, model, provider_cfg):
    return {"bot_id": bot_id, "model": model, "system_prompt": "",
            "provider_cfg": provider_cfg}


def _create_bot(bot_id, model, profile):
    from fastapi.testclient import TestClient
    client = TestClient(main.app, cookies={"session": "sess-alice"})
    r = client.post("/api/bots", json={
        "id": bot_id, "name": f"Bot {bot_id}",
        "model": model, "provider_profile_id": profile})
    assert r.status_code == 200, r.text
    assert client.patch(f"/api/bots/{bot_id}", json={"status": "running"}).status_code == 200
    return bots.get_bot(bot_id)


# ── 1-5. cache identity over a Bot's provider config ───────────────

def test_bot_session_respawns_when_provider_config_changes():
    ws = Path("/tmp/cache-ws")
    cfg_a = _provider_cfg("prof-a", "anthropic", "m-a", "https://a.example",
                          "sk-a", {"X-A": "a"})
    cfg_b = _provider_cfg("prof-b", "openai", "m-b", "https://b.example/v1",
                          "sk-b", {"X-B": "b"})

    with patch.object(chat_service, "EngineSession", _FakeSession):
        # 1. a session is created on profile A
        s1 = chat_service._get_engine_session(
            "alice", "cid-1", ws, _bot_cfg("bot-a", "m-a", cfg_a))
        assert s1.provider_cfg["provider"] == "anthropic"
        # 5. an UNCHANGED config reuses the same session
        assert chat_service._get_engine_session(
            "alice", "cid-1", ws, _bot_cfg("bot-a", "m-a", cfg_a)) is s1
        # 2/3. config changed A -> B must NOT reuse A
        s2 = chat_service._get_engine_session(
            "alice", "cid-1", ws, _bot_cfg("bot-a", "m-b", cfg_b))
        assert s2 is not s1, "stale EngineSession was reused after a profile change"
        assert s1._closed is True
        # 4. the new session runs B's provider/model/endpoint/headers/key
        assert s2.provider_cfg["provider"] == "openai"
        assert s2.provider_cfg["model"] == "m-b"
        assert s2.provider_cfg["base_url"] == "https://b.example/v1"
        assert s2.provider_cfg["headers"] == {"X-B": "b"}
        assert s2.provider_cfg["api_key"] == "sk-b"
        # and B is itself reusable while unchanged
        assert chat_service._get_engine_session(
            "alice", "cid-1", ws, _bot_cfg("bot-a", "m-b", cfg_b)) is s2


def test_bot_session_respawns_on_credential_header_or_profile_change():
    ws = Path("/tmp/cache-ws")
    base = _provider_cfg("prof-a", "openai", "m", "https://x.example/v1",
                         "sk-one", {"X-Tenant": "one"})

    with patch.object(chat_service, "EngineSession", _FakeSession):
        s1 = chat_service._get_engine_session(
            "alice", "cid-2", ws, _bot_cfg("bot", "m", base))
        # Rotated API KEY only -> must respawn (credentials changed).
        rotated = dict(base, api_key="sk-two")
        s2 = chat_service._get_engine_session(
            "alice", "cid-2", ws, _bot_cfg("bot", "m", rotated))
        assert s2 is not s1
        # Changed HEADER value only -> must respawn.
        hdr = dict(rotated, headers={"X-Tenant": "two"})
        s3 = chat_service._get_engine_session(
            "alice", "cid-2", ws, _bot_cfg("bot", "m", hdr))
        assert s3 is not s2
        # Changed PROFILE reference only (same provider/model/endpoint) ->
        # must respawn.
        prof = dict(hdr, profile="prof-b")
        s4 = chat_service._get_engine_session(
            "alice", "cid-2", ws, _bot_cfg("bot", "m", prof))
        assert s4 is not s3
        # Identical again -> reuse.
        assert chat_service._get_engine_session(
            "alice", "cid-2", ws, _bot_cfg("bot", "m", dict(prof))) is s4


def test_provider_identity_is_stable_and_never_contains_secrets():
    cfg = _provider_cfg("prof-a", "anthropic", "claude-x", "https://a.example",
                        "sk-super-secret-1234", {"X-Secret": "hdr-secret-value"})
    ident = chat_service._provider_identity(cfg)

    # No secret material, in any form, is echoed into the identity.
    assert "sk-super-secret-1234" not in ident
    assert "hdr-secret-value" not in ident
    # The non-secret routing fields are legible.
    assert "anthropic" in ident and "claude-x" in ident
    assert "prof-a" in ident and "https://a.example" in ident
    # Stable across equal configs; empty config -> empty identity.
    assert ident == chat_service._provider_identity(dict(cfg))
    assert chat_service._provider_identity(None) == ""
    assert chat_service._provider_identity({}) == ""
    # A rotated secret still changes the identity (so it forces a respawn).
    assert ident != chat_service._provider_identity(dict(cfg, api_key="other"))


# ── 6. Kyrex Chat (non-Bot) reuse is unchanged ─────────────────────

def test_kyrex_chat_session_reuse_is_unchanged():
    ws = Path("/tmp/cache-ws")
    chat_a = {"provider": "openai", "profile": "openai", "model": "model-one",
              "api_key": "sk-chat", "base_url": CHAT_BASE}
    chat_b = {"provider": "openrouter", "profile": "openrouter",
              "model": "model-two", "api_key": "sk-chat-2",
              "base_url": "https://other.example/v1"}

    with patch.object(chat_service, "EngineSession", _FakeSession):
        s1 = chat_service._get_engine_session("alice", "cw", ws, None, chat_a)
        assert chat_service._get_engine_session("alice", "cw", ws, None, chat_a) is s1
        # A non-Bot session carries an EMPTY provider identity, so a changed
        # Kyrex Chat provider does not add any new invalidation — reuse here is
        # exactly the pre-existing behaviour, deliberately unchanged.
        assert chat_service._get_engine_session("alice", "cw", ws, None, chat_b) is s1
        assert getattr(s1, "provider_id") == ""


# ── 1-5 again, through the REAL turn path ─────────────────────────

def test_live_turn_respawns_after_bot_profile_change():
    _profile("prof-a", "anthropic", "m-a", "https://a.example", "sk-a", {"X-A": "a"})
    _profile("prof-b", "openai", "m-b", "https://b.example/v1", "sk-b", {"X-B": "b"})
    _create_bot("cache-bot", "m-a", "prof-a")
    conv = chat_service.create_conversation("alice", bot_id="cache-bot")
    cid = conv["conversation_id"]

    with patch.object(chat_service, "EngineSession", _FakeSession):
        # 1. first turn builds a session on profile A
        _drain(chat_service.stream_chat("alice", cid, "one"))
        assert len(_FakeSession.built) == 1
        first = _FakeSession.built[0]
        assert first.provider_cfg["provider"] == "anthropic"
        assert first.provider_cfg["model"] == "m-a"
        assert first.provider_cfg["base_url"] == "https://a.example"

        # 5. an unchanged Bot reuses its session across turns
        _drain(chat_service.stream_chat("alice", cid, "two"))
        assert len(_FakeSession.built) == 1

        # 2. retarget the Bot to profile B through the config API
        from fastapi.testclient import TestClient
        client = TestClient(main.app, cookies={"session": "sess-alice"})
        r = client.patch("/api/bots/cache-bot", json={
            "provider_profile_id": "prof-b", "model": "m-b"})
        assert r.status_code == 200, r.text

        # 3. the next turn must NOT reuse the stale A session
        _drain(chat_service.stream_chat("alice", cid, "three"))
        assert len(_FakeSession.built) == 2, "stale session was reused"
        second = _FakeSession.built[1]
        assert first._closed is True
        # 4. it runs B's provider/model/endpoint/headers/key
        assert second.provider_cfg["provider"] == "openai"
        assert second.provider_cfg["model"] == "m-b"
        assert second.provider_cfg["base_url"] == "https://b.example/v1"
        assert second.provider_cfg["headers"] == {"X-B": "b"}
        assert second.provider_cfg["api_key"] == "sk-b"


def test_kyrex_chat_workspace_turn_reuses_its_session(monkeypatch):
    ws = tempfile.mkdtemp(prefix="kyrex-cache-ws-")
    monkeypatch.setenv("KYREX_CHAT_WORKSPACES", json.dumps({"testws": ws}))
    monkeypatch.delenv("KYREX_CHAT_WORKSPACE", raising=False)
    conv = chat_service.create_conversation("alice")
    cid = conv["conversation_id"]

    with patch.object(chat_service, "EngineSession", _FakeSession):
        _drain(chat_service.stream_chat("alice", cid, "hi", workspace_id="testws"))
        assert len(_FakeSession.built) == 1
        _drain(chat_service.stream_chat("alice", cid, "again", workspace_id="testws"))
        # Unchanged Kyrex Chat session is reused, exactly as before.
        assert len(_FakeSession.built) == 1
        assert getattr(_FakeSession.built[0], "provider_id") == ""
