"""Bots-in-Kyrex-Chat, slice 3: policy-aware Bot execution.

Focused regression coverage proving the PERMISSION DECISION — not merely
that a request returned 200:

  1. Policy explicitly allows an operation  → the mapped engine tool is
     allowed, subject to the host tier (fs:read is host T0 → auto-allow).
  2. Policy explicitly denies an operation  → the mapped tool is denied.
  3. Prefix policy works per existing semantics (``fs:*``).
  4. Exact policy overrides prefix per existing semantics.
  5. No matching policy rule denies (default deny).
  6. Policy cannot reduce a host-required safety tier: write/command tools
     are never added, ``KYREX_READ_ONLY_REPO=1`` is always set, and a raised
     tier (approval-required) removes tools instead of auto-approving.
  7. A Bot with an invalid policy fails closed (no engine session, no
     fallback to unrestricted Chat behavior).
  8. A nonexistent Bot cannot fall back to any global/default policy.
  9. An unauthorized Bot cannot access another Bot's policy.
 10. Two Bots with different policies receive different effective
     capabilities (stream path AND real spawn env).
 11. Non-Bot (``bot_id = null``) Chat is unchanged (full host base allowlist,
     provider path, no engine session).
 12. Approval-required operations remain unavailable; they are never
     auto-approved or added to the engine allowlist.

Boundary: the permission decision is asserted on the bot_capabilities
decision table (matched_rule / effective_tier / reason), the stream path is
captured by a recording engine fake (exact ``allowed_tools`` handed to the
engine boundary), and the REAL EngineSession spawn env/cwd is captured via a
stubbed subprocess.Popen (``KYREX_ALLOWED_TOOLS`` / ``KYREX_READ_ONLY_REPO``
/ cwd). Plus one end-to-end proof that a Bot-bound Chat request carries the
expected capabilities into the engine.

Run: python3 -m pytest test_chat_bot_policy.py
"""

import asyncio
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-bot-policy-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BACKEND)
sys.path.insert(0, os.path.dirname(_BACKEND))

import chat_service  # noqa: E402
import bots  # noqa: E402

CAPS = chat_service.bot_capabilities
HOST_BASE = set(CAPS.CHAT_HOST_BASE_TOOLS)
HOST_GRANTED = set(CAPS.HOST_GRANTED_TOOLS)
WRITE_TOOLS = {"edit_file", "write_file_with_gate", "run_command"}


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


def teardown_function():
    _reset()


def _rift_dir() -> str:
    return tempfile.mkdtemp(prefix="kyrex-bot-rift-")


def _bot(bot_id="qa", owner="", model="anthropic:claude-exec-test",
         system_prompt="", rift=None, policy=None):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", model, rift or _rift_dir(),
        policy=policy, status="stopped", owner=owner,
        system_prompt=system_prompt,
    )


def _seed_conv(user, conv_id, bot_id=None):
    conv = {
        "conversation_id": conv_id,
        "title": "seeded",
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
        "messages": [],
    }
    if bot_id:
        conv["bot_id"] = bot_id
    chat_service._write(user, conv)
    return conv


class _RecordingEngine:
    """EngineSession stand-in that records the EXACT values (including the
    policy-derived ``allowed_tools``) handed to the engine boundary."""

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
            "allowed_tools": list(bot_cfg.get("allowed_tools") or []),
        })
        self.workspace = Path(workspace)
        self.bot_id = _RecordingEngine.calls[-1]["bot_id"]
        self.model = _RecordingEngine.calls[-1]["model"]
        self.system_prompt = _RecordingEngine.calls[-1]["system_prompt"]
        self.allowed_tools = chat_service._effective_caps(bot_cfg)

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
        "— a Bot policy failure must never fall back to a default session")


class _StubSession:
    """No-spawn EngineSession substitute for session-cache identity tests."""

    instances: list = []

    def __init__(self, ws, cfg, bot_cfg=None):
        bot_cfg = bot_cfg or {}
        self.bot_id = (bot_cfg.get("bot_id") or "").strip() or None
        self.workspace = Path(ws)
        self.allowed_tools = chat_service._effective_caps(bot_cfg)
        self._closed = False
        self._proc = MagicMock()
        self._proc.poll.return_value = None  # process alive
        _StubSession.instances.append(self)

    def close(self):
        self._closed = True


