"""chat_service.py — Kyrex Chat engine integration + persistence.

This is the standalone Chat product's backend core. It answers conversational
prompts by driving the *existing* Kyrex engine/provider layer directly, and
persists conversations as JSON files under the existing Cloud data root
(``kyrex-cloud/paths.py`` ``data_dir()`` — the same ``KYREX_DATA_DIR`` used by
bots/audit/MCP).

Isolation invariants (deliberately preserved):
  * No ~/.px/config.json global fallback. Provider/model/key resolution uses
    the exact environment keys the Cloud already uses for every other path
    (``KYREX_PROVIDER``, ``KYREX_MODEL``, ``KYREX_API_KEY``,
    ``OPENAI_BASE_URL`` / ``ANTHROPIC_BASE_URL``). There is no second
    ConfigManager and no silent global-config read.
  * Conversations are keyed per-user and stored under a per-user directory, so
    Chat can never see another bot/workspace/IDE session's state.
  * The default (no-workspace) turn is invoked with ``tools=None``: a pure
    conversational completion. This is a *chat*, not an agent task, so it does
    not (and must not) traverse the tier/policy/approval/audit gate that
    serves agent tasks.

Repo-aware mode (attached workspace):
  When a conversation has a workspace attached (a server-registered workspace
  id — never a client-supplied filesystem path), the turn is served by the
  REAL Kyrex engine: ``kyrex_engine/core_bridge.py`` is spawned as a
  subprocess with the workspace as its working directory, exactly like the
  VS Code extension / Tauri IDE / headless agent paths. The engine therefore
  self-configures (working directory + file tree in the system prompt,
  ToolBox tools, tool execution loop). The engine process runs strictly
  READ-ONLY: ``KYREX_READ_ONLY_REPO=1`` plus a ``KYREX_ALLOWED_TOOLS``
  allowlist exposing only inspection tools (read/list/search/knowledge);
  write, edit, delete, and command tools are neither advertised nor
  executable, and any ``propose_edit`` / ``confirm_request`` is answered
  with an explicit denial by this service. Agent-task gates (tier/policy/
  approval/audit) remain untouched — read-only inspection needs none.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue as _queue
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import AsyncIterator, Optional

# ── paths ──────────────────────────────────────────────────────────
# Resolve the shared data root exactly as the rest of Kyrex Cloud does.
SCRIPT_DIR = Path(__file__).resolve().parent            # web/backend/
KYREX_CLOUD_DIR = SCRIPT_DIR.parent.parent              # kyrex-cloud/
sys.path.insert(0, str(KYREX_CLOUD_DIR))

from paths import data_dir as _data_dir  # noqa: E402

# ── engine import ──────────────────────────────────────────────────
# The Kyrex engine lives in the sibling ``kyrex_engine/`` package. Import its
# provider factory and config manager so we reuse the real provider plumbing
# (retry/backoff, streaming callbacks) instead of re-implementing it.
ENGINE_DIR = KYREX_CLOUD_DIR.parent / "kyrex_engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from kyrex.providers import get_provider  # noqa: E402

# ── config ─────────────────────────────────────────────────────────
CHAT_DIR_NAME = "chat"
CHAT_SYSTEM_PROMPT = (
    "You are Kyrex Chat, the conversational assistant product from Kyrex. "
    "Answer clearly, directly, and in a natural conversational tone. "
    "Format responses with Markdown where it aids readability."
)

MAX_MESSAGE_CHARS = 32_000


class ChatUnavailable(Exception):
    """Raised when the engine/provider cannot be reached or is unconfigured."""


def _chat_root() -> Path:
    root = _data_dir() / CHAT_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _user_dir(user: str) -> Path:
    """Per-user subdirectory guarantees cross-user isolation."""
    if not user:
        raise ValueError("user is required")
    # Sanitize to a filesystem-safe segment.
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in user)
    d = _chat_root() / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _conv_path(user: str, conversation_id: str) -> Path:
    return _user_dir(user) / f"{conversation_id}.json"


# ── model / provider resolution (existing engine env keys) ────────

def _resolve_provider() -> dict:
    """Return {provider, model, api_key, base_url} from the existing env keys.

    Uses the same keys every other Cloud model call uses; no config-file
    fallback. ``provider``/``base_url`` are resolved the same way
    ``kyrex.providers.get_provider`` does.
    """
    provider = (os.environ.get("KYREX_PROVIDER") or os.environ.get("PROVIDER") or "openai").lower()
    model = os.environ.get("KYREX_MODEL") or ""
    api_key = os.environ.get("KYREX_API_KEY") or ""
    if provider == "anthropic":
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    else:
        base_url = os.environ.get("KYREX_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    return {"provider": provider, "model": model.strip(), "api_key": api_key.strip(), "base_url": base_url.strip()}


def engine_available() -> tuple[bool, str]:
    cfg = _resolve_provider()
    if not cfg["model"]:
        return False, "KYREX_MODEL is not configured"
    if not cfg["api_key"]:
        return False, "KYREX_API_KEY is not configured"
    return True, f"{cfg['provider']}/{cfg['model']}"


# ── workspace registry (server-controlled; never client-supplied paths) ──
# A browser request can only ever reference a workspace *id* from this
# registry. The registry itself is built exclusively from server-side
# environment configuration:
#
#   KYREX_CHAT_WORKSPACES  — JSON, either {"<id>": "<abs path>", ...} or
#                            [{"id": ..., "path": ..., "name": ...}, ...]
#   KYREX_CHAT_WORKSPACE   — single absolute path (registers id "default")
#
# Cloud deployments register server-side clones (same discipline as the
# repo executor); a local/desktop launcher may register a user-selected
# directory at process start. No API input can add, change, or bypass this
# registry, so a browser request can never select an arbitrary server path.

ENGINE_BRIDGE_PATH = ENGINE_DIR / "core_bridge.py"
# Read-only inspection tools exposed to the Chat engine process. task_complete
# is included because the engine's system prompt mandates it for turn ends.
CHAT_ENGINE_ALLOWED_TOOLS = (
    "read_local_file,list_local_files,search,query_memory,query_knowledge,"
    "task_complete"
)
ENGINE_HANDSHAKE_TIMEOUT = float(os.environ.get("KYREX_CHAT_ENGINE_START_TIMEOUT", "90"))
ENGINE_TURN_TIMEOUT = float(os.environ.get("KYREX_CHAT_ENGINE_TURN_TIMEOUT", "600"))
MAX_ENGINE_SESSIONS = int(os.environ.get("KYREX_CHAT_MAX_ENGINE_SESSIONS", "32"))


class EngineSessionError(Exception):
    """Raised when the engine bridge process cannot be used."""


import re as _re


def _workspaces_root():
    """Root dir for Chat-provisioned repo workspaces (server-owned)."""
    return _data_dir() / "chat-workspaces"


_SAFE_ID = _re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def provision_workspace(workspace_id: str, repo_url: str) -> dict:
    """Clone *repo_url* into a server-owned dir under _workspaces_root().

    The id must be a safe slug; the target path is server-generated (never
    client-supplied). Returns {"id", "path"} on success. Raises ValueError on
    a bad id and RuntimeError on clone failure. Caller is responsible for
    authorizing repo_url (allowlist / own-repo) before invoking this.
    """
    wid = str(workspace_id or "").strip()
    if not _SAFE_ID.match(wid):
        raise ValueError("workspace id must be a slug: a-z 0-9 dash, <=64 chars")
    root = _workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / wid
    if target.exists():
        raise ValueError(f"workspace '{wid}' already exists")
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", str(repo_url), str(target)],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("git clone timed out")
    if proc.returncode != 0:
        # Clean up a partial clone so a retry with the same id can succeed.
        try:
            import shutil
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
        except Exception:
            pass
        raise RuntimeError(f"git clone failed: {proc.stderr.strip()[:300]}")
    return {"id": wid, "path": str(target)}


def _workspace_registry() -> dict:
    """Parse the server-side workspace registry from environment config."""
    entries: dict[str, dict] = {}
    raw = (os.environ.get("KYREX_CHAT_WORKSPACES") or "").strip()
    if raw:
        doc = None
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            doc = None
        items: list = []
        if isinstance(doc, dict):
            items = [{"id": k, "path": v} for k, v in doc.items() if isinstance(v, str)]
        elif isinstance(doc, list):
            items = [i for i in doc if isinstance(i, dict)]
        for it in items:
            wid = str(it.get("id") or "").strip()
            wpath = str(it.get("path") or "").strip()
            if wid and wpath:
                entries[wid] = {"path": wpath, "name": str(it.get("name") or wid)}
    single = (os.environ.get("KYREX_CHAT_WORKSPACE") or "").strip()
    if single and "default" not in entries:
        entries["default"] = {"path": single, "name": "default"}
    # Auto-discover Chat-provisioned workspaces (dirs under chat-workspaces/).
    # These need no env var: provisioning creates the dir, discovery registers it.
    try:
        root = _workspaces_root()
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and child.name not in entries:
                    entries[child.name] = {"path": str(child), "name": child.name}
    except OSError:
        pass
    return entries


def _resolve_workspace_entry(entry: dict):
    """Validate one registry entry. Returns (ok, resolved_path_or_None).

    Fail-closed: the path must be absolute, must resolve, and must be an
    existing directory. Relative or non-existent paths are unavailable.
    """
    raw = str(entry.get("path") or "").strip()
    p = Path(raw)
    if not p.is_absolute():
        return False, None
    try:
        resolved = p.resolve()
    except OSError:
        return False, None
    if not resolved.is_dir():
        return False, None
    return True, resolved


def list_workspaces() -> list[dict]:
    """Registry entries for the UI. Paths are intentionally NOT exposed."""
    out = []
    for wid, entry in sorted(_workspace_registry().items()):
        ok, _ = _resolve_workspace_entry(entry)
        out.append({"id": wid, "name": entry["name"], "available": ok})
    return out


def resolve_workspace(workspace_id):
    """Resolve a registry id to an existing directory, or None (fail-closed)."""
    entry = _workspace_registry().get(str(workspace_id or "").strip())
    if entry is None:
        return None
    ok, resolved = _resolve_workspace_entry(entry)
    return resolved if ok else None


# ── engine bridge session (one core_bridge.py process per conversation) ──

class EngineSession:
    """Client for one spawned ``core_bridge.py`` engine process.

    Speaks the engine's existing NDJSON stdio protocol — the same protocol
    the Go TUI, the VS Code extension, the Tauri IDE, and headless_agent.py
    already speak. The engine is spawned with the workspace as its working
    directory (matching those surfaces), so it self-configures: the bootstrap
    system prompt carries the working directory and file tree, and the
    ToolBox/MCP tooling runs inside the engine's own loop.

    Read-only enforcement for Kyrex Chat:
      * env ``KYREX_READ_ONLY_REPO=1``  — toolbox refuses writes/edits and
        network-write git even if a write tool were somehow invoked;
      * env ``KYREX_ALLOWED_TOOLS``     — only inspection tools are advertised
        in the schema AND executable in the engine's dispatch loop;
      * this client answers every ``propose_edit`` / ``confirm_request``
        with an explicit denial (defense in depth; with the allowlist above
        the engine can never emit one in the first place).
    """

    def __init__(self, workspace_path: Path, provider_cfg: dict):
        self.workspace = Path(workspace_path)
        self.denied_requests: list[dict] = []
        self.session_state: Optional[dict] = None
        self._closed = False

        env = os.environ.copy()
        env["KYREX_SURFACE"] = "Kyrex Chat"
        env["KYREX_READ_ONLY_REPO"] = "1"
        env["KYREX_ALLOWED_TOOLS"] = CHAT_ENGINE_ALLOWED_TOOLS
        # KYREX_VSCODE=1 is the embedding-surface handshake: without it (and
        # without a config file) core_bridge.py prints the setup wizard and
        # exits before the NDJSON session starts. The VS Code extension, the
        # Tauri bridge, and headless_agent.py all set it for the same reason.
        # It routes hypothetical edit gates through propose_edit messages —
        # which this client always DENIES — and read-only is enforced
        # independently by KYREX_READ_ONLY_REPO and the tool allowlist.
        env["KYREX_VSCODE"] = "1"
        # The engine's workspace root is THIS workspace (its cwd). An inherited
        # WORKSPACE_ROOT / PROJECT_SOURCE_ROOT (e.g. a chat backend running
        # inside an agent sandbox) would make the toolbox validate and rebase
        # paths against a foreign root — deny reads and misbind writes. The
        # engine derives both from its cwd, so they must not be inherited.
        env.pop("WORKSPACE_ROOT", None)
        env.pop("PROJECT_SOURCE_ROOT", None)
        # Provider config comes from the same env keys the chat service uses
        # (ConfigManager consults KYREX_* env before any config file).
        env["KYREX_PROVIDER"] = provider_cfg["provider"]
        env["KYREX_MODEL"] = provider_cfg["model"]
        env["KYREX_API_KEY"] = provider_cfg["api_key"]
        if provider_cfg["provider"] == "anthropic":
            if provider_cfg["base_url"]:
                env["ANTHROPIC_BASE_URL"] = provider_cfg["base_url"]
        else:
            if provider_cfg["base_url"]:
                env["KYREX_BASE_URL"] = provider_cfg["base_url"]
                env["OPENAI_BASE_URL"] = provider_cfg["base_url"]

        self._proc = subprocess.Popen(
            [sys.executable, str(ENGINE_BRIDGE_PATH)],
            cwd=str(self.workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._frames: _queue.Queue = _queue.Queue()
        self._stdin_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        # Ring buffer of the engine's last stderr lines — the only place the
        # engine reports startup failures (tracebacks go to stderr), so the
        # session error surfaces them instead of a bare "stdout closed".
        self.stderr_tail: list[str] = []
        self._stderr_lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True,
                         name=f"kyrex-chat-engine-reader-{id(self):x}").start()
        # Drain stderr so the pipe never fills and blocks the engine.
        threading.Thread(target=self._drain_stderr, daemon=True,
                         name=f"kyrex-chat-engine-stderr-{id(self):x}").start()
        self._wait_handshake()

    def _stderr_snapshot(self) -> str:
        with self._stderr_lock:
            return "\n".join(self.stderr_tail[-15:])

    # ── process plumbing ──────────────────────────────────────────

    def _read_stdout(self):
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._frames.put(frame)
        except Exception:
            pass
        finally:
            self._frames.put(None)

    def _drain_stderr(self):
        try:
            for line in self._proc.stderr:
                with self._stderr_lock:
                    self.stderr_tail.append(line.rstrip("\n"))
                    if len(self.stderr_tail) > 100:
                        del self.stderr_tail[:-100]
        except Exception:
            pass

    def _send(self, payload: dict) -> None:
        if self._closed or self._proc.poll() is not None:
            raise EngineSessionError("engine process is not running")
        try:
            with self._stdin_lock:
                self._proc.stdin.write(json.dumps(payload) + "\n")
                self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise EngineSessionError(f"engine stdin closed: {exc}")

    def _wait_handshake(self) -> None:
        deadline = time.monotonic() + ENGINE_HANDSHAKE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                frame = self._frames.get(timeout=0.25)
            except _queue.Empty:
                if self._proc.poll() is not None:
                    raise EngineSessionError(
                        f"engine exited during startup (exit code {self._proc.returncode})"
                        f"\nengine stderr:\n{self._stderr_snapshot()}")
                continue
            if frame is None:
                raise EngineSessionError(
                    "engine stdout closed during startup"
                    f"\nengine stderr:\n{self._stderr_snapshot()}")
            t = frame.get("type")
            if t == "session_state" and self.session_state is None:
                self.session_state = frame
            elif t == "phase" and frame.get("value") == "IDLE":
                return
        self.close()
        raise EngineSessionError(
            f"engine handshake timed out after {int(ENGINE_HANDSHAKE_TIMEOUT)}s")

    # ── turns ─────────────────────────────────────────────────────

    def run_turn(self, text: str, on_token, cancel_check=None) -> tuple[str, Optional[str]]:
        """Run one engine chat turn to completion. Blocking.

        ``on_token(chunk)`` receives streamed content chunks;
        ``cancel_check()`` is polled and, when true, an interrupt is sent to
        the engine (which cancels the active turn — the bridge then emits its
        chat_done + IDLE frames and this method returns promptly).

        Returns ``(final_content, error_message_or_None)``.
        """
        if self._closed:
            raise EngineSessionError("engine session is closed")
        if not self._turn_lock.acquire(blocking=False):
            raise EngineSessionError("engine is busy with another turn")
        try:
            self._send({"type": "chat", "content": text})
            deadline = time.monotonic() + ENGINE_TURN_TIMEOUT
            final: Optional[str] = None
            error: Optional[str] = None
            saw_done = False
            while True:
                if cancel_check is not None and cancel_check():
                    self.interrupt()
                try:
                    frame = self._frames.get(timeout=0.1)
                except _queue.Empty:
                    if time.monotonic() > deadline:
                        raise EngineSessionError(
                            f"engine turn timed out after {int(ENGINE_TURN_TIMEOUT)}s")
                    continue
                if frame is None:
                    raise EngineSessionError("engine process terminated mid-turn")
                t = frame.get("type")
                if t == "token":
                    chunk = frame.get("content")
                    if chunk:
                        on_token(chunk)
                elif t == "propose_edit":
                    # Read-only chat: deny every edit proposal explicitly.
                    self.denied_requests.append(
                        {"kind": "edit", "path": frame.get("filePath")})
                    self._send({"type": "edit_decision",
                                "editId": frame.get("editId"), "accepted": False})
                elif t == "confirm_request":
                    # Read-only chat: deny every confirmation gate explicitly.
                    self.denied_requests.append(
                        {"kind": str(frame.get("value") or "confirm"),
                         "path": frame.get("path")})
                    self._send({"type": "confirm_response",
                                "id": frame.get("id"), "approved": False})
                elif t == "error":
                    msg = frame.get("content") or frame.get("message") or "engine error"
                    if error is None:
                        error = msg
                elif t == "chat_done":
                    final = frame.get("content") or ""
                    saw_done = True
                elif t == "phase" and frame.get("value") == "IDLE" and saw_done:
                    break
                # reasoning / tool_start / tool_result / diff / tui_pause /
                # final_round_* frames are engine telemetry: intentionally not
                # forwarded (the public SSE contract is unchanged).
            if error is None and final and _provider_error_content(final):
                # The engine returns provider failures as error-prefixed
                # content (same shape the pure-chat path detects).
                error = final
            return final or "", error
        finally:
            self._turn_lock.release()

    def interrupt(self) -> None:
        try:
            self._send({"type": "interrupt"})
        except EngineSessionError:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._proc.terminate()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


# Live engine sessions, keyed by (user, conversation_id). LRU-capped so a
# long-lived server cannot accumulate engine processes for stale chats.
_engine_sessions: "OrderedDict[tuple[str, str], EngineSession]" = OrderedDict()


def _get_engine_session(user: str, conversation_id: str,
                        workspace_path: Path) -> EngineSession:
    key = (user, conversation_id)
    sess = _engine_sessions.get(key)
    if sess is not None:
        alive = (not sess._closed) and sess._proc.poll() is None
        same_ws = sess.workspace == workspace_path
        if alive and same_ws:
            _engine_sessions.move_to_end(key)
            return sess
        sess.close()
        _engine_sessions.pop(key, None)
    cfg = _resolve_provider()
    sess = EngineSession(workspace_path, cfg)
    _engine_sessions[key] = sess
    while len(_engine_sessions) > MAX_ENGINE_SESSIONS:
        _, oldest = _engine_sessions.popitem(last=False)
        oldest.close()
    return sess


def close_engine_session(user: str, conversation_id: str) -> None:
    sess = _engine_sessions.pop((user, conversation_id), None)
    if sess is not None:
        sess.close()


def close_all_engine_sessions() -> None:
    while _engine_sessions:
        _, sess = _engine_sessions.popitem()
        sess.close()


# ── persistence ────────────────────────────────────────────────────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create_conversation(user: str, title: str = "New chat") -> dict:
    now = _now_iso()
    conv = {
        "conversation_id": uuid.uuid4().hex,
        "title": title or "New chat",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    _write(user, conv)
    return conv


def _write(user: str, conv: dict) -> None:
    conv["updated_at"] = _now_iso()
    path = _conv_path(user, conv["conversation_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(conv, indent=2))
    tmp.replace(path)


def get_conversation(user: str, conversation_id: str) -> Optional[dict]:
    path = _conv_path(user, conversation_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data


def list_conversations(user: str) -> list[dict]:
    d = _user_dir(user)
    out = []
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "conversation_id": data.get("conversation_id", p.stem),
            "title": data.get("title", "New chat"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "message_count": len(data.get("messages", [])),
            "workspace_id": data.get("workspace_id"),
        })
    return out


def delete_conversation(user: str, conversation_id: str) -> bool:
    path = _conv_path(user, conversation_id)
    if not path.exists():
        return False
    # Tear down any live engine process bound to this conversation.
    close_engine_session(user, conversation_id)
    path.unlink()
    return True


def _append_message(user: str, conv: dict, role: str, content: str) -> dict:
    msg = {
        "id": uuid.uuid4().hex,
        "role": role,
        "content": content,
        "created_at": _now_iso(),
    }
    conv["messages"].append(msg)
    return msg


def _title_from(user_message: str) -> str:
    t = " ".join(user_message.split())
    return t[:40] + ("..." if len(t) > 40 else "") or "New chat"


# ── engine invocation (streaming) ──────────────────────────────────

def build_messages(history: list[dict], user_content: str) -> list[dict]:
    """Assemble the provider message list, mirroring the existing chat path:
    a leading system prompt, prior turns, then the new user turn."""
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    for m in history or []:
        role = m.get("role")
        if role not in ("user", "assistant", "system"):
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_content})
    return messages


# Sentinel payload markers placed on the worker->loop queue so the streaming
# coroutine can distinguish terminal outcomes (completion / provider error)
# from incrementally streamed token deltas.
_SENTINEL = object()
_ERROR = object()
_CANCELLED = object()


def _provider_error_content(content: str) -> bool:
    """Detect the provider's swallowed-error shape.

    Both OpenAIProvider and AnthropicProvider catch every exception inside
    their streaming path and return ``content`` prefixed with
    ``[<name> Provider Error: ...`` instead of raising. This is the only signal
    the Chat service receives for a mid-stream or pre-first-token failure, so
    we detect it here and convert it into a deterministic error event rather
    than persisting it as a successful assistant message.
    """
    if not content:
        return False
    low = content.lower()
    return ("provider error:" in low) or (content.lstrip().startswith("[") and "error:" in low)


# Sentinel: the request did not express a workspace → use the conversation's
# stored binding (or none). An explicit "" detaches the workspace.
_WORKSPACE_UNSET = object()


async def stream_chat(
    user: str,
    conversation_id: str,
    user_content: str,
    cancel_event: Optional[asyncio.Event] = None,
    workspace_id=_WORKSPACE_UNSET,
) -> AsyncIterator[dict]:
    """Stream assistant output for one user turn.

    Yields control frames (``dict``) rather than raw strings:
      * ``{"type": "conversation", "conversation_id": ...}`` — once, first.
      * ``{"type": "delta", "content": <token>}`` — each incremental token.
    Termination is *not* yielded from here; the terminal frame is produced by
    the caller after this generator returns a ``(status, final_text)`` pair:

      ``status`` is one of ``"complete"``, ``"error"``, ``"cancelled"``.

    The provider's ``stream_callback`` is bridged to the event loop via a
    stdlib ``queue.Queue`` (thread-safe) — never ``asyncio.Queue`` across
    threads — drained by this coroutine. The worker thread is always joined
    before returning so no orphaned provider call outlives the request.
    """
    ok, detail = engine_available()
    if not ok:
        raise ChatUnavailable(detail)

    conv = get_conversation(user, conversation_id)
    if conv is None:
        conv = create_conversation(user, title=_title_from(user_content))
        conversation_id = conv["conversation_id"]

    # ── workspace resolution ──────────────────────────────────────────
    # Absent on the request → use the conversation's stored binding (or none).
    # Explicit "" / None → detach: pure conversation, stored key removed.
    # The id is always matched against the server-side registry — a browser
    # request can never name a filesystem path directly.
    if workspace_id is _WORKSPACE_UNSET:
        requested_ws = conv.get("workspace_id") or None
    else:
        requested_ws = str(workspace_id or "").strip() or None
    resolved_ws = None
    if requested_ws:
        resolved_ws = resolve_workspace(requested_ws)
        if resolved_ws is None:
            raise ChatUnavailable(
                f"workspace '{requested_ws}' is not registered or is unavailable")

    history = conv.get("messages", [])
    if resolved_ws is None:
        messages = build_messages(history, user_content)

    # Persist the workspace binding (only when this request expressed one),
    # then the user message up-front so a concurrent read sees the turn.
    if workspace_id is not _WORKSPACE_UNSET:
        if requested_ws:
            conv["workspace_id"] = requested_ws
        else:
            conv.pop("workspace_id", None)
    _append_message(user, conv, "user", user_content)
    _write(user, conv)

    engine_session: Optional[EngineSession] = None
    if resolved_ws is not None:
        # ── repo-aware turn: the REAL Kyrex engine (core_bridge.py) ─────
        # Spawned with the workspace as its working directory, exactly like
        # the VS Code / Tauri IDE / headless-agent surfaces. Read-only is
        # enforced inside the engine process (see EngineSession).
        try:
            engine_session = _get_engine_session(user, conversation_id, resolved_ws)
        except EngineSessionError as exc:
            raise ChatUnavailable(f"engine session failed: {exc}")

        def _run_blocking() -> None:
            outcome = _SENTINEL
            try:
                final, err = engine_session.run_turn(
                    user_content, _on_token, cancel_check=cancel.is_set)
                engine_final[0] = final
                if err:
                    outcome = _ERROR
                    q.put({"__error__": err})
                else:
                    outcome = _SENTINEL
            except EngineSessionError as exc:
                outcome = _ERROR
                q.put({"__error__": str(exc)})
            finally:
                q.put({"__outcome__": outcome})
    else:
        cfg = _resolve_provider()
        provider = get_provider(cfg["provider"], cfg["api_key"], base_url=cfg["base_url"] or None)

        def _run_blocking() -> None:
            outcome = _SENTINEL  # default: clean completion (no error)
            try:
                # The provider's chat() is async; run it in this thread's own loop.
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    interrupt = asyncio.Event()
                    interrupt_handle[0] = (loop, interrupt)
                    result = loop.run_until_complete(provider.chat(
                        model=cfg["model"],
                        messages=messages,
                        tools=None,
                        stream_callback=_on_token,
                        interrupt_event=interrupt,
                    ))
                    # The provider swallows exceptions into error-prefixed content.
                    content = (result or {}).get("content") or ""
                    if _provider_error_content(content):
                        outcome = _ERROR
                        q.put({"__error__": content})
                    else:
                        outcome = _SENTINEL
                finally:
                    interrupt_handle[0] = None
                    loop.close()
            except Exception as exc:  # an exception that escaped the provider layer
                outcome = _ERROR
                q.put({"__error__": str(exc)})
            finally:
                # Always signal termination exactly once, carrying the outcome so
                # the drainer knows whether this was a clean finish or a failure.
                q.put({"__outcome__": outcome})

    # Thread-safe bridge: the blocking engine/provider call runs in a worker
    # thread. We use a stdlib queue.Queue (thread-safe, no cross-thread
    # asyncio.Queue) drained by this coroutine via asyncio.to_thread.
    q: _queue.Queue = _queue.Queue()
    cancel = cancel_event if cancel_event is not None else asyncio.Event()

    # Provider-facing interrupt event (pure-chat path only; created in the
    # worker loop, set from the event-loop side). A list-of-one is used as a
    # mutable handle into the worker thread since it must be created inside
    # that same loop.
    interrupt_handle: list = [None]
    # Repo-aware path: the engine's authoritative chat_done content.
    engine_final: list = [None]

    def _on_token(text: str) -> None:
        if text:
            q.put(text)

    worker = threading.Thread(target=_run_blocking, daemon=True,
                              name=f"chat-{conversation_id[:12]}")
    worker.start()

    def _request_cancel() -> None:
        """Cooperatively stop the in-flight generation.

        Repo-aware: sends the engine's interrupt control message; the bridge
        cancels the active turn and emits chat_done + phase IDLE promptly.
        Pure chat: sets the provider-facing asyncio.Event from the calling
        thread; the provider checks it per chunk and stops producing tokens.
        Neither can abort an in-flight HTTP request mid-flight, but both stop
        token delivery immediately and let the worker unwind.
        """
        if engine_session is not None:
            engine_session.interrupt()
            return
        h = interrupt_handle[0]
        if h is not None:
            loop, interrupt = h
            loop.call_soon_threadsafe(interrupt.set)

    full: list[str] = []
    result = None
    first = True
    outcome = _SENTINEL
    # Poll cadence for observing cancellation while the provider is quiet
    # between tokens. Kept short so a cancel or client disconnect is noticed
    # promptly even if the provider stalls mid-stream.
    POLL_SECONDS = 0.05
    try:
        while True:
            # Wait briefly for the next frame WITHOUT blocking the event loop:
            # q.get() is a blocking stdlib call, so it must never run directly
            # on the loop thread. Bridge it through asyncio.to_thread (worker
            # thread) and await the result — the loop stays fully responsive
            # during generation. The short timeout still lets us observe
            # cancellation during provider silence without busy-spinning.
            try:
                token = await asyncio.to_thread(q.get, True, POLL_SECONDS)
            except _queue.Empty:
                if cancel.is_set():
                    outcome = _CANCELLED
                    _request_cancel()
                    break
                continue
            if isinstance(token, dict) and "__outcome__" in token:
                outcome = token["__outcome__"]
                break
            if isinstance(token, dict) and "__error__" in token:
                outcome = _ERROR
                result = token["__error__"]
                break
            if cancel.is_set():
                outcome = _CANCELLED
                _request_cancel()
                break
            full.append(token)
            if first:
                yield {"type": "conversation", "conversation_id": conversation_id}
                first = False
            yield {"type": "delta", "content": token}
    finally:
        # A cancelled or disconnected stream must not leave the worker running.
        if cancel.is_set():
            _request_cancel()
        # Joining the worker blocks; never do it on the event-loop thread, or
        # provider unwind time (up to the 30s cap) starves every other request.
        # Off-load to a worker thread instead. If finalization happens without
        # a running loop (post-close GC), fall back to a direct join — there is
        # no loop left to starve in that case.
        try:
            await asyncio.to_thread(worker.join, 30.0)
        except RuntimeError:
            worker.join(timeout=30.0)
        # Drain any residual frames the worker may have enqueued so the queue
        # and worker's producer never deadlock on a full/non-consumed queue.
        # A terminal outcome decided before this point (cancel/error) is
        # authoritative and must not be overwritten by a late clean completion.
        while True:
            try:
                leftover = q.get_nowait()
            except _queue.Empty:
                break
            if isinstance(leftover, dict) and "__outcome__" in leftover:
                if outcome in (_SENTINEL,):
                    outcome = leftover["__outcome__"]
            elif isinstance(leftover, dict) and "__error__" in leftover:
                result = leftover["__error__"]
                if outcome not in (_CANCELLED,):
                    outcome = _ERROR

    final_text = "".join(full).strip()

    # Repo-aware turns: the engine's chat_done content is the authoritative
    # final text (the same contract that makes done.content replace the
    # client's accumulated deltas). Cancelled turns keep the streamed partial.
    if engine_session is not None and outcome is _SENTINEL and engine_final[0]:
        authoritative = str(engine_final[0]).strip()
        if authoritative:
            final_text = authoritative

    # Persistence: only a successfully-completed turn persists an assistant
    # message. Failed and cancelled streams are never recorded as a completed
    # assistant reply (no duplicate/false assistant messages).
    if outcome is _SENTINEL and final_text:
        conv_now = get_conversation(user, conversation_id) or conv
        _append_message(user, conv_now, "assistant", final_text)
        _write(user, conv_now)

    # Terminal status frame. An async generator cannot ``return`` a value, so
    # the terminal outcome is yielded as the final control frame, which the
    # caller (_drive_stream) maps to the matching explicit SSE event.
    if outcome is _ERROR:
        yield {"type": "status", "status": "error",
               "message": result or "provider error"}
    elif outcome is _CANCELLED:
        yield {"type": "status", "status": "cancelled", "content": final_text}
    else:
        yield {"type": "status", "status": "complete", "content": final_text}
