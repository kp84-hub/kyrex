"""Privacy preferences are authenticated, owner-scoped, and enforced on turns."""
import asyncio
import json
import os
import stat

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")

import chat_privacy
import chat_service as chat


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KYREX_PROVIDER", "openai")
    monkeypatch.setenv("KYREX_API_KEY", "test-key")
    monkeypatch.setenv("KYREX_MODEL", "test-model")
    monkeypatch.setattr(chat, "_data_dir", lambda: tmp_path)
    yield
    chat.close_all_engine_sessions()


def test_preferences_are_owner_scoped_and_private():
    assert chat_privacy.settings("alice")["share_saved_memory"] is True
    chat_privacy.save_settings("alice", {"share_saved_memory": False})
    assert chat_privacy.settings("alice")["share_saved_memory"] is False
    assert chat_privacy.settings("bob")["share_saved_memory"] is True
    path = chat_privacy._path("alice")
    assert "alice" not in path.name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    path.write_text('broken')
    assert chat_privacy.settings("alice")["share_saved_memory"] is False


@pytest.mark.parametrize("body", [{}, [], {"share_saved_memory": "false"},
    {"share_saved_memory": 0}, {"share_saved_memory": False, "user": "bob"}])
def test_bad_preferences_cannot_enable_sharing(body):
    with pytest.raises(ValueError):
        chat_privacy.save_settings("alice", body)


def test_memory_toggle_changes_the_next_request_in_same_chat(monkeypatch):
    payloads, memory_reads = [], []
    async def reply(**kwargs):
        payloads.append(kwargs["messages"])
        return {"role": "assistant", "content": "ok", "tool_calls": None}
    from types import SimpleNamespace
    monkeypatch.setattr(chat, "get_provider", lambda *a, **kw: SimpleNamespace(chat=reply))
    def memory(user):
        memory_reads.append(user)
        return "User-saved memory: family-private-fact"
    monkeypatch.setattr(chat.chat_memory, "context", memory)
    cid = chat.create_conversation("alice")["conversation_id"]
    async def run():
        return [f async for f in chat.stream_chat("alice", cid, "hi")]
    assert asyncio.run(run())[-1]["status"] == "complete"
    assert "family-private-fact" in json.dumps(payloads[-1])
    chat_privacy.save_settings("alice", {"share_saved_memory": False})
    assert asyncio.run(run())[-1]["status"] == "complete"
    assert "family-private-fact" not in json.dumps(payloads[-1])
    assert memory_reads == ["alice"]
    chat_privacy.save_settings("alice", {"share_saved_memory": True})
    asyncio.run(run())
    assert "family-private-fact" in json.dumps(payloads[-1])


def test_conversations_are_published_with_private_permissions():
    conv = chat.create_conversation("alice")
    path = chat._conv_path("alice", conv["conversation_id"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert chat.get_conversation("alice", conv["conversation_id"]) == conv
    assert chat.get_conversation("bob", conv["conversation_id"]) is None
    cid = chat.ensure_level6_preview_conversation("alice", "L6")
    assert stat.S_IMODE(chat._conv_path("alice", cid).stat().st_mode) == 0o600


def test_memory_toggle_refreshes_a_reused_engine_session(monkeypatch, tmp_path):
    from pathlib import Path
    from unittest.mock import MagicMock
    sessions = []
    class Session:
        def __init__(self, ws, cfg, bot_cfg=None):
            self.workspace = Path(ws)
            self.bot_id = (bot_cfg or {}).get("bot_id")
            self.allowed_tools = chat._effective_caps(bot_cfg or {})
            self._closed = False
            self._proc = MagicMock()
            self._proc.poll.return_value = None
            self.surface_context = None
            self.contexts = []
            sessions.append(self)
        def run_turn(self, text, on_token, cancel_check=None):
            self.contexts.append(self.surface_context)
            return "ok", None
        def close(self):
            self._closed = True
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("KYREX_CHAT_WORKSPACE", str(workspace))
    monkeypatch.setattr(chat, "EngineSession", Session)
    monkeypatch.setattr(chat.chat_memory, "context", lambda user: "saved-private-fact")
    cid = chat.create_conversation("alice")["conversation_id"]
    async def run():
        return [f async for f in chat.stream_chat("alice", cid, "hi", workspace_id="default")]
    assert asyncio.run(run())[-1]["status"] == "complete"
    chat_privacy.save_settings("alice", {"share_saved_memory": False})
    assert asyncio.run(run())[-1]["status"] == "complete"
    assert len(sessions) == 1
    assert "saved-private-fact" in sessions[0].contexts[0]
    assert "saved-private-fact" not in sessions[0].contexts[1]


def test_privacy_api_uses_authenticated_owner(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setitem(main.sessions, "privacy-a", "alice")
    monkeypatch.setitem(main.sessions, "privacy-b", "bob")
    alice = TestClient(main.app, cookies={"session": "privacy-a"})
    bob = TestClient(main.app, cookies={"session": "privacy-b"})
    assert alice.put("/api/chat/privacy", json={"share_saved_memory": False}).status_code == 200
    assert alice.get("/api/chat/privacy").json()["share_saved_memory"] is False
    assert bob.get("/api/chat/privacy").json()["share_saved_memory"] is True
    assert bob.put("/api/chat/privacy", json={"share_saved_memory": "false"}).status_code == 400
    assert TestClient(main.app).get("/api/chat/privacy").status_code == 401
    assert TestClient(main.app).put("/api/chat/privacy", json={"share_saved_memory": True}).status_code == 401


def test_unexpected_chat_error_does_not_echo_private_details(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setitem(main.sessions, "privacy-error", "alice")
    async def broken(*args, **kwargs):
        yield {"type": "conversation", "conversation_id": "private-test"}
        raise RuntimeError("private email body; password=private-credential")
    monkeypatch.setattr(chat, "stream_chat", broken)
    response = TestClient(main.app, cookies={"session": "privacy-error"}).post(
        "/api/chat", json={"message": "hi"})
    assert "Chat request failed. Try again." in response.text
    assert "private email body" not in response.text and "private-credential" not in response.text