async def _frames(agen):
    out = []
    async for f in agen:
        out.append(f)
    return out


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


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


def _decide(policy, tool):
    """The permission decision for one tool, straight from the boundary."""
    return CAPS.derive_bot_capabilities(policy)["decisions"][tool]


# ── 1-2. explicit allow / explicit deny ───────────────────────────

def test_policy_explicit_allow_allows_operation_subject_to_host_tier():
    # fs:read only — this stays on the read-only engine path (a writable Bot
    # would route to the executor path under Step 2).
    bot = _bot("qa", owner="alice", policy={"fs:read": 0})
    conv = chat_service.create_conversation("alice", bot_id="qa")

    with _patch_recording_engine():
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert _terminal(frames)["status"] == "complete", frames
    caps = _RecordingEngine.calls[0]["allowed_tools"]
    # fs:read is host T0 -> every fs:read-mapped read tool is allowed...
    assert "read_local_file" in caps
    assert "list_local_files" in caps
    assert "search" in caps
    # ...subject to the host tier: write/command tools are NEVER granted.
    assert not (set(caps) & WRITE_TOOLS)
    # The permission decision itself (not just HTTP success):
    dec = _decide(bot["policy"], "read_local_file")
    assert dec["allowed"] is True
    assert dec["effective_tier"] == 0
    assert dec["matched_rule"] == "fs:read"


def test_policy_explicit_deny_denies_operation():
    bot = _bot("qa", owner="alice", policy={"fs:read": "deny"})
    conv = chat_service.create_conversation("alice", bot_id="qa")

    with _patch_recording_engine():
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert _terminal(frames)["status"] == "complete", frames
    caps = set(_RecordingEngine.calls[0]["allowed_tools"])
    assert "read_local_file" not in caps
    assert "list_local_files" not in caps
    assert "search" not in caps
    # Only the host-granted conversational surface remains.
    assert caps == HOST_GRANTED
    dec = _decide(bot["policy"], "read_local_file")
    assert dec["allowed"] is False
    assert dec["effective_tier"] == "deny"
    assert dec["matched_rule"] == "fs:read"


# ── 3-4. prefix and exact-over-prefix semantics ────────────────────

def test_prefix_policy_matches_per_existing_semantics():
    bot = _bot("qa", owner="alice", policy={"fs:*": 0})
    caps = CAPS.derive_bot_capabilities(bot["policy"])
    for tool in ("read_local_file", "list_local_files", "search"):
        dec = caps["decisions"][tool]
        assert dec["allowed"] is True
        assert dec["matched_rule"] == "fs:*"   # the prefix rule fired
        assert dec["operation"] == "fs:read"
        assert tool in caps["tools"]


def test_exact_rule_overrides_prefix_rule():
    # Exact deny beats prefix allow: every tool mapped to fs:read is denied
    # because the exact rule fires first (all three tools map to fs:read).
    bot = _bot("qa", owner="alice", policy={"fs:*": 0, "fs:read": "deny"})
    caps = CAPS.derive_bot_capabilities(bot["policy"])
    for tool in ("read_local_file", "list_local_files", "search"):
        dec = caps["decisions"][tool]
        assert dec["matched_rule"] == "fs:read"    # exact wins over prefix
        assert dec["effective_tier"] == "deny"
        assert dec["allowed"] is False
        assert tool not in caps["tools"]

    # Symmetric proof: exact ALLOW beats prefix deny.
    bot2 = _bot("qa2", owner="alice", policy={"fs:*": "deny", "fs:read": 0})
    caps2 = CAPS.derive_bot_capabilities(bot2["policy"])
    dec2 = caps2["decisions"]["read_local_file"]
    assert dec2["matched_rule"] == "fs:read"
    assert dec2["effective_tier"] == 0
    assert dec2["allowed"] is True
    assert "read_local_file" in caps2["tools"]


# ── 5. default deny on no match ────────────────────────────────────

