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

Bot-bound conversations (Bot policy, slice 3):
  The tool allowlist for a Bot-bound turn is DERIVED from the Bot's policy
  (bot_capabilities.derive_bot_capabilities) using the existing Cloud policy
  engine and host tier table — exact > prefix > ``*``, no match denies, a
  deny denies, and the host tier can never be lowered. The derived set is
  always a SUBSET of the host base, so a Bot policy may only restrict what
  the Bot can do inside the still-read-only engine session. Approval-required
  operations stay unavailable (no approval UX in Chat; nothing is auto-
  approved). The engine session is re-spawned when a Bot's policy changes —
  never reused with stale permissions. Conversations with ``bot_id = null``
  keep the exact pre-Bots allowlist behavior.
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
import bots  # noqa: E402  — the single, authoritative Bot registry.
# Bot policy -> engine capability translation (reuses the Cloud policy engine
# and host tier table; never a second policy engine). Same-directory module.
import bot_capabilities  # noqa: E402
import serve  # noqa: E402  — host tier table + executor result formatting
import dev_bot  # noqa: E402  — writable-Bot gate + submit_bot_task entry point

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

def _provider_profiles() -> list[dict]:
    """Return configured provider/model profiles without exposing secrets."""
    raw = os.environ.get("KYREX_CHAT_PROVIDERS", "").strip()
    profiles = []
    if raw:
        try:
            value = json.loads(raw)
            entries = value.items() if isinstance(value, dict) else enumerate(value)
            for key, item in entries:
                if not isinstance(item, dict): continue
                provider = str(item.get("provider") or key).strip().lower()
                profile_id = str(item.get("id") or key or provider).strip().lower()
                models = item.get("models") or []
                if isinstance(models, str): models = [models]
                models = [str(m).strip() for m in models if str(m).strip()]
                if provider and profile_id and models:
                    profiles.append({"id": profile_id, "label": str(item.get("label") or profile_id),
                                     "provider": provider, "models": models,
                                     "api_key_env": str(item.get("api_key_env") or "KYREX_API_KEY"),
                                     "base_url": str(item.get("base_url") or "")})
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    default_provider = (os.environ.get("KYREX_PROVIDER") or os.environ.get("PROVIDER") or "openai").lower()
    default_model = (os.environ.get("KYREX_MODEL") or "").strip()
    if default_model and not any(p["provider"] == default_provider for p in profiles):
        profiles.append({"id": default_provider, "label": default_provider, "provider": default_provider,
                         "models": [default_model], "api_key_env": "KYREX_API_KEY", "base_url": ""})
    return profiles

def list_provider_profiles() -> list[dict]:
    return [{k: p[k] for k in ("id", "label", "provider", "models")} for p in _provider_profiles()]

