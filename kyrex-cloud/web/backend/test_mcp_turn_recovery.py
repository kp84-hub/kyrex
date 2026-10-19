"""Blocking host reads must not strand an engine turn or corrupt its retry."""
import asyncio
import json
import threading
import time

import pytest

from test_chat_failure_paths import _bare_session
import chat_service
import remote_mcp_connections


FRAME = {"type": "confirm_request", "id": "read-one", "value": "mcp_read",
         "action": "call", "connection_id": "owner-connection",
         "tool": "GOOGLEDRIVE_FIND_FILE", "arguments": {"query": "private query"}}


def session():
    value = _bare_session()
    value.mcp_owner = "alice"
    value.bot_id = None
    value.allowed_tools = {"mcp_read"}
    value._fitness_read_only = False
    value.sent = []
    value._send = value.sent.append
    return value


def test_mcp_success_replies_once_and_reports_safe_progress(monkeypatch):
    value = session()
    monkeypatch.setattr(value, "_handle_mcp_read", lambda frame, cancel: (True, {"result": "document"}))
    stages = []
    value._progress_callback = stages.append
    for frame in (FRAME, {"type": "chat_done", "content": "Found it"},
                  {"type": "phase", "value": "IDLE"}):
        value._frames.put(frame)
    assert value.run_turn("read my Drive", lambda text: None) == ("Found it", None)
    responses = [frame for frame in value.sent if frame["type"] == "confirm_response"]
    assert responses == [{"type": "confirm_response", "id": "read-one",
                          "approved": True, "result": {"result": "document"}}]
    assert {"stage": "Reading connected MCP data…"} in stages
    assert "private query" not in json.dumps(stages)
    assert not value._closed and not value._turn_lock.locked()


def test_slow_mcp_times_out_closes_session_and_discards_late_result(monkeypatch):
    value = session()
    monkeypatch.setattr(chat_service, "MCP_READ_TIMEOUT", 0.03)
    release, done = threading.Event(), threading.Event()
    abandoned = []

    def read(frame, cancelled):
        try:
            assert release.wait(3)
            abandoned.append(cancelled())
            return True, {"result": "late document"}
        finally:
            done.set()

    monkeypatch.setattr(value, "_handle_mcp_read", read)
    value._frames.put(FRAME)
    started = time.monotonic()
    try:
        with pytest.raises(chat_service.EngineSessionError, match="MCP service did not respond"):
            value.run_turn("read Drive", lambda text: None)
        assert time.monotonic() - started < 1
        assert value._closed and not value._turn_lock.locked()
    finally:
        release.set()
        assert done.wait(2)
    assert abandoned == [True]
    assert not any(frame["type"] == "confirm_response" for frame in value.sent)


def test_stop_releases_turn_while_mcp_provider_is_still_blocked(monkeypatch):
    value = session()
    started, release, done, cancel = (threading.Event() for _ in range(4))
    observed = []

    def read(frame, cancelled):
        started.set()
        try:
            assert release.wait(3)
            observed.append(cancelled())
            return True, {"result": "late"}
        finally:
            done.set()

    monkeypatch.setattr(value, "_handle_mcp_read", read)
    value._frames.put(FRAME)
    result = []
    worker = threading.Thread(target=lambda: result.append(value.run_turn(
        "read Drive", lambda text: None, cancel.is_set)))
    worker.start()
    try:
        assert started.wait(2)
        cancel.set()
        worker.join(1)
        assert not worker.is_alive()
        assert result == [("", None)]
        assert value._closed and not value._turn_lock.locked()
        assert not done.is_set()  # host request outlives the safely retired turn
    finally:
        release.set()
        worker.join(2)
        assert done.wait(2)
    assert observed == [True]
    assert not any(frame["type"] == "confirm_response" for frame in value.sent)


def test_host_rechecks_cancellation_before_actual_remote_submission(monkeypatch):
    value = session()
    cancel = threading.Event()
    submitted = []

    class Store:
        def call(self, owner, connection_id, name, arguments, *, authorize):
            assert owner == "alice" and connection_id == "owner-connection"
            cancel.set()  # cancellation lands during remote metadata/account checks
            if not authorize():
                raise remote_mcp_connections.MCPConnectionError("Read cancelled before submission")
            submitted.append(name)
            return {"result": "unexpected"}

    monkeypatch.setattr(remote_mcp_connections, "RemoteMCPConnections", Store)
    approved, result = value._handle_mcp_read(FRAME, cancel.is_set)
    assert not approved and "cancelled" in result["error"]
    assert submitted == []


def test_closed_engine_cannot_submit_mcp_read(monkeypatch):
    value = session()
    value.close()
    monkeypatch.setattr(remote_mcp_connections, "RemoteMCPConnections",
                        lambda: pytest.fail("closed engine constructed a credential store"))
    assert value._handle_mcp_read(FRAME)[0] is False


def test_busy_retry_does_not_persist_duplicate_or_replace_active_callbacks(monkeypatch):
    value = session()
    marker = lambda payload: None
    value._progress_callback = marker
    original = {"conversation_id": "conversation-one", "messages": [
        {"id": "original-turn", "role": "user", "content": "read Drive"}]}
    monkeypatch.setattr(chat_service, "_engine_sessions", {("alice", "conversation-one"): value})
    monkeypatch.setattr(chat_service, "get_conversation", lambda *args: original)
    monkeypatch.setattr(chat_service, "_write", lambda *args: pytest.fail("busy retry persisted a message"))
    value._turn_lock.acquire()

    async def collect():
        return [frame async for frame in chat_service.stream_chat(
            "alice", "conversation-one", "read Drive", request_id="retry")]

    try:
        with pytest.raises(chat_service.ChatUnavailable, match="previous reply is still running or stopping"):
            asyncio.run(collect())
        assert len(original["messages"]) == 1
        assert value._progress_callback is marker
        assert value._turn_lock.locked() and not value._closed
        assert value.sent == []
    finally:
        value._turn_lock.release()


def test_busy_check_is_scoped_to_the_authenticated_owner(monkeypatch):
    value = session()
    monkeypatch.setattr(chat_service, "_engine_sessions", {("alice", "same-id"): value})
    monkeypatch.setattr(chat_service, "get_conversation", lambda *args: {
        "conversation_id": "same-id", "messages": []})
    class ReachedOwnerFlow(Exception):
        pass
    def reached(*args):
        raise ReachedOwnerFlow()
    monkeypatch.setattr(chat_service.email_identity, "is_booking_question", reached)
    value._turn_lock.acquire()
    async def collect():
        return [frame async for frame in chat_service.stream_chat("bob", "same-id", "hi")]
    try:
        with pytest.raises(ReachedOwnerFlow):
            asyncio.run(collect())
    finally:
        value._turn_lock.release()
