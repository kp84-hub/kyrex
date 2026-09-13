"""
daemon_bridge — keeps the Kyrex engine alive when the UI closes.

Problem this solves
-------------------
The IDE (and TUI) run the engine as a child process wired over stdio. When
the app closes, the pipes break and the engine dies with it — killing any
in-flight turn. This module gives the engine a daemon mode:

- It listens on a localhost TCP socket instead of stdio. Stdout is tee'd
  through a ring buffer so a UI that reconnects later can replay everything
  it missed.
- A small control file (~/.kyrex/daemons/{workspace-key}.json) records
  {pid, port} so the IDE can discover and reattach to a live engine after a
  restart instead of spawning a second one.
- While no UI is attached, edit-approval gates (propose_edit and
  confirm_request with value "edit") are auto-approved so background work
  keeps flowing, matching race-mode precedent. Deletion gates are auto-DENIED
  — nothing destructive should run unattended.
- If no UI reattaches and no turn is running for KYREX_DAEMON_IDLE_EXIT
  seconds (default 900), the daemon saves the session and exits so it never
  becomes a zombie.

The workspace key is FNV-1a 64 of the normalized workspace path (separators
normalized to "/", trailing slashes trimmed) and must match the Rust side in
kyrex-ide/src-tauri/src/daemon.rs exactly.
"""

import io
import json
import os
import socket
import sys
import threading
import time
from collections import deque
from pathlib import Path

try:
    from kyrex import toolbox as _toolbox
except ImportError:  # pragma: no cover - allows isolated testing
    _toolbox = None

DEFAULT_IDLE_EXIT_SECONDS = 900.0
REPLAY_LIMIT = 800


# ── Workspace key (must mirror kyrex-ide/src-tauri/src/daemon.rs) ──────────

def _fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def normalize_workspace(workspace: str) -> str:
    """Canonical form used for the daemon control-file key."""
    p = (workspace or "").replace("\\", "/")
    return p.rstrip("/") or "/"


def daemon_key(workspace: str) -> str:
    return f"{_fnv1a64(normalize_workspace(workspace).encode('utf-8')):016x}"


def control_file_path(workspace: str) -> Path:
    home = Path(os.environ.get("KYREX_HOME", str(Path.home())))
    return home / ".kyrex" / "daemons" / f"{daemon_key(workspace)}.json"


def write_control_file(workspace: str, port: int) -> Path:
    path = control_file_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": os.getpid(),
        "port": port,
        "workspace": normalize_workspace(workspace),
        "started": time.time(),
    }))
    return path


def read_control_file(workspace: str):
    """Return the recorded {pid, port} for this workspace, or None."""
    path = control_file_path(workspace)
    try:
        data = json.loads(path.read_text())
        return {"pid": int(data["pid"]), "port": int(data["port"])}
    except Exception:
        return None


def remove_control_file(workspace: str):
    try:
        control_file_path(workspace).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


# ── Stdout tee: pass everything through, capture complete lines ────────────

class TeeStdout:
    """Wraps sys.stdout: every byte still reaches the original stream (which
    the spawner redirects to a log file), and every complete JSON line is
    handed to the DaemonHub for replay buffering, broadcasting, and
    background approval resolution."""

    def __init__(self, original, hub):
        self._original = original
        self._hub = hub
        self._buf = ""

    def write(self, s):
        self._original.write(s)
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._hub.observe_line(line)
        return len(s)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self._original, name)


# ── The daemon hub ─────────────────────────────────────────────────────────