def _resolve_provider(provider_id: str | None = None, selected_model: str | None = None) -> dict:
    default_provider = (os.environ.get("KYREX_PROVIDER") or os.environ.get("PROVIDER") or "openai").lower()
    default_model = (os.environ.get("KYREX_MODEL") or "").strip()
    provider = (provider_id or default_provider).strip().lower()
    model = (selected_model or default_model).strip()
    profile = next((p for p in _provider_profiles() if p["id"] == provider or p["provider"] == provider), None)
    if profile:
        provider = profile["provider"]
        if model not in profile["models"]:
            raise ChatUnavailable(f"model '{model}' is not available for provider '{provider}'")
        api_key = os.environ.get(profile["api_key_env"], "")
        base_url = profile["base_url"]
    else:
        if provider != default_provider or model != default_model:
            raise ChatUnavailable(f"provider '{provider}' is not configured for Kyrex Chat")
        api_key = os.environ.get("KYREX_API_KEY") or ""
        base_url = ""
    if provider == "anthropic":
        base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL", "")
    else:
        base_url = base_url or os.environ.get("KYREX_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    return {"provider": provider, "model": model, "api_key": api_key.strip(), "base_url": base_url.strip()}

def set_conversation_provider(user: str, conversation_id: str, provider: str, model: str) -> dict:
    conv = get_conversation(user, conversation_id)
    if conv is None: raise KeyError("conversation not found")
    if conv.get("bot_id"): raise ValueError("Bot-bound conversations use the Bot's configured model")
    cfg = _resolve_provider(provider, model)
    if not cfg["api_key"]: raise ChatUnavailable(f"provider '{cfg['provider']}' is not configured")
    conv["provider"], conv["model"] = cfg["provider"], cfg["model"]
    _write(user, conv)
    close_engine_session(user, conversation_id)
    return conv

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
# Host-allowed inspection tools exposed to the Chat engine process. The set
# is owned by bot_capabilities (single host source of truth); task_complete
# is included because the engine's system prompt mandates it for turn ends.
# Non-Bot sessions always receive this full base. Bot-bound sessions receive
# a policy-derived SUBSET (bot_capabilities.derive_bot_capabilities), so the
# host base is the mask and a Bot policy can only restrict, never widen.
CHAT_ENGINE_ALLOWED_TOOLS = ",".join(
    sorted(bot_capabilities.CHAT_HOST_BASE_TOOLS)
)


def _effective_caps(bot_cfg) -> frozenset:
    """The host-masked capability set for one engine session.

    ``bot_cfg["allowed_tools"]`` (a Bot-bound conversation's policy-derived
    allowlist) is intersected with the host base — a capability translation
    bug must never widen the host's own allowlist — and the
    protocol-mandated ``task_complete`` is always present. When the session
    is not Bot-bound (no ``allowed_tools`` key), the full host base applies,
    which is exactly the pre-Bots allowlist.
    """
    derived = (bot_cfg or {}).get("allowed_tools")
    if derived is not None:
        want = {str(t).strip() for t in derived if str(t).strip()}
    else:
        want = set(bot_capabilities.CHAT_HOST_BASE_TOOLS)
    return frozenset(
        (want & bot_capabilities.CHAT_HOST_BASE_TOOLS)
        | bot_capabilities.HOST_GRANTED_TOOLS
    )
ENGINE_HANDSHAKE_TIMEOUT = float(os.environ.get("KYREX_CHAT_ENGINE_START_TIMEOUT", "90"))
ENGINE_TURN_TIMEOUT = float(os.environ.get("KYREX_CHAT_ENGINE_TURN_TIMEOUT", "600"))
MAX_ENGINE_SESSIONS = int(os.environ.get("KYREX_CHAT_MAX_ENGINE_SESSIONS", "32"))
# Bounded cap for joining the turn worker during stream_chat finalization.
# The turn is cancelled first (engine interrupt / provider interrupt event),
# so the worker normally returns within a poll interval; this is defense in
# depth only. It replaces the old behavior of joining up to 30s in the
# generator's finally while the worker could still run out a full turn
# timeout — which left the async-generator finalizer task hanging.
WORKER_JOIN_TIMEOUT = float(os.environ.get("KYREX_CHAT_WORKER_JOIN_TIMEOUT", "5"))

# Step 2 (writable developer Bots): how long a Chat stream may follow one
# Bot task's durable event stream, and how often it polls the store for new
# events. These bound the consumer side only; the task itself has the
# executor watchdog and the store recovers orphans.
BOT_TASK_STREAM_MAX_SECONDS = float(
    os.environ.get("KYREX_CHAT_BOT_TASK_STREAM_MAX_SECONDS", "3600")
)
BOT_TASK_POLL_SECONDS = 0.25

_task_store_instance = None


def _task_store():
    """Return the process-wide CloudTaskStore used for Bot task submission.

    This is the SAME durable store the worker process claims tasks from; a
    separate connection per process is the established pattern (main.py and
    worker.py each own one), and SQLite WAL makes that safe.
    """
    global _task_store_instance
    if _task_store_instance is None:
        from task_store import CloudTaskStore
        _task_store_instance = CloudTaskStore()
    return _task_store_instance


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


# ── Bot registry surface (single authoritative registry — bots.py) ──
# Kyrex Chat is a surface for Bots, not the registry. Discovery and binding
# read the EXISTING server-side Bot registry (kyrex-cloud/bots.py) and never
# create a second one. User scoping mirrors the registry's own owner field
# (the same ownership the rest of Kyrex Cloud already persists): a Bot with
# an explicit owner is visible ONLY to that owner; a Bot with no owner is
# operator-created and visible to any authenticated user. Anything else
# fails closed.

class BotUnavailable(Exception):
    """The requested Bot cannot be bound: unknown, not visible to the user,
    or its Rift cannot be resolved safely. Never falls back to another Bot,
    a default Rift, or an arbitrary workspace."""


class BotRegistryError(Exception):
    """The Bot registry itself cannot be trusted (corrupt/unloadable)."""


def _bot_visible_to(bot: dict, user: str) -> bool:
    """Fail-closed visibility: owner matches the user, or no owner at all."""
    owner = str(bot.get("owner") or "").strip()
    return owner == "" or owner == user


def _bot_rift_resolves(bot: dict) -> bool:
    """Fail-closed Rift check, mirroring workspace resolution: the Rift must
    be an absolute path to an existing directory."""
    raw = str(bot.get("rift") or "").strip()
    p = Path(raw)
    if not p.is_absolute():
        return False
    try:
        return p.resolve().is_dir()
    except OSError:
        return False


def list_bots_for_user(user: str) -> list[dict]:
    """Bots visible to the authenticated user, from the existing registry.

    Exposes only UI metadata — id, name, status, model, availability. Never
    rift paths, policy, system prompts, credentials, or other internals.
    Registry errors are NOT swallowed: a corrupt/unloadable registry raises
    (the caller surfaces it as a 500), never a silent empty list.
    """
    registry = bots.load_bots()  # raises RegistryError on corruption
    out = []
    for bot in registry.values():
        if not _bot_visible_to(bot, user):
            continue
        out.append({
            "id": bot.get("id"),
            "name": bot.get("name"),
            "status": bot.get("status"),
            "manageable": str(bot.get("owner") or "") == user,
            "model": bot.get("model"),
            "available": _bot_rift_resolves(bot),
        })
    return sorted(out, key=lambda b: b["id"] or "")


def resolve_bot_for_user(user: str, bot_id: str) -> dict:
    """Resolve *bot_id* to the Bot the user may bind, fail-closed.

    Returns the registry bot dict. Raises BotUnavailable when the Bot does
    not exist, is not visible to *user*, or its Rift cannot be resolved
    safely; raises BotRegistryError when the registry file cannot be loaded.
    Never returns a fallback Bot.
    """
    bot_id = str(bot_id or "").strip()
    if not bot_id:
        raise BotUnavailable("bot_id is required")
    try:
        registry = bots.load_bots()
    except Exception as exc:
        raise BotRegistryError(f"bot registry unavailable: {exc}")
    bot = registry.get(bot_id)
    if bot is None:
        raise BotUnavailable(f"unknown bot: '{bot_id}'")
    if not _bot_visible_to(bot, user):
        raise BotUnavailable(f"bot '{bot_id}' is not available to this user")
    if not _bot_rift_resolves(bot):
        raise BotUnavailable(f"bot '{bot_id}' rift is not resolvable")
    return bot


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

    def __init__(self, workspace_path: Path, provider_cfg: dict,
                 bot_cfg: Optional[dict] = None):
        self.workspace = Path(workspace_path)
        self.denied_requests: list[dict] = []
        self.session_state: Optional[dict] = None
        self._closed = False

        # Bot-aware execution identity. When the conversation is bound to a
        # Bot, provider_cfg["model"] is overridden by the Bot's configured
        # model ("provider:model" per the registry schema) and the Bot's
        # system_prompt is carried to the engine process. Absent bot_cfg this
        # is exactly the pre-Bots session (server default model, no injected
        # prompt). `bot_id` is retained on the session so the session factory
        # can refuse to reuse it for a different Bot (no context bleed).
        bot_cfg = bot_cfg or {}
        self.bot_id: Optional[str] = (bot_cfg.get("bot_id") or "").strip() or None
        self.system_prompt: Optional[str] = \
            (bot_cfg.get("system_prompt") or "").strip() or None
        raw_bot_model = (bot_cfg.get("model") or "").strip()
        eff_provider = provider_cfg["provider"]
        eff_model = provider_cfg["model"]
        if raw_bot_model:
            if ":" in raw_bot_model:
                pfx, _, mdl = raw_bot_model.partition(":")
                if pfx.strip():
                    eff_provider = pfx.strip().lower()
                if mdl.strip():
                    eff_model = mdl.strip()
            else:
                eff_model = raw_bot_model
        self.model = eff_model

        # Effective capability allowlist for this session. A Bot-bound
        # conversation carries the policy-derived tool set (already masked by
        # the host base in bot_capabilities); _effective_caps re-applies the
        # host base as defense in depth and always keeps the
        # protocol-mandated task_complete. Kept on the session so the session
        # factory can refuse to reuse a process whose permissions changed
        # (a Bot policy edit must re-spawn the engine, never serve stale
        # capabilities).
        self.allowed_tools = _effective_caps(bot_cfg)

        env = os.environ.copy()
        env["KYREX_SURFACE"] = "Kyrex Chat"
        env["KYREX_READ_ONLY_REPO"] = "1"
        env["KYREX_ALLOWED_TOOLS"] = ",".join(sorted(self.allowed_tools))
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
        env["KYREX_PROVIDER"] = eff_provider
        env["KYREX_MODEL"] = eff_model
        env["KYREX_API_KEY"] = provider_cfg["api_key"]
        if eff_provider == "anthropic":
            if provider_cfg["base_url"]:
                env["ANTHROPIC_BASE_URL"] = provider_cfg["base_url"]
        else:
            if provider_cfg["base_url"]:
                env["KYREX_BASE_URL"] = provider_cfg["base_url"]
                env["OPENAI_BASE_URL"] = provider_cfg["base_url"]
        # The owning Bot's system prompt reaches the engine process; the
        # bridge injects it into the session once (see core_bridge.py).
        if self.system_prompt:
            env["KYREX_CHAT_SYSTEM_PROMPT"] = self.system_prompt

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

    def _stderr_delta(self, baseline: int) -> str:
        """Join stderr lines appended after *baseline* (captured at turn start),
        so only output produced DURING this turn is inspected."""
        with self._stderr_lock:
            return "\n".join(self.stderr_tail[baseline:])

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
            # Snapshot the stderr tail position so only output produced DURING
            # this turn is considered for frame-less failure detection.
            with self._stderr_lock:
                stderr_baseline = len(self.stderr_tail)
            while True:
                if cancel_check is not None and cancel_check():
                    self.interrupt()
                try:
                    frame = self._frames.get(timeout=0.1)
                except _queue.Empty:
                    if time.monotonic() > deadline:
                        raise EngineSessionError(
                            f"engine turn timed out after {int(ENGINE_TURN_TIMEOUT)}s")
                    if self._proc.poll() is not None:
                        raise EngineSessionError("engine process terminated mid-turn")
                    # Frame-less engine failure: the bridge raised an
                    # unhandled exception and printed a traceback — no
                    # chat_done / phase IDLE frame will ever follow. Detect
                    # the new stderr output now rather than waiting out the
                    # turn timeout.
                    tail = self._stderr_delta(stderr_baseline)
                    if tail and _has_engine_failure_marker(tail):
                        error = tail.strip().splitlines()[-1][:500]
                        break
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
                    # An explicit engine error frame is terminal: the bridge
                    # reports the failure after a fatal turn. Do not keep
                    # waiting (possibly until the turn timeout) for a
                    # chat_done that may never come.
                    msg = frame.get("content") or frame.get("message") or "engine error"
                    error = msg
                    break
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
                        workspace_path: Path,
                        bot_cfg: Optional[dict] = None,
                        provider_cfg: Optional[dict] = None) -> EngineSession:
    """Get (or spawn) the engine session for one conversation.

    *bot_cfg* — {"bot_id", "model", "system_prompt", "allowed_tools"} when the
    conversation is Bot-bound. A cached session is reused only if it lives in
    the SAME workspace, belongs to the SAME Bot identity, AND carries the SAME
    effective capabilities; otherwise it is closed and respawned — two Bots
    can never share an engine process or its context, and a changed Bot
    policy never reuses a process spawned under the old permissions.
    """
    key = (user, conversation_id)
    bot_cfg = bot_cfg or {}
    want_bot = (bot_cfg.get("bot_id") or "").strip() or None
    want_caps = _effective_caps(bot_cfg)
    sess = _engine_sessions.get(key)
    if sess is not None:
        alive = (not sess._closed) and sess._proc.poll() is None
        same_ws = sess.workspace == workspace_path
        same_bot = sess.bot_id == want_bot
        same_caps = sess.allowed_tools == want_caps
        if alive and same_ws and same_bot and same_caps:
            _engine_sessions.move_to_end(key)
            return sess
        sess.close()
        _engine_sessions.pop(key, None)
    cfg = provider_cfg or _resolve_provider()
    sess = EngineSession(workspace_path, cfg, bot_cfg or None)
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