def test_no_matching_policy_rule_denies():
    bot = _bot("qa", owner="alice", policy={"cal:list": 0})
    caps = CAPS.derive_bot_capabilities(bot["policy"])
    dec = caps["decisions"]["read_local_file"]
    assert dec["allowed"] is False
    assert dec["matched_rule"] is None
    assert "no matching rule" in (dec["reason"] or "")
    assert not (set(caps["tools"]) & {"read_local_file", "list_local_files", "search"})
    # Host-granted surface survives, but no fs:read tool does.
    assert set(caps["tools"]) == HOST_GRANTED


# ── 6. policy cannot reduce the host-required safety tier ──────────

def test_policy_cannot_add_write_or_command_tools():
    # The policy tries to grant writes (and a catch-all) — the host
    # still denies them in Chat: no approval UX, nothing auto-approved.
    bot = _bot("qa", owner="alice", policy={"fs:*": 0, "fs:write": 0, "*": 2})
    caps = CAPS.derive_bot_capabilities(bot["policy"])
    assert not (set(caps["tools"]) & WRITE_TOOLS)
    assert set(caps["tools"]) <= HOST_BASE

    # The REAL spawn env reflects the same mask: read-only always on.
    cfg = chat_service._resolve_provider()
    captured = _spawn_capture(
        bot["rift"], cfg,
        bot_cfg={"bot_id": "qa", "model": bot["model"],
                 "system_prompt": bot["system_prompt"],
                 "allowed_tools": caps["tools"]})
    env_tools = set(captured["env"]["KYREX_ALLOWED_TOOLS"].split(","))
    assert env_tools == set(caps["tools"])
    assert not (env_tools & WRITE_TOOLS)
    assert captured["env"]["KYREX_READ_ONLY_REPO"] == "1"


def test_policy_raised_tier_removes_tool_never_auto_approved():
    # A policy that raises fs:read to tier 1 makes it approval-required in
    # the K-Bot tier model; Chat has no approval UX, so the tool is removed
    # from the allowlist — never silently auto-approved.
    bot = _bot("qa", owner="alice", policy={"fs:read": 1})
    caps = CAPS.derive_bot_capabilities(bot["policy"])
    dec = caps["decisions"]["read_local_file"]
    assert dec["effective_tier"] == 1          # policy DID raise the tier
    assert dec["allowed"] is False             # host does not serve T1+ reads
    assert "read_local_file" not in caps["tools"]


def test_derived_capabilities_always_subset_of_host_base():
    for pol in ({"fs:*": 0}, {"fs:read": "deny"}, {"*": 2}, {}, {"fs:write": 0}):
        caps = CAPS.derive_bot_capabilities(pol)
        assert set(caps["tools"]) <= HOST_BASE, pol


# ── 7-9. fail closed: invalid / missing / unauthorized ─────────────