class DaemonHub:
    """TCP host for the engine: one UI client at a time, replay buffer for
    reconnecting UIs, background approval resolution while detached, and
    idle-exit watchdog."""

    def __init__(self, workspace: str, idle_exit: float = DEFAULT_IDLE_EXIT_SECONDS,
                 replay_limit: int = REPLAY_LIMIT):
        self.workspace = normalize_workspace(workspace)
        self.idle_exit = max(0.0, float(idle_exit))
        self.replay_buffer = deque(maxlen=replay_limit)
        self.clients = []
        self.clients_lock = threading.Lock()
        # Guards buffer-append + broadcast vs replay-snapshot + client-add,
        # so no line is lost between replay and live streaming.
        self.stream_lock = threading.Lock()
        self.turn_active = False
        self.last_activity = time.monotonic()
        self._stop = threading.Event()
        self.server_sock = None
        self.port = None
        self.loop = None          # asyncio loop, injected by run_daemon
        self.queue = None         # asyncio.Queue, injected by run_daemon
        self.on_line = None       # callable(line) — set by core_bridge
        self.branch_provider = None  # callable() -> current branch name
        self.pid = os.getpid()

    # ── lifecycle ──

    def bind(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(4)
        self.port = s.getsockname()[1]
        self.server_sock = s
        write_control_file(self.workspace, self.port)
        return self.port

    def attach(self, queue, loop):
        """Wire the hub into core_bridge's asyncio queue loop."""
        self.queue = queue
        self.loop = loop

    def request_stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        with self.clients_lock:
            for c in self.clients:
                try:
                    c.shutdown(socket.SHUT_RDWR)
                    c.close()
                except Exception:
                    pass
            self.clients = []
        self._push_sentinel()

    def _push_sentinel(self):
        if self.loop is not None and self.queue is not None:
            try:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, None)
            except Exception:
                pass

    def cleanup(self):
        remove_control_file(self.workspace)
        self.request_stop()
        if self.server_sock is not None:
            try:
                self.server_sock.close()
            except Exception:
                pass

    # ── accept loop (runs in its own thread) ──

    def serve(self):
        while not self._stop.is_set():
            self.server_sock.settimeout(0.5)
            try:
                conn, _ = self.server_sock.accept()
            except socket.timeout:
                if self._idle_expired():
                    self.request_stop()
                    break
                continue
            except OSError:
                break
            self._replace_with_new_client(conn)
        self.cleanup()

    def _idle_expired(self):
        if self.idle_exit <= 0:
            return False
        with self.clients_lock:
            has_client = bool(self.clients)
        return not has_client and not self.turn_active and \
            (time.monotonic() - self.last_activity) > self.idle_exit

    def _replace_with_new_client(self, conn):
        # Single active client policy: the newest UI wins.
        with self.clients_lock:
            for old in self.clients:
                try:
                    old.shutdown(socket.SHUT_RDWR)
                    old.close()
                except Exception:
                    pass
            self.clients = []
        thread = threading.Thread(target=self._client_thread, args=(conn,), daemon=True)
        thread.start()

    # ── per-client thread ──

    def _client_thread(self, conn):
        conn.settimeout(None)
        # _send_replay registers the client under stream_lock, so no line can
        # slip between the replay snapshot and live streaming.
        try:
            self._send_replay(conn)
        except Exception:
            with self.clients_lock:
                if conn in self.clients:
                    self.clients.remove(conn)
            try:
                conn.close()
            except Exception:
                pass
            return
        reader = conn.makefile("r", encoding="utf-8", errors="replace")
        try:
            while not self._stop.is_set():
                line = reader.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                self.last_activity = time.monotonic()
                if self._handle_control_line(line):
                    continue
                if self.on_line is not None:
                    self.on_line(line)
        except Exception:
            pass
        finally:
            with self.clients_lock:
                if conn in self.clients:
                    self.clients.remove(conn)
            try:
                reader.close()
            except Exception:
                pass
            self.last_activity = time.monotonic()

    def _handle_control_line(self, line) -> bool:
        """Shutdown is handled hub-side; everything else is the engine's."""
        try:
            payload = json.loads(line)
            if isinstance(payload, dict) and payload.get("type") == "shutdown":
                self.request_stop()
                return True
        except Exception:
            pass
        return False

    def _send_replay(self, conn):
        with self.stream_lock:
            marker = {
                "type": "session_replay",
                "count": len(self.replay_buffer),
                "branch": self.branch_provider() if self.branch_provider else None,
                "pid": self.pid,
            }
            conn.sendall((json.dumps(marker) + "\n").encode("utf-8"))
            for raw in self.replay_buffer:
                conn.sendall((self._mark_replay(raw) + "\n").encode("utf-8"))
            with self.clients_lock:
                self.clients.append(conn)

    @staticmethod
    def _mark_replay(raw: str) -> str:
        """Tag replayed lines so the UI can treat them as history, not live
        prompts (e.g. stale propose_edit requests must not reopen modals)."""
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                obj["replay"] = True
                return json.dumps(obj)
        except Exception:
            pass
        return raw

    # ── output capture path ──

    def observe_line(self, line: str):
        self.last_activity = time.monotonic()
        with self.stream_lock:
            self.replay_buffer.append(line)
            self._resolve_background_approval(line)
            self._broadcast(line)

    def _resolve_background_approval(self, line: str):
        """While no UI is attached, keep work flowing: approve edit gates,
        deny deletion gates. Mirrors the stdin_thread interception contract
        in core_bridge (set result, then release the waiting event)."""
        if _toolbox is None or self.clients:
            return
        try:
            payload = json.loads(line)
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        m_type = payload.get("type")
        if payload.get("replay"):
            return

        if m_type in ("propose_edit", "confirm_request"):
            if m_type == "propose_edit":
                edit_id = payload.get("editId", "")
                note = {"type": "system",
                        "content": f"[*] Background: auto-approved edit for {payload.get('filePath', '?')} (no UI attached)"}
                self.replay_buffer.append(json.dumps(note))
                self._broadcast(json.dumps(note))
                if _toolbox is not None and edit_id:
                    _toolbox._edit_results[edit_id] = True
                    ev = _toolbox._pending_edits.get(edit_id)
                    if ev is not None:
                        ev.set()
                return
            # confirm_request: only the diff gate is safe to auto-approve
            if payload.get("value") == "edit":
                confirm_id = payload.get("id", "")
                note = {"type": "system",
                        "content": f"[*] Background: auto-approved edit gate for {payload.get('path', '?')} (no UI attached)"}
                self.replay_buffer.append(json.dumps(note))
                self._broadcast(json.dumps(note))
                if _toolbox is not None and confirm_id:
                    _toolbox._confirmation_results[confirm_id] = True
                    ev = _toolbox._pending_confirmations.get(confirm_id)
                    if ev is not None:
                        ev.set()
            elif payload.get("value") == "deletion":
                confirm_id = payload.get("id", "")
                note = {"type": "system",
                        "content": f"[*] Background: DENIED deletion request for {payload.get('path', '?')} (no UI attached to approve it)"}
                self.replay_buffer.append(json.dumps(note))
                self._broadcast(json.dumps(note))
                if _toolbox is not None and confirm_id:
                    _toolbox._confirmation_results[confirm_id] = False
                    ev = _toolbox._pending_confirmations.get(confirm_id)
                    if ev is not None:
                        ev.set()

    def _broadcast(self, line: str):
        if not self.clients:
            return
        data = (line + "\n").encode("utf-8")
        dead = []
        for conn in self.clients:
            try:
                conn.sendall(data)
            except Exception:
                dead.append(conn)
        if dead:
            with self.clients_lock:
                for conn in dead:
                    if conn in self.clients:
                        self.clients.remove(conn)
        if not dead:
            self.last_activity = time.monotonic()


def is_daemon_mode() -> bool:
    return os.environ.get("KYREX_DAEMON") == "1" or "--daemon" in sys.argv


def idle_exit_seconds() -> float:
    raw = os.environ.get("KYREX_DAEMON_IDLE_EXIT", "")
    try:
        return float(raw) if raw else DEFAULT_IDLE_EXIT_SECONDS
    except ValueError:
        return DEFAULT_IDLE_EXIT_SECONDS
