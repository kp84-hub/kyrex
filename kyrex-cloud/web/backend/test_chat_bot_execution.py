"""Bots-in-Kyrex-Chat, slice 2: Bot-aware execution (identity/context).

Focused regression coverage proving that a Bot-bound conversation's EXECUTION
uses the Bot's identity and context — not merely that the binding exists:

  1. A Bot-bound conversation resolves the correct Bot.
  2. The Bot's configured model reaches execution (engine spawn env).
  3. The Bot's "system_prompt" reaches execution (engine spawn env).
  4. The Bot's Rift/workspace is the execution context (spawn cwd).
  5. A nonexistent Bot fails at turn time rather than falling back.
  6. An unauthorized Bot fails at turn time rather than falling back.
  7. A Bot with an unavailable/invalid Rift fails rather than falling back.
  8. A "bot_id = null" conversation follows the existing path unchanged
     (provider path; no engine session is ever requested).
  9. Two different Bots cannot share/fall through to each other's execution
     context (stream path AND session-cache identity checks).

Boundary: every execution assertion captures the EXACT values handed to the
engine boundary — either a recording fake for chat_service._get_engine_session
or a stubbed subprocess.Popen capturing the real EngineSession spawn env/cwd
(KYREX_MODEL / KYREX_PROVIDER / KYREX_CHAT_SYSTEM_PROMPT / cwd). No test
asserts merely that a request returned 200.

Run: python3 -m pytest test_chat_bot_execution.py
"""

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-bot-exec-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BACKEND)
sys.path.insert(0, os.path.dirname(_BACKEND))

import main  # noqa: E402  (after env setup; seeds the shared app/session map)
import chat_service  # noqa: E402
import bots  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    bots.save_bots({})
    chat_service._engine_sessions.clear()
    _RecordingEngine.calls.clear()
    _StubSession.instances.clear()


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()


def _rift_dir() -> str:
    return tempfile.mkdtemp(prefix="kyrex-bot-rift-")


def _bot(bot_id="qa", owner="", model="anthropic:claude-exec-test",
         system_prompt="", rift=None):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", model, rift or _rift_dir(),
        status="stopped", owner=owner, system_prompt=system_prompt,
    )


def _seed_conv(user, conv_id, bot_id=None, messages=None):
    """Persist a conversation record directly (bypasses create validation)."""
    conv = {
        "conversation_id": conv_id,
        "title": "seeded",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
        "messages": messages or [],
    }
    if bot_id:
        conv["bot_id"] = bot_id
    chat_service._write(user, conv)
    return conv


class _RecordingEngine:
    """EngineSession stand-in that records the EXACT execution values handed
    to the engine boundary for every turn."""

    calls: list = []

    def __init__(self, user, conversation_id, workspace, bot_cfg=None):
        bot_cfg = dict(bot_cfg or {})
        _RecordingEngine.calls.append({
            "user": user,
            "conversation_id": conversation_id,
            "workspace": Path(workspace),
            "bot_id": (bot_cfg.get("bot_id") or "").strip() or None,
            "model": (bot_cfg.get("model") or "").strip() or None,
            "system_prompt": (bot_cfg.get("system_prompt") or "").strip() or None,
        })
        self.workspace = Path(workspace)
        self.bot_id = _RecordingEngine.calls[-1]["bot_id"]
        self.model = _RecordingEngine.calls[-1]["model"]
        self.system_prompt = _RecordingEngine.calls[-1]["system_prompt"]

    def run_turn(self, text, on_token, cancel_check=None):
        on_token("bot answer")
        return "bot answer", None

    def interrupt(self):
        pass

    def close(self):
        pass


def _patch_recording_engine():
    return patch("chat_service._get_engine_session", new=_RecordingEngine)


def _must_not_request_engine(*args, **kwargs):
    raise AssertionError(
        f"engine session must NOT be requested (got {args}, {kwargs}) "
        "— a Bot failure must not fall back to a default session")


async def _frames(agen):
    out = []
    async for f in agen:
        out.append(f)
    return out


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