@pytest.mark.parametrize("bad_policy", [
    {"fs:read": "maybe"},          # value outside the tier model
    "strict",                      # not a dict at all
    [("fs:read", 0)],              # list, not a dict
    {"fs:read": 5},                # tier outside 0..2
    {5: "fs:read"},                # non-string rule key
    None,                          # registry says no policy at all
])
def test_invalid_policy_fails_closed(bad_policy):
    _bot("qa", owner="alice")
    bots.update_bot("qa", policy=bad_policy)
    conv = chat_service.create_conversation("alice", bot_id="qa")

    with patch("chat_service._get_engine_session", new=_must_not_request_engine):
        with pytest.raises(chat_service.ChatUnavailable) as exc:
            asyncio.run(_frames(
                chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert "policy" in str(exc.value).lower()
    assert _RecordingEngine.calls == []          # never reached the engine
    stored = chat_service.get_conversation("alice", conv["conversation_id"])
    assert stored["messages"] == []              # nothing persisted/rebound


def test_nonexistent_bot_cannot_fall_back_to_global_or_default_policy():
    conv = _seed_conv("alice", "cid-ghost", bot_id="ghost")

    with patch("chat_service._get_engine_session", new=_must_not_request_engine):
        with pytest.raises(chat_service.ChatUnavailable) as exc:
            asyncio.run(_frames(
                chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert "unknown bot" in str(exc.value).lower()
    assert _RecordingEngine.calls == []
    stored = chat_service.get_conversation("alice", conv["conversation_id"])
    assert stored["bot_id"] == "ghost"
    assert "workspace_id" not in stored          # nothing silently rebound


def test_unauthorized_bot_cannot_access_another_bots_policy():
    _bot("bobs", owner="bob", policy={"fs:*": 0})  # alice is NOT the owner
    conv = _seed_conv("alice", "cid-sneaky", bot_id="bobs")

    with patch("chat_service._get_engine_session", new=_must_not_request_engine):
        with pytest.raises(chat_service.ChatUnavailable) as exc:
            asyncio.run(_frames(
                chat_service.stream_chat("alice", conv["conversation_id"], "hi")))

    assert "not available" in str(exc.value).lower()
    assert _RecordingEngine.calls == []
    # Alice can never resolve Bob's Bot — her bot list never shows it.
    assert [b["id"] for b in chat_service.list_bots_for_user("alice")] == []


# ── 10. two Bots, two policies, two capability sets ────────────────

def test_two_bots_with_different_policies_receive_different_capabilities():
    _bot("qa1", owner="alice", policy={"fs:read": 0}, rift=_rift_dir(),
         model="anthropic:claude-one", system_prompt="PROMPT ONE")
    _bot("qa2", owner="alice", policy={"fs:read": "deny"}, rift=_rift_dir(),
         model="anthropic:claude-two", system_prompt="PROMPT TWO")
    conv1 = chat_service.create_conversation("alice", bot_id="qa1")
    conv2 = chat_service.create_conversation("alice", bot_id="qa2")

    with _patch_recording_engine():
        f1 = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv1["conversation_id"], "a")))
        f2 = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv2["conversation_id"], "b")))

    assert _terminal(f1)["status"] == "complete"
    assert _terminal(f2)["status"] == "complete"
    c1, c2 = _RecordingEngine.calls
    assert "read_local_file" in c1["allowed_tools"]
    assert "read_local_file" not in c2["allowed_tools"]

    # The REAL spawn envs differ: the engine receives per-Bot capabilities.
    cfg = chat_service._resolve_provider()
    s1 = _spawn_capture(c1["workspace"], cfg, bot_cfg={
        "bot_id": "qa1", "model": c1["model"], "system_prompt": c1["system_prompt"],
        "allowed_tools": c1["allowed_tools"]})
    s2 = _spawn_capture(c2["workspace"], cfg, bot_cfg={
        "bot_id": "qa2", "model": c2["model"], "system_prompt": c2["system_prompt"],
        "allowed_tools": c2["allowed_tools"]})
    assert s1["env"]["KYREX_ALLOWED_TOOLS"] != s2["env"]["KYREX_ALLOWED_TOOLS"]
    assert "read_local_file" in s1["env"]["KYREX_ALLOWED_TOOLS"]
    assert "read_local_file" not in s2["env"]["KYREX_ALLOWED_TOOLS"]
    # Neither env can ever carry host-denied tools.
    for s in (s1, s2):
        assert not (set(s["env"]["KYREX_ALLOWED_TOOLS"].split(",")) & WRITE_TOOLS)


# ── 11. non-Bot Chat unchanged ─────────────────────────────────────

def test_non_bot_chat_unchanged():
    class FakeProvider:
        async def chat(self, model, messages, tools=None, stream_callback=None,
                       interrupt_event=None, **kw):
            assert model == "gpt-test"          # server default; nothing Bot-injected
            assert tools is None                # pure conversation path
            if stream_callback:
                stream_callback("plain answer")
            return {"role": "assistant", "content": "plain answer"}

    with patch("chat_service.get_provider", return_value=FakeProvider()), \
         patch("chat_service._get_engine_session", new=_must_not_request_engine):
        frames = asyncio.run(_frames(chat_service.stream_chat("alice", "", "hi")))

    term = _terminal(frames)
    assert term is not None and term["status"] == "complete", frames
    assert term["content"] == "plain answer"
    assert _RecordingEngine.calls == []         # provider path, not the engine

    # A non-Bot engine session (if one were spawned) still gets the FULL
    # host base — exactly the pre-Bots allowlist, unchanged.
    cfg = chat_service._resolve_provider()
    plain = _spawn_capture(_rift_dir(), cfg, None)
    assert set(plain["env"]["KYREX_ALLOWED_TOOLS"].split(",")) == HOST_BASE
    assert plain["env"]["KYREX_READ_ONLY_REPO"] == "1"


