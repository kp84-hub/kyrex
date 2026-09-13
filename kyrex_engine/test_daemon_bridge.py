"""Tests for daemon_bridge — the background-engine host.

Covers the guarantees the IDE relies on:
- workspace key stability (must match the Rust side byte-for-byte)
- control file round-trip
- stdout tee capture (complete lines only, passthrough intact)
- replay-on-connect + live streaming with no lost lines in between
- background approval resolution: edits approved, deletions denied
- replay lines are tagged so the UI won't reopen stale approval modals
- idle exit and shutdown-message teardown
"""

import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from daemon_bridge import (  # noqa: E402
    DaemonHub,
    TeeStdout,
    control_file_path,
    daemon_key,
    idle_exit_seconds,
    is_daemon_mode,
    normalize_workspace,
    read_control_file,
    remove_control_file,
    write_control_file,
    _fnv1a64,
)


class _StringSink:
    def __init__(self):
        self._buf = []

    def write(self, s):
        self._buf.append(s)
        return len(s)

    def flush(self):
        pass

    def getvalue(self):
        return "".join(self._buf)


class HubHarness:
    def __init__(self, tmp_path, monkeypatch, idle_exit=60.0):
        monkeypatch.setenv("KYREX_HOME", str(tmp_path))
        self.hub = DaemonHub("/tmp/ws-test", idle_exit=idle_exit)
        self.received_lines = []
        self.hub.on_line = self.received_lines.append
        self.hub.branch_provider = lambda: "main"
        self.hub.bind()
        self.port = self.hub.port
        self.thread = threading.Thread(target=self.hub.serve, daemon=True)
        self.thread.start()

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.settimeout(5)
        return sock

    def _recv_lines(self, sock, count, timeout=5.0):
        sock.settimeout(timeout)
        buf = b""
        lines = []
        while len(lines) < count:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf and len(lines) < count:
                raw, buf = buf.split(b"\n", 1)
                if raw.strip():
                    lines.append(raw.decode("utf-8"))
        return lines

    def close(self):
        self.hub.request_stop()
        self.thread.join(timeout=3)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    h = HubHarness(tmp_path, monkeypatch)
    yield h
    h.close()


# ── workspace key ──────────────────────────────────────────────────────────

class TestWorkspaceKey:
    def test_fnv1a64_reference_vectors(self):
        # Standard FNV-1a 64-bit test vectors
        assert _fnv1a64(b"") == 0xCBF29CE484222325
        assert _fnv1a64(b"a") == 0xAF63DC4C8601EC8C
        assert _fnv1a64(b"foobar") == 0x85944171F73967E8

    def test_key_is_stable_and_hex(self):
        key = daemon_key("/tmp/some/workspace")
        assert len(key) == 16
        assert int(key, 16) >= 0
        assert daemon_key("/tmp/some/workspace") == key

    def test_normalization_merges_separator_styles(self):
        assert normalize_workspace("/a/b/") == normalize_workspace("/a/b")
        assert normalize_workspace("C:\\repo\\sub") == "C:/repo/sub"
        assert daemon_key("C:\\repo\\sub") == daemon_key("C:/repo/sub")

    def test_different_workspaces_differ(self):
        assert daemon_key("/ws/a") != daemon_key("/ws/b")


# ── control file ───────────────────────────────────────────────────────────

class TestControlFile:
    def test_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KYREX_HOME", str(tmp_path))
        path = control_file_path("/tmp/ws")
        assert path.parent.name == "daemons"

        write_control_file("/tmp/ws", 4711)
        info = read_control_file("/tmp/ws")
        assert info == {"pid": os.getpid(), "port": 4711}

        remove_control_file("/tmp/ws")
        assert read_control_file("/tmp/ws") is None

    def test_read_missing_file_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KYREX_HOME", str(tmp_path))
        assert read_control_file("/nope") is None


# ── tee ────────────────────────────────────────────────────────────────────