class _StubSession:
    """No-spawn EngineSession substitute for session-cache identity tests."""

    instances: list = []

    def __init__(self, ws, cfg, bot_cfg=None):
        bot_cfg = bot_cfg or {}
        self.bot_id = (bot_cfg.get("bot_id") or "").strip() or None
        self.workspace = Path(ws)
        self._closed = False
        self._proc = MagicMock()
        self._proc.poll.return_value = None  # process alive (real Popen semantics)
        _StubSession.instances.append(self)

    def close(self):
        self._closed = True


def _spawn_capture(workspace, provider_cfg, bot_cfg=None, timeout=0.3):
    """Construct a REAL EngineSession with Popen stubbed, capturing the exact
    spawn env + cwd without spawning a process. Returns the captured dict."""
    captured = {}

    def fake_popen(*args, **kwargs):
        captured["cmd"] = args[0] if args else kwargs.get("args")
        captured["env"] = dict(kwargs.get("env") or {})
        captured["cwd"] = kwargs.get("cwd")
        proc = MagicMock()
        proc.poll.return_value = 0
        proc.stdout = io.StringIO()   # reader thread ends immediately
        proc.stderr = io.StringIO()
        proc.stdin = io.StringIO()
        return proc

    with patch("subprocess.Popen", side_effect=fake_popen), \
         patch.object(chat_service, "ENGINE_HANDSHAKE_TIMEOUT", timeout):
        try:
            chat_service.EngineSession(Path(workspace), provider_cfg, bot_cfg)
            captured["constructed"] = True
        except chat_service.EngineSessionError:
            captured["constructed"] = False  # no real process -> no handshake
    return captured


# ── 1-4. identity/context reaches execution ────────────────────────

def test_bot_turn_resolves_correct_bot_and_binding():
    rift = _rift_dir()
    _bot("qa", owner="alice", rift=rift, system_prompt="Be terse.")
    conv = chat_service.create_conversation("alice", bot_id="qa")
    conv_id = conv["conversation_id"]

    with _patch_recording_engine():
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv_id, "hi")))

    term = _terminal(frames)
    assert term is not None and term["status"] == "complete", frames
    assert term["content"] == "bot answer"
    assert len(_RecordingEngine.calls) == 1
    call = _RecordingEngine.calls[0]
    assert call["bot_id"] == "qa"
    assert call["workspace"] == Path(rift)
    # Turn persisted under the same binding; bot identity preserved.
    stored = chat_service.get_conversation("alice", conv_id)
    assert stored["bot_id"] == "qa"
    assert [m["role"] for m in stored["messages"]] == ["user", "assistant"]


def test_bot_model_reaches_execution():
    bot = _bot("qa", owner="alice", model="anthropic:claude-exec-model")
    conv = chat_service.create_conversation("alice", bot_id="qa")

    with _patch_recording_engine():
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert _terminal(frames)["status"] == "complete"
    assert _RecordingEngine.calls[0]["model"] == "anthropic:claude-exec-model"

    # The REAL EngineSession translates that into the engine spawn env: the
    # "provider:model" registry string is split — provider -> KYREX_PROVIDER,
    # model -> KYREX_MODEL. This is what the engine process actually receives.
    cfg = chat_service._resolve_provider()
    captured = _spawn_capture(
        bot["rift"], cfg,
        bot_cfg={"bot_id": "qa", "model": bot["model"], "system_prompt": ""})
    assert captured["env"]["KYREX_MODEL"] == "claude-exec-model"
    assert captured["env"]["KYREX_PROVIDER"] == "anthropic"
    assert captured["env"]["KYREX_API_KEY"] == "sk-test"


def test_bot_system_prompt_reaches_execution():
    prompt = "You are the deploy Bot. Only answer with deployment plans."
    bot = _bot("qa", owner="alice", system_prompt=prompt)
    conv = chat_service.create_conversation("alice", bot_id="qa")

    with _patch_recording_engine():
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert _terminal(frames)["status"] == "complete"
    assert _RecordingEngine.calls[0]["system_prompt"] == prompt

    # The Bot prompt is carried to the engine process via
    # KYREX_CHAT_SYSTEM_PROMPT (core_bridge.py injects it into the session).
    cfg = chat_service._resolve_provider()
    captured = _spawn_capture(
        bot["rift"], cfg,
        bot_cfg={"bot_id": "qa", "model": bot["model"],
                 "system_prompt": prompt})
    assert captured["env"]["KYREX_CHAT_SYSTEM_PROMPT"] == prompt

    # A NON-Bot session must NOT carry the key at all.
    plain = _spawn_capture(_rift_dir(), cfg, None)
    assert "KYREX_CHAT_SYSTEM_PROMPT" not in plain["env"]