def create_conversation(user: str, title: str = "New chat",
                        bot_id: str = "") -> dict:
    """Create a conversation, optionally bound to a Bot.

    ``bot_id=""``/None means ordinary Kyrex Chat (no Bot). A provided
    bot_id is validated HERE, fail-closed: the Bot must exist, be visible
    to *user*, and have a resolvable Rift. No fallback Bot, default Rift,
    or workspace is ever substituted. Older conversations carry no bot_id
    and behave exactly as before.
    """
    now = _now_iso()
    conv = {
        "conversation_id": uuid.uuid4().hex,
        "title": title or "New chat",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    bound = str(bot_id or "").strip()
    if bound:
        resolve_bot_for_user(user, bound)
        conv["bot_id"] = bound
    _write(user, conv)
    return conv


def _opencode_session_for(user: str, conv: dict) -> str:
    """Stable per-conversation OpenCode session id (generated once, persisted).

    The OpenCode gateway rejects requests without a stable x-opencode-session
    header (it cannot route them to a conversation). The repo-aware engine
    path gets its id from TreeSessionManager inside the engine; the pure-chat
    provider must carry an equally stable per-conversation id so the gateway
    accepts the request and conversation routing stays consistent across
    turns. Persisted on the conversation record so resuming a chat reuses the
    same OpenCode session.
    """
    sid = conv.get("opencode_session_id") or ""
    if not sid:
        sid = uuid.uuid4().hex
        conv["opencode_session_id"] = sid
        _write(user, conv)
    return sid


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
            "bot_id": data.get("bot_id"),
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


# Frame-less engine failure detection. core_bridge.py prints an unhandled
# exception as a traceback to stderr (and startup/loop failures as
# "FATAL: ..."); for those NO chat_done / phase IDLE frame ever follows, so
# run_turn must terminate the turn on this signal instead of waiting out the
# long turn timeout.
_ENGINE_FAILURE_MARKERS = ("traceback (most recent call last):", "fatal:", "exception caught:")


def _has_engine_failure_marker(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _ENGINE_FAILURE_MARKERS)


# Sentinel: the request did not express a workspace → use the conversation's
# stored binding (or none). An explicit "" detaches the workspace.
_WORKSPACE_UNSET = object()


def _bot_task_event_frame(event, store, task_id):
    """Map one durable task event to a Chat control frame (or None)."""
    etype = event.get("type")
    payload = event.get("payload") or {}
    if etype in ("submitted", "claimed"):
        status = "queued" if etype == "submitted" else "running"
        return {"type": "task", "task_id": task_id, "status": status}
    if etype == "status":
        status = payload.get("status")
        if status in ("queued", "running", "awaiting_approval"):
            return {"type": "task", "task_id": task_id, "status": status}
        return None
    if etype == "progress":
        return {"type": "progress", "payload": payload}
    if etype == "approval_requested":
        pending = store.get_pending_approval(task_id) or {}
        return {
            "type": "approval_request",
            "task_id": task_id,
            "approval_id": payload.get("approval_id"),
            "tier": pending.get("tier", payload.get("tier")),
            "summary": pending.get("summary") or payload.get("summary") or "",
            "detail": pending.get("detail") or "",
            "token": pending.get("token") or "",
        }
    if etype == "approval_resolved":
        return {
            "type": "approval_result",
            "task_id": task_id,
            "decision": payload.get("decision"),
        }
    if etype == "error":
        return {"type": "error", "message": payload.get("error") or "stream failure"}
    return None


async def _stream_writable_bot_task(user, conv, bot, user_content,
                                    conversation_id, cancel_event):
    """Submit a writable Bot turn to the executor path and stream its events.

    Reuses the EXISTING durable task event stream (flux.py) and maps it to
    Chat control frames. The terminal frame is produced here from the task's
    authoritative row, exactly like the provider/engine paths produce their
    terminal ``status`` frame.
    """
    from task_store import CloudTaskStore, TERMINAL_STATUSES
    import flux as flux_module

    store = _task_store()
    try:
        task_id = dev_bot.submit_bot_task(user, bot, user_content, store=store)
    except dev_bot.DevBotError as exc:
        raise ChatUnavailable(str(exc))

    yield {"type": "conversation", "conversation_id": conversation_id}

    loop = asyncio.get_running_loop()
    q = _queue.Queue()
    sentinel = object()
    final_result = None

    def pump():
        try:
            for event in flux_module.stream_events(
                store, task_id,
                after_event_id=0,
                max_seconds=BOT_TASK_STREAM_MAX_SECONDS,
            ):
                q.put(event)
        finally:
            q.put(sentinel)

    threading.Thread(
        target=pump, daemon=True,
        name=f"bot-task-{task_id[:12]}",
    ).start()

    try:
        while True:
            try:
                event = await asyncio.to_thread(
                    q.get, True, BOT_TASK_POLL_SECONDS)
            except _queue.Empty:
                if cancel_event.is_set():
                    try:
                        store.request_cancel(task_id)
                    except Exception:
                        pass
                    yield {"type": "status", "status": "cancelled",
                           "content": ""}
                    return
                continue
            if event is sentinel:
                break
            if event.get("type") == "result":
                final_result = event.get("payload") or {}
                continue
            frame = _bot_task_event_frame(event, store, task_id)
            if frame is not None:
                yield frame

        task = store.get(task_id) or {}
        status = task.get("status")
        result = task.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (json.JSONDecodeError, TypeError):
                result = {}
        if isinstance(result, dict) and result:
            final_result = result
        elif not isinstance(final_result, dict):
            final_result = {}

        if status == "done":
            content = serve.format_result(final_result) if final_result else ""
            if content:
                conv_now = get_conversation(user, conversation_id) or conv
                _append_message(user, conv_now, "assistant", content)
                _write(user, conv_now)
            yield {"type": "status", "status": "complete", "content": content}
        elif status == "failed":
            errs = (final_result or {}).get("errors") or []
            message = (errs[-1][:500] if errs
                       else (task.get("error") or "task failed"))
            yield {"type": "status", "status": "error", "message": message}
        elif status == "cancelled":
            yield {"type": "status", "status": "cancelled", "content": ""}
        else:
            yield {"type": "status", "status": "error",
                   "message": f"task ended with status {status}"}
    finally:
        # Abandonment / disconnect must stop the queued or running task; a
        # terminal task is a no-op inside request_cancel.
        try:
            if store.status(task_id) not in TERMINAL_STATUSES:
                store.request_cancel(task_id)
        except Exception:
            pass


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
    conv = get_conversation(user, conversation_id)
    if conv is None:
        conv = create_conversation(user, title=_title_from(user_content))
        conversation_id = conv["conversation_id"]
    provider_cfg = _resolve_provider(conv.get("provider"), conv.get("model"))
    if not provider_cfg["model"]:
        raise ChatUnavailable("KYREX_MODEL is not configured")
    if not provider_cfg["api_key"]:
        raise ChatUnavailable(f"provider '{provider_cfg['provider']}' is not configured")

    # ── bot binding (authoritative, resolved every turn) ──────────────
    # A Bot-bound conversation carries its explicit bot_id in storage — the
    # binding survives reloads and reconnects and is authoritative: each turn
    # re-resolves the SAME Bot for the requesting user and runs inside that
    # Bot's Rift. It never falls back to another Bot, a default Rift, a
    # workspace, or an arbitrary directory. Engine policy/tool permissions
    # are unchanged (read-only chat) — this pass only proves the identity
    # path end to end. When the Bot is gone, no longer visible, or its Rift
    # cannot be resolved, the turn fails closed with a clear error.
    resolved_ws = None
    bot_cfg: Optional[dict] = None
    bot_binding = conv.get("bot_id") or None
    route_to_executor = False
    if bot_binding:
        try:
            bot = resolve_bot_for_user(user, bot_binding)
        except (BotUnavailable, BotRegistryError) as exc:
            raise ChatUnavailable(str(exc))
        resolved_ws = Path(str(bot["rift"])).resolve()
        # Policy-aware execution: the Bot's policy (authoritative registry
        # record) is translated into the engine capability allowlist using
        # the EXISTING policy engine and host tier table. A malformed policy
        # fails the turn closed (ChatUnavailable) — never a fallback to
        # another Bot's policy, a global/default policy, or unrestricted
        # Chat behavior. The effective allowlist is a subset of the host
        # base: the host tier can never be lowered by a Bot policy.
        try:
            caps = bot_capabilities.derive_bot_capabilities(bot.get("policy"))
        except bot_capabilities.BotPolicyError as exc:
            raise ChatUnavailable(f"bot policy unavailable: {exc}")
        # Bot-aware execution: the engine session for this conversation is
        # spawned with the Bot's configured model (registry "provider:model"
        # schema) and system_prompt, running in the Bot's Rift, with the
        # policy-derived tool allowlist (caps["tools"]). The identity is
        # carried on the session (bot_id) so the factory can never hand one
        # Bot's process to another Bot's conversation.
        bot_cfg = {
            "bot_id": bot_binding,
            "model": bot.get("model") or "",
            "system_prompt": bot.get("system_prompt") or "",
            "allowed_tools": caps["tools"],
        }
        # The binding is authoritative: a simultaneous explicit workspace id
        # is contradictory and must not silently rebind the conversation.
        if workspace_id is not _WORKSPACE_UNSET \
                and (str(workspace_id or "").strip() or None):
            raise ChatUnavailable(
                "a bot-bound conversation cannot attach a workspace")

        # Step 2: a writable developer Bot (fs:write grant) routes OFF the
        # read-only Chat engine session and onto the EXISTING approval-capable
        # executor path (submit_bot_task -> CloudTaskStore -> TaskWorker ->
        # serve.run_task -> git_workflow --rift). Read-only Bots keep the
        # slice-3 read-only engine session. The single writable-Bot gate lives
        # in serve.py so this decision can never drift from serve.run_task's.
        try:
            route_to_executor = dev_bot.is_writable_bot_policy(bot.get("policy"))
        except Exception:
            route_to_executor = False

    # ── workspace resolution (non-bot conversations only) ─────────────
    # Absent on the request → use the conversation's stored binding (or none).
    # Explicit "" / None → detach: pure conversation, stored key removed.
    # The id is always matched against the server-side registry — a browser
    # request can never name a filesystem path directly.
    if bot_binding is None:
        if workspace_id is _WORKSPACE_UNSET:
            requested_ws = conv.get("workspace_id") or None
        else:
            requested_ws = str(workspace_id or "").strip() or None
        if requested_ws:
            resolved_ws = resolve_workspace(requested_ws)
            if resolved_ws is None:
                raise ChatUnavailable(
                    f"workspace '{requested_ws}' is not registered or is unavailable")
        # Persist the workspace binding (only when this request expressed
        # one). A bot-bound conversation never reaches here.
        if workspace_id is not _WORKSPACE_UNSET:
            if requested_ws:
                conv["workspace_id"] = requested_ws
            else:
                conv.pop("workspace_id", None)

    history = conv.get("messages", [])
    if resolved_ws is None:
        messages = build_messages(history, user_content)

    _append_message(user, conv, "user", user_content)
    _write(user, conv)

    if route_to_executor:
        cancel = cancel_event if cancel_event is not None else asyncio.Event()
        async for frame in _stream_writable_bot_task(
                user, conv, bot, user_content, conversation_id, cancel):
            yield frame
        return

    engine_session: Optional[EngineSession] = None
    if resolved_ws is not None:
        # ── repo-aware turn: the REAL Kyrex engine (core_bridge.py) ─────
        # Spawned with the workspace as its working directory, exactly like
        # the VS Code / Tauri IDE / headless-agent surfaces. Read-only is
        # enforced inside the engine process (see EngineSession).
        try:
            if bot_cfg:
                engine_session = _get_engine_session(
                    user, conversation_id, resolved_ws, bot_cfg)
            else:
                engine_session = _get_engine_session(
                    user, conversation_id, resolved_ws, bot_cfg, provider_cfg)
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
        # OpenCode gateway requires a stable x-opencode-session on every
        # request; the pure-chat provider must carry the per-conversation id
        # (the engine path gets it from TreeSessionManager inside the engine).
        provider = get_provider(
            cfg["provider"], cfg["api_key"], base_url=cfg["base_url"] or None,
            session_id=_opencode_session_for(user, conv),
        )

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
    # True once the worker produced a terminal outcome (__outcome__/__error__
    # frame). A close landing mid-await leaves this False — exactly the
    # disconnect case that must request cancellation in the finally.
    finished = False
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
                    # Cancellation takes effect asynchronously at the worker
                    # (provider event-loop interrupt / engine control frame).
                    # The "cancelled" terminal must not race ahead of the
                    # worker observing it — wait, bounded by
                    # WORKER_JOIN_TIMEOUT, for the worker's terminal outcome so
                    # the client-visible cancelled state reflects a genuinely
                    # stopped turn. Late token frames are dropped.
                    unwind_deadline = time.monotonic() + WORKER_JOIN_TIMEOUT
                    while time.monotonic() < unwind_deadline and not finished:
                        try:
                            late = await asyncio.to_thread(q.get, True, POLL_SECONDS)
                        except _queue.Empty:
                            continue
                        if isinstance(late, dict) and (
                                "__outcome__" in late or "__error__" in late):
                            finished = True
                    break
                continue
            if isinstance(token, dict) and "__outcome__" in token:
                outcome = token["__outcome__"]
                finished = True
                break
            if isinstance(token, dict) and "__error__" in token:
                outcome = _ERROR
                result = token["__error__"]
                finished = True
                break
            if cancel.is_set():
                outcome = _CANCELLED
                _request_cancel()
                # Bounded wait for the worker to observe the interrupt (see
                # the queue-empty branch above): the terminal must reflect a
                # stopped turn, not merely a requested stop.
                unwind_deadline = time.monotonic() + WORKER_JOIN_TIMEOUT
                while time.monotonic() < unwind_deadline and not finished:
                    try:
                        late = await asyncio.to_thread(q.get, True, POLL_SECONDS)
                    except _queue.Empty:
                        continue
                    if isinstance(late, dict) and (
                            "__outcome__" in late or "__error__" in late):
                        finished = True
                break
            full.append(token)
            if first:
                yield {"type": "conversation", "conversation_id": conversation_id}
                first = False
            yield {"type": "delta", "content": token}

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
                finished = True
                if outcome in (_SENTINEL,):
                    outcome = leftover["__outcome__"]
            elif isinstance(leftover, dict) and "__error__" in leftover:
                finished = True
                result = leftover["__error__"]
                if outcome not in (_CANCELLED,):
                    outcome = _ERROR

        # Cancellation wins over a worker-reported CLEAN completion: the
        # worker can finish (and enqueue __outcome__) in the same window the
        # cancel flag flips, before this loop observes it — without this a
        # user-cancelled turn would report complete. The decision is made
        # HERE, at terminal time, NOT inside the frame loop, so the interrupt
        # is never requested against a stale/cleared provider handle, and a
        # worker-reported ERROR still wins (the turn really failed).
        if cancel.is_set() and outcome is _SENTINEL:
            outcome = _CANCELLED
            _request_cancel()

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

        # Terminal status frame. An async generator cannot ``return`` a value,
        # so the terminal outcome is yielded as the final control frame, which
        # the caller (_drive_stream) maps to the matching explicit SSE event.
        # This yield lives INSIDE the try: once finalization has begun (the
        # finally below / GeneratorExit from aclose) no further yield is legal,
        # and a close must never leave the async_generator_athrow finalizer
        # task waiting on this stream.
        if outcome is _ERROR:
            yield {"type": "status", "status": "error",
                   "message": result or "provider error"}
        elif outcome is _CANCELLED:
            yield {"type": "status", "status": "cancelled", "content": final_text}
        else:
            yield {"type": "status", "status": "complete", "content": final_text}
    finally:
        # A cancelled or disconnected stream must not leave the worker running.
        # Cancellation is only skipped when the worker already produced its
        # terminal outcome (finished): on every abandon/close path we still
        # request it so the engine/provider stops promptly.
        if not finished:
            _request_cancel()
        # Bounded worker unwind. Never join for the full turn timeout and
        # never block the event-loop thread: run the (capped) join on a
        # worker thread. If finalization happens without a running loop
        # (post-close GC), fall back to a bounded synchronous join — the GC
        # thread can absorb the short wait. The cap keeps generator
        # finalization bounded so no async_generator_athrow task is left
        # pending while a long join drains.
        try:
            await asyncio.to_thread(worker.join, WORKER_JOIN_TIMEOUT)
        except RuntimeError:
            worker.join(timeout=WORKER_JOIN_TIMEOUT)