class TestTee:
    def test_captures_complete_lines_and_passes_through(self):
        hub = DaemonHub("/tmp/ws")
        sink = _StringSink()
        tee = TeeStdout(sink, hub)

        tee.write('{"type": "token", "content": "he')
        assert list(hub.replay_buffer) == []  # partial line not captured yet
        tee.write('llo"}\n')
        tee.write('{"type": "phase"}\n{"type": "x"}\n')

        assert list(hub.replay_buffer) == [
            '{"type": "token", "content": "hello"}',
            '{"type": "phase"}',
            '{"type": "x"}',
        ]
        # Original stream still received everything byte-for-byte
        assert sink.getvalue() == (
            '{"type": "token", "content": "he'
            'llo"}\n{"type": "phase"}\n{"type": "x"}\n'
        )


# ── hub: replay + live streaming ───────────────────────────────────────────

class TestReplayAndLive:
    def test_replay_marker_then_buffer_then_live(self, harness):
        harness.hub.observe_line('{"type": "session_state"}')
        harness.hub.observe_line('{"type": "token", "content": "hi"}')

        client = harness.connect()
        lines = harness._recv_lines(client, 3)

        marker = json.loads(lines[0])
        assert marker["type"] == "session_replay"
        assert marker["count"] == 2
        assert marker["branch"] == "main"
        assert json.loads(lines[1]) == {"type": "session_state", "replay": True}
        assert json.loads(lines[2]) == {"type": "token", "content": "hi", "replay": True}

        # Live line after attach arrives on the socket
        harness.hub.observe_line('{"type": "token", "content": "live"}')
        live = harness._recv_lines(client, 1)
        assert json.loads(live[0])["content"] == "live"
        client.close()

    def test_client_lines_reach_on_line(self, harness):
        client = harness.connect()
        harness._recv_lines(client, 1)  # consume replay marker

        client.sendall(b'{"type": "chat", "content": "hello"}\n')
        deadline = time.monotonic() + 5
        while not harness.received_lines and time.monotonic() < deadline:
            time.sleep(0.02)

        assert harness.received_lines == ['{"type": "chat", "content": "hello"}']
        client.close()

    def test_newest_client_wins(self, harness):
        first = harness.connect()
        harness._recv_lines(first, 1)

        second = harness.connect()
        harness._recv_lines(second, 1)  # second client gets its replay

        harness.hub.observe_line('{"type": "token", "content": "live"}')
        live = harness._recv_lines(second, 1)
        assert json.loads(live[0])["content"] == "live"
        first.close()
        second.close()


# ── background approvals ───────────────────────────────────────────────────

class TestBackgroundApprovals:
    def test_propose_edit_auto_approved_while_detached(self, harness):
        import kyrex.toolbox as tb
        edit_id = "edit-1"
        event = threading.Event()
        tb._pending_edits[edit_id] = event
        try:
            payload = json.dumps({
                "type": "propose_edit",
                "editId": edit_id,
                "filePath": "/tmp/x.py",
                "content": "x = 1",
            })
            harness.hub.observe_line(payload)
            assert event.wait(timeout=2)
            assert tb._edit_results[edit_id] is True
            notes = [json.loads(m) for m in harness.hub.replay_buffer]
            assert any(
                n.get("type") == "system" and "auto-approved edit" in n.get("content", "")
                for n in notes
            )
        finally:
            tb._pending_edits.pop(edit_id, None)
            tb._edit_results.pop(edit_id, None)

    def test_deletion_gate_auto_denied_while_detached(self, harness):
        import kyrex.toolbox as tb
        confirm_id = "del-1"
        event = threading.Event()
        tb._pending_confirmations[confirm_id] = event
        try:
            payload = json.dumps({
                "type": "confirm_request",
                "id": confirm_id,
                "value": "deletion",
                "path": "DELETE: rm -rf build",
                "diff": "...",
            })
            harness.hub.observe_line(payload)
            assert event.wait(timeout=2)
            assert tb._confirmation_results[confirm_id] is False
        finally:
            tb._pending_confirmations.pop(confirm_id, None)
            tb._confirmation_results.pop(confirm_id, None)

    def test_edit_gate_auto_approved_while_detached(self, harness):
        import kyrex.toolbox as tb
        confirm_id = "edit-gate-1"
        event = threading.Event()
        tb._pending_confirmations[confirm_id] = event
        try:
            payload = json.dumps({
                "type": "confirm_request",
                "id": confirm_id,
                "value": "edit",
                "path": "/tmp/x.py",
                "diff": "--- a\n+++ b",
            })
            harness.hub.observe_line(payload)
            assert event.wait(timeout=2)
            assert tb._confirmation_results[confirm_id] is True
        finally:
            tb._pending_confirmations.pop(confirm_id, None)
            tb._confirmation_results.pop(confirm_id, None)

    def test_propose_edit_not_resolved_while_client_attached(self, harness):
        import kyrex.toolbox as tb
        client = harness.connect()
        harness._recv_lines(client, 1)  # consume marker

        edit_id = "edit-attached"
        event = threading.Event()
        tb._pending_edits[edit_id] = event
        try:
            harness.hub.observe_line(json.dumps({
                "type": "propose_edit", "editId": edit_id, "filePath": "/x",
            }))
            assert not event.wait(timeout=0.4)  # decision belongs to the UI
            line = harness._recv_lines(client, 1)[0]
            assert json.loads(line)["editId"] == edit_id
        finally:
            tb._pending_edits.pop(edit_id, None)
            tb._edit_results.pop(edit_id, None)
            client.close()