def test_bot_rift_is_execution_context():
    rift = _rift_dir()
    bot = _bot("qa", owner="alice", rift=rift)
    conv = chat_service.create_conversation("alice", bot_id="qa")

    with _patch_recording_engine():
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "hi")))
    assert _terminal(frames)["status"] == "complete"
    assert _RecordingEngine.calls[0]["workspace"] == Path(rift)

    # The REAL EngineSession spawns core_bridge.py with the Bot's Rift as cwd
    # — that is the engine's execution workspace (self-configuring bootstrap).
    captured = _spawn_capture(
        rift, chat_service._resolve_provider(),
        bot_cfg={"bot_id": "qa", "model": bot["model"], "system_prompt": ""})
    assert captured["cwd"] == rift
    assert Path(captured["cwd"]).is_dir()


# ── 5-7. fail closed at turn time: no fallback session ─────────────

def test_nonexistent_bot_fails_at_turn_no_fallback():
    conv = _seed_conv("alice", "cid-ghost", bot_id="ghost")

    with patch("chat_service._get_engine_session", new=_must_not_request_engine):
        with pytest.raises(chat_service.ChatUnavailable) as exc:
            asyncio.run(_frames(
                chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert "unknown bot" in str(exc.value).lower()
    assert _RecordingEngine.calls == []
    stored = chat_service.get_conversation("alice", conv["conversation_id"])
    assert stored["bot_id"] == "ghost"
    assert "workspace_id" not in stored  # nothing silently rebound


def test_unauthorized_bot_fails_at_turn_no_fallback():
    _bot("bobs", owner="bob")
    conv = _seed_conv("alice", "cid-sneaky", bot_id="bobs")

    with patch("chat_service._get_engine_session", new=_must_not_request_engine):
        with pytest.raises(chat_service.ChatUnavailable) as exc:
            asyncio.run(_frames(
                chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert "not available" in str(exc.value).lower()
    assert _RecordingEngine.calls == []
    stored = chat_service.get_conversation("alice", conv["conversation_id"])
    assert stored["bot_id"] == "bobs"
    assert "workspace_id" not in stored


def test_invalid_rift_fails_at_turn_no_fallback():
    bot = _bot("qa", owner="alice")
    rift = bot["rift"]
    conv = chat_service.create_conversation("alice", bot_id="qa")
    # Rift disappears after binding — the turn must fail, not fall back.
    shutil.rmtree(rift, ignore_errors=True)

    with patch("chat_service._get_engine_session", new=_must_not_request_engine):
        with pytest.raises(chat_service.ChatUnavailable) as exc:
            asyncio.run(_frames(
                chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert "rift" in str(exc.value).lower()
    assert _RecordingEngine.calls == []
    stored = chat_service.get_conversation("alice", conv["conversation_id"])
    assert stored["bot_id"] == "qa"
    assert "workspace_id" not in stored

    # Registry-time rejection too: binding with an unresolvable Rift is a 400.
    import main as main_module
    from fastapi.testclient import TestClient
    client = TestClient(main_module.app, cookies={"session": "sess-alice"})
    dead = _rift_dir()
    shutil.rmtree(dead, ignore_errors=True)
    bots.add_bot("deadrift", "Dead", "anthropic:claude-test", dead,
                 status="stopped", owner="alice")
    r = client.post("/api/conversations", json={"bot_id": "deadrift"})
    assert r.status_code == 400
    assert "rift" in r.json()["detail"].lower()


# ── 8. non-Bot path unchanged ──────────────────────────────────────

def test_non_bot_conversation_executes_existing_path_unchanged():
    class FakeProvider:
        async def chat(self, model, messages, tools=None, stream_callback=None,
                       interrupt_event=None, **kw):
            assert model == "gpt-test"  # server default; nothing Bot-injected
            if stream_callback:
                stream_callback("plain")
                stream_callback(" answer")
            return {"role": "assistant", "content": "plain answer"}

    with patch("chat_service.get_provider", return_value=FakeProvider()), \
         patch("chat_service._get_engine_session", new=_must_not_request_engine):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "hi")))

    term = _terminal(frames)
    assert term is not None and term["status"] == "complete", frames
    assert term["content"] == "plain answer"
    assert _RecordingEngine.calls == []  # provider path, NOT the engine path

    convs = chat_service.list_conversations("alice")
    assert len(convs) == 1
    assert convs[0].get("bot_id") is None
    stored = chat_service.get_conversation("alice", convs[0]["conversation_id"])
    assert "bot_id" not in stored  # nothing Bot-related leaked into storage
    assert stored["messages"][1]["content"] == "plain answer"


# ── 9. no cross-Bot context sharing ────────────────────────────────

def test_two_bots_never_share_execution_context_stream_path():
    rift1, rift2 = _rift_dir(), _rift_dir()
    _bot("qa1", owner="alice", model="anthropic:claude-one",
         system_prompt="PROMPT ONE", rift=rift1)
    _bot("qa2", owner="alice", model="anthropic:claude-two",
         system_prompt="PROMPT TWO", rift=rift2)
    conv1 = chat_service.create_conversation("alice", bot_id="qa1")
    conv2 = chat_service.create_conversation("alice", bot_id="qa2")

    with _patch_recording_engine():
        f1 = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv1["conversation_id"], "a")))
        f2 = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv2["conversation_id"], "b")))

    assert _terminal(f1)["status"] == "complete"
    assert _terminal(f2)["status"] == "complete"
    assert len(_RecordingEngine.calls) == 2
    c1, c2 = _RecordingEngine.calls
    assert (c1["bot_id"], c1["model"], c1["system_prompt"]) == \
           ("qa1", "anthropic:claude-one", "PROMPT ONE")
    assert (c2["bot_id"], c2["model"], c2["system_prompt"]) == \
           ("qa2", "anthropic:claude-two", "PROMPT TWO")
    assert c1["workspace"] == Path(rift1)
    assert c2["workspace"] == Path(rift2)
    # No bleed in either direction.
    assert c1["system_prompt"] != c2["system_prompt"]
    assert c1["workspace"] != c2["workspace"]