# ── 12. approval-required operations stay unavailable ──────────────

def test_approval_required_operations_remain_unavailable():
    # A policy explicitly permitting write-class ops must NOT create or
    # auto-approve anything: the host serves no T1/T2 operation in Chat.
    bot = _bot("qa", owner="alice", policy={
        "fs:write": 0, "repo:push": 0, "mail:send": 0})
    caps = CAPS.derive_bot_capabilities(bot["policy"])
    assert not (set(caps["tools"]) & WRITE_TOOLS)

    cfg = chat_service._resolve_provider()
    captured = _spawn_capture(bot["rift"], cfg, bot_cfg={
        "bot_id": "qa", "model": bot["model"], "system_prompt": bot["system_prompt"],
        "allowed_tools": caps["tools"]})
    assert "edit_file" not in captured["env"]["KYREX_ALLOWED_TOOLS"]
    assert "run_command" not in captured["env"]["KYREX_ALLOWED_TOOLS"]
    assert captured["env"]["KYREX_READ_ONLY_REPO"] == "1"


# ── session cache: policy change never serves stale permissions ────

def test_policy_change_respawns_session_never_stale_permissions():
    """Tightening a Bot's policy must re-spawn the engine process — a cached
    session spawned under the old, more permissive allowlist is closed."""
    ws = Path(_rift_dir())
    generous = CAPS.derive_bot_capabilities({"fs:*": 0})["tools"]
    strict = CAPS.derive_bot_capabilities({"fs:read": "deny"})["tools"]

    with patch.object(chat_service, "EngineSession", _StubSession):
        s1 = chat_service._get_engine_session(
            "alice", "cid", ws,
            {"bot_id": "qa", "model": "m", "system_prompt": "",
             "allowed_tools": generous})
        s2 = chat_service._get_engine_session(
            "alice", "cid", ws,
            {"bot_id": "qa", "model": "m", "system_prompt": "",
             "allowed_tools": strict})
        s3 = chat_service._get_engine_session(
            "alice", "cid", ws,
            {"bot_id": "qa", "model": "m", "system_prompt": "",
             "allowed_tools": strict})

    assert s1._closed          # permissive session never reused
    assert not s2._closed
    assert s3 is s2            # identical capabilities still reuse
    assert len(_StubSession.instances) == 2


# ── end-to-end: the Bot-bound request carries caps into the engine ──

def test_bot_bound_turn_carries_expected_capabilities_into_engine():
    """One end-to-end proof: a Bot-bound Chat turn drives stream_chat, and
    the REAL EngineSession it requests spawns the engine with the
    policy-derived KYREX_ALLOWED_TOOLS and unchanged read-only host env."""
    bot = _bot("qa", owner="alice", policy={"fs:read": 0},
               system_prompt="Be terse.", model="anthropic:claude-e2e")
    conv = chat_service.create_conversation("alice", bot_id="qa")

    with _patch_recording_engine():
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "read the rift")))

    assert _terminal(frames)["status"] == "complete"
    delivered = _RecordingEngine.calls[0]["allowed_tools"]
    # The same capability list the policy decision produced...
    expected = CAPS.derive_bot_capabilities(bot["policy"])["tools"]
    assert set(delivered) == set(expected)

    # ...arrives at the actual engine spawn env, in the Bot's Rift, read-only.
    cfg = chat_service._resolve_provider()
    captured = _spawn_capture(bot["rift"], cfg, bot_cfg={
        "bot_id": "qa", "model": bot["model"],
        "system_prompt": bot["system_prompt"],
        "allowed_tools": delivered})
    assert set(captured["env"]["KYREX_ALLOWED_TOOLS"].split(",")) == set(expected)
    assert captured["env"]["KYREX_READ_ONLY_REPO"] == "1"
    assert captured["cwd"] == bot["rift"]