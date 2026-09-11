"""Kyrex Chat coordinator awareness — Chat system context, all non-Bot modes.

Proves that Kyrex Chat injects a dynamic, per-turn system context:

  1. ordinary (pure) Chat — asserted against the ACTUAL provider message list
     (`messages[0]`), not only the context helper;
  2. workspace-attached read-only Chat — asserted against the ACTUAL engine
     frame (`surfaceContext`) run_turn sends AND the engine-side refresh
     (core_bridge._apply_surface_context), including the REUSED-session case:
     a Bot status change is reflected on the next turn of the SAME engine
     process;

and that the context:
  * identifies Kyrex Chat and states its capability limits,
  * lists the Bots visible to the authenticated user (UI-safe metadata only),
  * never leaks another user's Bot or any sensitive field,
  * renders the workspace read-only wording, and
  * leaves Bot prompts, Bot routing, provider selection, and tool permissions
    unchanged (a Bot-bound session never receives the surface context).

Run: python3 -m pytest test_chat_coordinator.py
"""

import asyncio
import os
import queue as _q
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-coordinator-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BACKEND)
sys.path.insert(0, os.path.dirname(_BACKEND))

# repo root = .../kyrex  (backend = kyrex-cloud/web/backend)
_REPO = Path(__file__).resolve().parents[3]
_ENGINE_DIR = _REPO / "kyrex_engine"

import chat_service  # noqa: E402
import bots  # noqa: E402  — the authoritative registry under test


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


def teardown_function():
    _reset()


def _rift_dir() -> str:
    return tempfile.mkdtemp(prefix="kyrex-bot-rift-")


def _bot(bot_id="qa", owner="", status="stopped", rift=None,
         model="anthropic:claude-test", policy=None, system_prompt=""):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", model, rift or _rift_dir(),
        policy=policy, status=status, owner=owner, system_prompt=system_prompt,
    )


async def _frames(agen):
    out = []
    async for f in agen:
        out.append(f)
    return out


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


class _CapturingProvider:
    """Provider stub that records the exact message list it receives."""

    def __init__(self):
        self.messages = None

    async def chat(self, model, messages, tools=None, stream_callback=None,
                   interrupt_event=None, **kw):
        self.messages = messages
        if stream_callback:
            stream_callback("ok")
        return {"role": "assistant", "content": "ok"}


def _ordinary_system_prompt(user="alice"):
    """Run one ordinary turn and return the system message content sent."""
    prov = _CapturingProvider()
    with patch("chat_service.get_provider", return_value=prov):
        frames = asyncio.run(_frames(chat_service.stream_chat(user, "", "hi")))
    assert _terminal(frames)["status"] == "complete", frames
    assert prov.messages and prov.messages[0]["role"] == "system"
    return prov.messages[0]["content"]


class _StubSession:
    """No-spawn EngineSession substitute that IS reused across turns (the real
    _get_engine_session cache path) and records the surface context stream_chat
    hands it for each turn."""

    instances = []

    def __init__(self, ws, cfg, bot_cfg=None):
        self.workspace = Path(ws)
        self.bot_id = (bot_cfg or {}).get("bot_id")
        self.allowed_tools = chat_service._effective_caps(bot_cfg or {})
        self._closed = False
        self._proc = MagicMock()
        self._proc.poll.return_value = None          # process alive
        self.surface_context = None
        self.turns = []
        _StubSession.instances.append(self)

    def run_turn(self, text, on_token, cancel_check=None):
        self.turns.append(self.surface_context)
        on_token("ok")
        return "ok", None

    def interrupt(self):
        pass

    def close(self):
        self._closed = True


# ── 1. ordinary chat: identity + limits ────────────────────────────

def test_ordinary_turn_system_message_identifies_kyrex_chat_and_limits():
    text = _ordinary_system_prompt()
    assert "You are Kyrex Chat" in text
    low = text.lower()
    assert "ordinary chat" in low
    assert "cannot" in low
    for limit in ("edit files", "execute tasks", "approve"):
        assert limit in low, limit


def test_build_messages_defaults_to_static_prompt_unchanged():
    msgs = chat_service.build_messages([], "hi")
    assert msgs[0] == {"role": "system",
                       "content": chat_service.CHAT_SYSTEM_PROMPT}
    # The pre-existing history handling is untouched.
    msgs2 = chat_service.build_messages(
        [{"role": "user", "content": "a"},
         {"role": "assistant", "content": "b"}], "c")
    assert [m["role"] for m in msgs2] == ["system", "user", "assistant", "user"]


# ── 2. visible Bot metadata appears ────────────────────────────────

def test_visible_bot_metadata_appears_in_ordinary_context():
    _bot("qa", owner="alice", status="running", model="anthropic:claude-qa",
         policy={"fs:read": 0})
    text = _ordinary_system_prompt()
    assert "qa" in text
    assert "Bot qa" in text                        # name
    assert "status: running" in text               # status
    assert "anthropic:claude-qa" in text           # model display metadata
    assert "available: yes" in text                # availability
    assert "writable developer bot: no" in text    # fs:read only -> read-only