def test_session_cache_refuses_cross_bot_reuse():
    """The engine-session cache must respawn when the Bot identity changes —
    a cached process belonging to one Bot can never serve another Bot."""
    ws1, ws2 = Path(_rift_dir()), Path(_rift_dir())
    with patch.object(chat_service, "EngineSession", _StubSession):
        s1 = chat_service._get_engine_session(
            "alice", "cid", ws1, {"bot_id": "qa1", "model": "m1",
                                  "system_prompt": "P1"})
        s2 = chat_service._get_engine_session(
            "alice", "cid", ws2, {"bot_id": "qa2", "model": "m2",
                                  "system_prompt": "P2"})
        s3 = chat_service._get_engine_session(
            "alice", "cid", ws1, {"bot_id": "qa1", "model": "m1",
                                  "system_prompt": "P1"})
        # Reuse IS preserved for an identical identity (same bot, same ws).
        s4 = chat_service._get_engine_session(
            "alice", "cid", ws1, {"bot_id": "qa1", "model": "m1",
                                  "system_prompt": "P1"})

    assert len(_StubSession.instances) == 3  # never reused across identities
    assert s1._closed and s2._closed          # old sessions closed on swap
    assert not s3._closed                     # newest session is the live one
    assert [s.bot_id for s in _StubSession.instances] == ["qa1", "qa2", "qa1"]
    # Cache holds exactly one session — the identity that just ran.
    assert len(chat_service._engine_sessions) == 1
    assert chat_service._engine_sessions[("alice", "cid")].bot_id == "qa1"

    assert s4 is s3
    assert len(_StubSession.instances) == 3