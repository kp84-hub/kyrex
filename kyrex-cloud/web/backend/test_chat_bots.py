"""Bots-in-Kyrex-Chat, slice 1: Bot discovery + conversation binding.

Focused regression coverage for the first Bots-in-Chat slice:

  1. GET /api/bots returns the Bots visible to the authenticated user
     (owner match or operator-created), with UI metadata only — never
     rift paths, policy, or credentials.
  2. A user cannot see (or bind) another user's Bot.
  3. Creating a conversation with bot_id persists the binding.
  4. Loading/reloading the conversation preserves bot_id.
  5. A nonexistent Bot is rejected at bind time.
  6. An unauthorized Bot is rejected at bind time.
  7. A Bot-bound conversation fails closed — it cannot silently fall
     back to another Bot/Rift/workspace when the Bot or its Rift is gone.
  8. Existing non-Bot conversations behave exactly as before (no bot_id,
     ordinary create/turn/persist still works).

The engine provider path is stubbed exactly like test_chat_api.py (a fake
provider through kyrex.providers.get_provider). For the repo-aware engine
branch used by bot-bound turns, the EngineSession factory is stubbed so no
real core_bridge.py process is spawned; the recorded workspace proves the
binding plumbing (the engine runs inside the Bot's Rift).

Run: python3 -m pytest test_chat_bots.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-bot-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BACKEND)
sys.path.insert(0, os.path.dirname(_BACKEND))

import main  # noqa: E402  (after env setup; seeds the shared app/session map)
import chat_service  # noqa: E402
import bots  # noqa: E402  — the authoritative registry under test


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    """Fresh chat store + fresh Bot registry."""
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    bots.save_bots({})


def setup_function():
    _reset()
    # One seeded browser session per test user.
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()


def _rift_dir() -> str:
    """A real, resolvable Rift directory for a test Bot."""
    return tempfile.mkdtemp(prefix="kyrex-bot-rift-")


def _bot(bot_id="qa", owner="", status="running", rift=None):
    # Default to a started (running) Bot: a Bot must be running to be bound or
    # to serve a turn. Lifecycle-specific behavior is covered by
    # test_bot_lifecycle.py.
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "anthropic:claude-test",
        rift or _rift_dir(), status=status, owner=owner,
    )


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


async def _frames(agen):
    out = []
    async for f in agen:
        out.append(f)
    return out


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


class _FakeEngineSession:
    """EngineSession stand-in: records its workspace, answers one turn."""

    def __init__(self, workspace, answer="bot answer", bot_cfg=None):
        self.workspace = Path(workspace)
        self.answer = answer
        bot_cfg = bot_cfg or {}
        self.bot_id = (bot_cfg.get("bot_id") or "").strip() or None
        self.model = (bot_cfg.get("model") or "").strip() or None
        self.system_prompt = (bot_cfg.get("system_prompt") or "").strip() or None

    def run_turn(self, text, on_token, cancel_check=None):
        on_token(self.answer)
        return self.answer, None

    def interrupt(self):
        pass

    def close(self):
        pass


# ── 1. discovery ──────────────────────────────────────────────────

def test_get_api_bots_returns_users_visible_bots():
    _bot("qa", owner="alice")
    _bot("shared", owner="")        # operator-created → visible to everyone
    _bot("bobs", owner="bob")       # another user's → must NOT appear

    r = _client("alice").get("/api/bots")
    assert r.status_code == 200, r.text
    payload = r.json()["bots"]
    ids = {b["id"] for b in payload}
    assert ids == {"qa", "shared"}, payload
    # UI metadata only — no internals of any kind.
    for b in payload:
        assert set(b.keys()) == {
            "id", "name", "status", "model", "available", "manageable", "claimable",
        }, b
        assert "rift" not in b and "policy" not in b
        assert "system_prompt" not in b and "owner" not in b
    by_id = {b["id"]: b for b in payload}
    # `claimable` distinguishes a visible OWNERLESS Bot (claim offered) from an
    # owned one (manageable). It never leaks ownership or grants management.
    assert by_id["qa"]["manageable"] is True and by_id["qa"]["claimable"] is False
    assert by_id["shared"]["manageable"] is False and by_id["shared"]["claimable"] is True


def test_get_api_bots_requires_auth():
    r = _client().get("/api/bots")
    assert r.status_code == 200  # alice session works
    from fastapi.testclient import TestClient
    anon = TestClient(main.app)
    assert anon.get("/api/bots").status_code == 401


def test_get_api_bots_never_silently_swallows_registry_errors():
    # Corrupt the registry file: the endpoint must 500, not return [].
    with open(bots.BOTS_FILE, "w") as f:
        f.write("{ not valid json ")
    r = _client().get("/api/bots")
    assert r.status_code == 500, r.text
    assert "registr" in r.json()["detail"].lower()


# ── 2. cross-user isolation ───────────────────────────────────────

def test_user_cannot_see_another_users_bot():
    _bot("bobs", owner="bob")
    r = _client("alice").get("/api/bots")
    assert r.status_code == 200
    ids = {b["id"] for b in r.json()["bots"]}
    assert "bobs" not in ids, "another user's Bot must not be listed"


def test_creating_with_another_users_bot_rejected():
    _bot("bobs", owner="bob")
    r = _client("alice").post("/api/conversations",
                              json={"bot_id": "bobs"})
    assert r.status_code == 400, r.text
    assert "not available" in r.json()["detail"]


# ── 3. binding persists on create ─────────────────────────────────

def test_create_conversation_with_bot_id_persists_binding():
    _bot("qa", owner="alice")
    r = _client().post("/api/conversations", json={"bot_id": "qa"})
    assert r.status_code == 200, r.text
    conv = r.json()
    assert conv["bot_id"] == "qa"
    # Whatever is stored on disk is authoritative for reloads.
    stored = chat_service.get_conversation("alice", conv["conversation_id"])
    assert stored["bot_id"] == "qa"

    # The list surface exposes the binding too.
    listing = _client().get("/api/conversations").json()["conversations"]
    assert len(listing) == 1
    assert listing[0]["bot_id"] == "qa"


# ── 4. reload preserves the binding ───────────────────────────────

def test_reload_preserves_bot_id():
    _bot("qa", owner="alice")
    conv = chat_service.create_conversation("alice", bot_id="qa")

    # Simulate a page reload: a brand-new read of the persisted record.
    conv_id = conv["conversation_id"]
    for _ in range(2):  # repeat reads == repeated reloads
        reloaded = chat_service.get_conversation("alice", conv_id)
        assert reloaded is not None
        assert reloaded["bot_id"] == "qa"

    # GET endpoint returns the same persisted binding.
    r = _client().get(f"/api/conversations/{conv_id}")
    assert r.status_code == 200
    assert r.json()["bot_id"] == "qa"

    # A bot-bound turn re-resolves the SAME Bot after reload: the engine
    # workspace is the Bot's Rift, and the binding is untouched afterwards.
    bot = bots.get_bot("qa")
    seen_ws = []
    fake = _FakeEngineSession(bot["rift"], answer="persisted answer")

    def _fake_factory(user_, cid, workspace, bot_cfg=None):
        seen_ws.append(Path(workspace))
        return fake

    with patch("chat_service._get_engine_session", side_effect=_fake_factory):
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", conv_id, "again")))

    term = _terminal(frames)
    assert term is not None and term["status"] == "complete", frames
    assert seen_ws == [Path(bot["rift"])], seen_ws
    # Turn persisted; binding survived it.
    after = chat_service.get_conversation("alice", conv_id)
    assert after["bot_id"] == "qa"
    assert [m["role"] for m in after["messages"]] == ["user", "assistant"]


# ── 5. nonexistent Bot rejected ───────────────────────────────────

def test_nonexistent_bot_rejected():
    r = _client().post("/api/conversations", json={"bot_id": "ghost"})
    assert r.status_code == 400, r.text
    assert "unknown bot" in r.json()["detail"].lower()
    # Nothing was created.
    assert chat_service.list_conversations("alice") == []


def test_create_conversation_without_bot_id_still_works():
    r = _client().post("/api/conversations", json={})
    assert r.status_code == 200, r.text


# ── 6. unauthorized Bot rejected ──────────────────────────────────

def test_unauthorized_bot_rejected_fail_closed():
    _bot("bobs", owner="bob")
    r = _client("alice").post("/api/conversations", json={"bot_id": "bobs"})
    assert r.status_code == 400, r.text
    assert "not available" in r.json()["detail"]
    # The owner CAN bind their own Bot.
    ok = _client("bob").post("/api/conversations", json={"bot_id": "bobs"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["bot_id"] == "bobs"


# ── 7. fail closed: no silent fallback ────────────────────────────

def test_bot_bound_conversation_cannot_fall_back_when_bot_gone():
    _bot("qa", owner="alice")
    conv = chat_service.create_conversation("alice", bot_id="qa")
    conv_id = conv["conversation_id"]

    # The Bot disappears from the registry after the conversation exists.
    bots.remove_bot("qa")

    with pytest.raises(chat_service.ChatUnavailable) as exc:
        asyncio.run(_frames(
            chat_service.stream_chat("alice", conv_id, "hello")))
    msg = str(exc.value).lower()
    assert "bot" in msg and "qa" in msg, msg
    # The conversation still names its Bot — nothing was silently rebound.
    after = chat_service.get_conversation("alice", conv_id)
    assert after["bot_id"] == "qa"
    assert "workspace_id" not in after


def test_bot_bound_conversation_fails_closed_over_http_sse():
    """The UI-facing surface reports the failure as a clean SSE error frame."""
    _bot("qa", owner="alice")
    r = _client().post("/api/conversations", json={"bot_id": "qa"})
    conv_id = r.json()["conversation_id"]
    bots.remove_bot("qa")  # gone after binding, before the next turn

    error_frames = []
    with _client().stream("POST", "/api/chat",
                          json={"conversation_id": conv_id,
                                "message": "hello"}) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                if payload.get("type") == "error":
                    error_frames.append(payload)

    assert len(error_frames) == 1
    assert "bot" in error_frames[0]["message"].lower()
    assert "qa" in error_frames[0]["message"]
    # No user message was persisted for the failed turn (nothing to fall back on).
    stored = chat_service.get_conversation("alice", conv_id)
    assert stored["bot_id"] == "qa"
    assert stored["messages"] == []


def test_bot_bound_conversation_cannot_fall_back_when_rift_gone():
    import shutil
    rift = _rift_dir()
    _bot("qa", owner="alice", rift=rift)
    conv = chat_service.create_conversation("alice", bot_id="qa")
    conv_id = conv["conversation_id"]

    # The Rift directory disappears after binding.
    shutil.rmtree(rift, ignore_errors=True)

    with pytest.raises(chat_service.ChatUnavailable) as exc:
        asyncio.run(_frames(
            chat_service.stream_chat("alice", conv_id, "hello")))
    assert "rift" in str(exc.value).lower(), exc
    # No workspace was attached as a substitute; binding intact.
    after = chat_service.get_conversation("alice", conv_id)
    assert after["bot_id"] == "qa"
    assert "workspace_id" not in after


def test_bot_bound_conversation_rejects_explicit_workspace_attach():
    _bot("qa", owner="alice")
    conv = chat_service.create_conversation("alice", bot_id="qa")

    with pytest.raises(chat_service.ChatUnavailable) as exc:
        asyncio.run(_frames(
            chat_service.stream_chat("alice", conv["conversation_id"], "hello",
                                     workspace_id="default")))
    msg = str(exc.value).lower()
    assert "workspace" in msg and "bot" in msg, msg


def test_attach_workspace_endpoint_rejects_bot_bound_conversation():
    _bot("qa", owner="alice")
    r = _client().post("/api/conversations", json={"bot_id": "qa"})
    conv_id = r.json()["conversation_id"]

    resp = _client().post("/api/chat/workspace",
                          json={"conversation_id": conv_id,
                                "workspace_id": "default"})
    assert resp.status_code == 400, resp.text
    assert "bot-bound" in resp.json()["detail"]
    # Nothing was stored — the binding stayed clean.
    stored = chat_service.get_conversation("alice", conv_id)
    assert stored["bot_id"] == "qa"
    assert "workspace_id" not in stored


# ── 8. non-Bot conversations unchanged ────────────────────────────

def test_non_bot_conversations_work_exactly_as_before():
    class FakeProvider:
        async def chat(self, model, messages, tools=None, stream_callback=None,
                       interrupt_event=None, **kw):
            if stream_callback:
                stream_callback("plain")
                stream_callback(" answer")
            return {"role": "assistant", "content": "plain answer"}

    with patch("chat_service.get_provider", return_value=FakeProvider()):
        frames = asyncio.run(_frames(
            chat_service.stream_chat("alice", "", "hi")))

    term = _terminal(frames)
    assert term is not None and term["status"] == "complete"
    assert term["content"] == "plain answer"

    convs = chat_service.list_conversations("alice")
    assert len(convs) == 1
    conv = convs[0]
    # List surface mirrors workspace_id: the key is present but empty.
    assert conv.get("bot_id") is None
    # The persisted record carries no bot_id at all (nothing bound).
    stored = chat_service.get_conversation("alice", conv["conversation_id"])
    assert "bot_id" not in stored
    assert [m["role"] for m in stored["messages"]] == ["user", "assistant"]
    assert stored["messages"][1]["content"] == "plain answer"

    # API surface identical: create without bot_id has no bot_id key.
    r = _client().post("/api/conversations", json={"title": "plain"})
    assert r.status_code == 200


# ── IDE bot management ─────────────────────────────────────────────

def test_create_api_bot_is_user_owned_and_available():
    r = _client("alice").post("/api/bots", json={
        "id": "ide-qa", "name": "IDE QA", "model": "gpt-5.6-luna",
    })
    assert r.status_code == 200, r.text
    assert r.json() == {
        "id": "ide-qa", "name": "IDE QA", "status": "stopped",
        "model": "gpt-5.6-luna", "available": True, "manageable": True,
        "claimable": False,
    }
    stored = bots.get_bot("ide-qa")
    assert stored["owner"] == "alice"
    assert Path(stored["rift"]).is_dir()


def test_create_api_bot_validates_id_and_authentication():
    assert _client("alice").post("/api/bots", json={
        "id": "../escape", "name": "Bad", "model": "m",
    }).status_code == 400

    from fastapi.testclient import TestClient
    assert TestClient(main.app).post("/api/bots", json={
        "id": "qa", "name": "QA", "model": "m",
    }).status_code == 401


def test_owner_can_start_and_stop_api_bot():
    _bot("owned", owner="alice")
    started = _client("alice").patch("/api/bots/owned", json={"status": "running"})
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "running"
    stopped = _client("alice").patch("/api/bots/owned", json={"status": "stopped"})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "stopped"


def test_user_cannot_mutate_another_users_or_operator_bot():
    _bot("bobs-managed", owner="bob")
    _bot("operator-managed", owner="")
    assert _client("alice").patch(
        "/api/bots/bobs-managed", json={"status": "running"}).status_code == 403
    assert _client("alice").patch(
        "/api/bots/operator-managed", json={"status": "running"}).status_code == 403