def test_operator_created_bot_is_visible_to_every_user():
    _bot("shared", owner="", policy={"fs:read": 0})
    assert "shared" in _ordinary_system_prompt("alice")
    assert "shared" in _ordinary_system_prompt("bob")


def test_writable_developer_bot_is_flagged():
    _bot("dev", owner="alice", policy={"fs:read": 0, "fs:write": 1})
    text = chat_service.build_system_context("alice")
    assert "dev" in text
    assert "writable developer bot: yes" in text


def test_no_bots_states_that_none_are_available():
    text = chat_service.build_system_context("alice")
    assert "No Bots are currently available" in text


# ── 3. no leaks ────────────────────────────────────────────────────

def test_inaccessible_bots_and_sensitive_fields_never_appear():
    alice_rift = _rift_dir()
    bots.add_bot(
        "qa", "Alice QA", "anthropic:claude-secret-model", alice_rift,
        policy={"fs:read": 0}, status="running", owner="alice",
        system_prompt="SUPER SECRET PROMPT")
    _bot("bobs", owner="bob", system_prompt="BOB SECRET")

    text = chat_service.build_system_context("alice")
    low = text.lower()

    # Alice's own Bot is present (with safe metadata only).
    assert "qa" in text and "Alice QA" in text

    # Another user's Bot is invisible.
    assert "bobs" not in low
    assert "bob secret" not in low

    # Sensitive fields never appear.
    assert alice_rift not in text
    assert "SUPER SECRET PROMPT" not in text
    for needle in ("rift", "policy", "system_prompt", "api_key", "token",
                   "credential"):
        assert needle not in low, needle

    # ...and the safe model string DOES appear (it is already public).
    assert "anthropic:claude-secret-model" in text


def test_registry_fault_does_not_break_an_ordinary_turn():
    # A corrupt registry must not raise into a chat turn; the roster is
    # simply omitted (the /api/bots surface still reports the fault).
    with open(bots.BOTS_FILE, "w") as f:
        f.write("{ not valid json ")
    text = _ordinary_system_prompt()
    assert "You are Kyrex Chat" in text
    assert "No Bots are currently available" in text


# ── 4. workspace mode: wording + roster ────────────────────────────

def test_workspace_mode_states_read_only_boundary():
    text = chat_service.build_system_context(
        "alice", mode=chat_service.MODE_WORKSPACE)
    low = text.lower()
    assert "You are Kyrex Chat" in text
    assert "workspace-attached read-only chat" in low
    assert "read-only" in low
    assert "cannot edit files" in low


def test_workspace_mode_includes_visible_bot_roster():
    _bot("qa", owner="alice", status="running", policy={"fs:read": 0})
    text = chat_service.build_system_context(
        "alice", mode=chat_service.MODE_WORKSPACE)
    assert "Available Bots for this user" in text
    assert "qa" in text and "Bot qa" in text


def test_bot_mode_omits_roster_and_states_bot_boundary():
    _bot("qa", owner="alice")
    text = chat_service.build_system_context(
        "alice", mode=chat_service.MODE_BOT)
    assert "Bot-bound chat" in text
    assert "Available Bots" not in text


# ── 5. per-turn refresh across a REUSED engine session ─────────────