# ── replay tagging ─────────────────────────────────────────────────────────

class TestReplayTagging:
    def test_replayed_propose_edit_is_tagged(self, harness):
        client = harness.connect()

        harness.hub.observe_line(json.dumps({
            "type": "propose_edit", "editId": "stale", "filePath": "/x",
        }))

        lines = harness._recv_lines(client, 2)
        assert json.loads(lines[0])["type"] == "session_replay"
        tagged = json.loads(lines[1])
        assert tagged["type"] == "propose_edit"
        assert tagged["replay"] is True
        client.close()


# ── lifecycle ──────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_shutdown_message_stops_hub(self, harness):
        client = harness.connect()
        harness._recv_lines(client, 1)
        client.sendall(b'{"type": "shutdown"}\n')

        harness.thread.join(timeout=3)
        assert not harness.thread.is_alive()
        client.close()

    def test_idle_exit(self, tmp_path, monkeypatch):
        h = HubHarness(tmp_path, monkeypatch, idle_exit=0.05)
        h.hub.last_activity = time.monotonic() - 1.0  # long since quiet
        h.thread.join(timeout=3)
        assert not h.thread.is_alive()
        h.hub.cleanup()  # already stopped; free the port

    def test_idle_zero_never_exits(self, tmp_path, monkeypatch):
        h = HubHarness(tmp_path, monkeypatch, idle_exit=0.0)
        try:
            h.hub.last_activity = time.monotonic() - 999
            time.sleep(0.3)
            assert h.thread.is_alive()
        finally:
            h.close()

    def test_idle_env_parsing(self, monkeypatch):
        monkeypatch.setenv("KYREX_DAEMON_IDLE_EXIT", "30")
        assert idle_exit_seconds() == 30.0
        monkeypatch.setenv("KYREX_DAEMON_IDLE_EXIT", "bogus")
        assert idle_exit_seconds() == 900.0
        monkeypatch.delenv("KYREX_DAEMON_IDLE_EXIT", raising=False)
        assert idle_exit_seconds() == 900.0

    def test_is_daemon_mode(self, monkeypatch):
        monkeypatch.delenv("KYREX_DAEMON", raising=False)
        argv_backup = sys.argv
        try:
            sys.argv = ["core_bridge.py"]
            assert not is_daemon_mode()
            sys.argv = ["core_bridge.py", "--daemon"]
            assert is_daemon_mode()
            sys.argv = ["core_bridge.py"]
            monkeypatch.setenv("KYREX_DAEMON", "1")
            assert is_daemon_mode()
        finally:
            sys.argv = argv_backup