def test_workspace_session_refreshes_context_on_every_turn(monkeypatch,
                                                           tmp_path):
    """The engine process is reused across turns; a safe Bot status change
    made after the session exists must appear on the NEXT turn (not only at
    spawn)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("KYREX_CHAT_WORKSPACE", str(ws))

    _StubSession.instances.clear()
    _bot("qa", owner="alice", status="stopped", policy={"fs:read": 0})
    conv = chat_service.create_conversation("alice", title="ws")
    cid = conv["conversation_id"]

    with patch.object(chat_service, "EngineSession", _StubSession):
        # Turn 1 — the Bot is "stopped".
        f1 = asyncio.run(_frames(chat_service.stream_chat(
            "alice", cid, "hi", workspace_id="default")))
        assert _terminal(f1)["status"] == "complete", f1

        # The Bot's SAFE status changes after the session already exists.
        bots.set_status("qa", "running")

        # Turn 2 — same conversation => the SAME engine process is reused.
        f2 = asyncio.run(_frames(chat_service.stream_chat(
            "alice", cid, "again")))
        assert _terminal(f2)["status"] == "complete", f2

    # One process, reused across both turns (this is the lifecycle under test).
    assert len(_StubSession.instances) == 1
    sess = _StubSession.instances[0]
    assert len(sess.turns) == 2

    ctx1, ctx2 = sess.turns
    # The context actually handed to the engine, per turn:
    assert "You are Kyrex Chat" in ctx1
    assert "read-only" in ctx1.lower()
    assert "status: stopped" in ctx1
    assert "status: running" not in ctx1
    # ...the second turn reflects the live, updated safe roster.
    assert "status: running" in ctx2
    assert "status: stopped" not in ctx2
    assert ctx1 != ctx2


def test_engine_run_turn_attaches_surface_context_to_frame():
    """The ACTUAL frame run_turn sends to the engine process carries the
    per-turn context — and omits it entirely when absent (Bot / pure)."""
    sent = []
    sess = chat_service.EngineSession.__new__(chat_service.EngineSession)
    sess._closed = False
    sess._turn_lock = threading.Lock()
    sess._stderr_lock = threading.Lock()
    sess.stderr_tail = []
    sess._proc = MagicMock()
    sess._proc.poll.return_value = None
    sess._send = lambda payload: sent.append(payload)
    frames = _q.Queue()
    sess._frames = frames

    def _drain():
        frames.put({"type": "chat_done", "content": "ok"})
        frames.put({"type": "phase", "value": "IDLE"})

    sess.surface_context = "CTX-NOW"
    _drain()
    final, err = sess.run_turn("hi", lambda t: None)
    assert final == "ok" and err is None
    assert sent[0]["type"] == "chat"
    assert sent[0]["surfaceContext"] == "CTX-NOW"

    sent.clear()
    sess.surface_context = None
    _drain()
    sess.run_turn("hi", lambda t: None)
    assert "surfaceContext" not in sent[0]   # unchanged frame for Bot / pure


def test_engine_refreshes_surface_context_in_place(monkeypatch):
    """The engine-side prompt path: exactly one surface-context system message,
    refreshed each turn; a no-op when absent; ignored for Bot sessions."""
    sys.path.insert(0, str(_ENGINE_DIR))
    import core_bridge  # the real engine bridge chat_service spawns

    class _Session:
        def __init__(self):
            self.history = []

        def append(self, m):
            self.history.append(m)

    class _Engine:
        def __init__(self):
            self.session = _Session()

    def _surface(eng):
        return [m for m in eng.session.history
                if str(m.get("content") or "").startswith(
                    core_bridge._SURFACE_MARKER)]

    monkeypatch.delenv("KYREX_CHAT_SYSTEM_PROMPT", raising=False)
    eng = _Engine()
    core_bridge._apply_surface_context(eng, "CTX ONE")
    core_bridge._apply_surface_context(eng, "CTX TWO")
    msgs = _surface(eng)
    assert len(msgs) == 1                                   # refreshed
    assert msgs[0]["content"] == "KYREX CHAT SURFACE CONTEXT: CTX TWO"

    # Empty / absent context is a no-op (never wipes, never appends).
    core_bridge._apply_surface_context(eng, None)
    core_bridge._apply_surface_context(eng, "   ")
    assert len(_surface(eng)) == 1

    # A Bot-bound session (Bot prompt env set) ignores it entirely.
    monkeypatch.setenv("KYREX_CHAT_SYSTEM_PROMPT", "BOT IDENTITY")
    eng2 = _Engine()
    core_bridge._apply_surface_context(eng2, "SHOULD NOT APPLY")
    assert eng2.session.history == []


# ── 6. Bot-bound behavior unchanged ────────────────────────────────

def test_bot_bound_system_prompt_and_routing_unchanged():
    _bot("qa", owner="alice", system_prompt="BOT IDENTITY",
         policy={"fs:read": 0})
    conv = chat_service.create_conversation("alice", bot_id="qa")

    calls = []
    seen_surface = []

    class _Engine:
        def __init__(self, user, cid, workspace, bot_cfg=None):
            calls.append(dict(bot_cfg or {}))
            self.workspace = Path(workspace)
            self.bot_id = (bot_cfg or {}).get("bot_id")
            self.system_prompt = (bot_cfg or {}).get("system_prompt")
            self.allowed_tools = chat_service._effective_caps(bot_cfg or {})
            self.surface_context = None

        def run_turn(self, text, on_token, cancel_check=None):
            seen_surface.append(self.surface_context)
            on_token("bot reply")
            return "bot reply", None

        def interrupt(self):
            pass

        def close(self):
            pass

    def _boom(*args, **kwargs):
        raise AssertionError(
            "build_system_context must NOT be used for a Bot-bound turn")

    with patch("chat_service._get_engine_session", new=_Engine), \
         patch("chat_service.build_system_context", side_effect=_boom):
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert _terminal(frames)["status"] == "complete", frames
    assert len(calls) == 1
    # The Bot's own identity reaches the engine untouched.
    assert calls[0]["bot_id"] == "qa"
    assert calls[0]["system_prompt"] == "BOT IDENTITY"
    # A Bot-bound session is never handed the Kyrex Chat surface context.
    assert seen_surface == [None]
