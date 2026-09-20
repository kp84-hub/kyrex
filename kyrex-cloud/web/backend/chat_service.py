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
import re
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
# Calendar Writer core: deterministic create-intent normalisation + validation.
import cal_writer  # noqa: E402  — the ONE source of "a safe create intent"
# Bot-to-Bot delegation (owner-scoped, single-level). Reuses the same registry,
# durable store, and host tier table; never a second bus or policy engine.
import delegation  # noqa: E402
import provider_profiles as user_provider_profiles  # noqa: E402
# Per-Bot LLM configuration: resolves a Bot's owner-scoped provider profile
# (provider / base URL / key / approved headers / validated model). Same
# directory; fail-closed when the Bot's configuration is missing or invalid.
import bot_provider  # noqa: E402

# ── engine import ──────────────────────────────────────────────────
# Reuse the installed Kyrex engine package's provider plumbing
# (retry/backoff, streaming callbacks) instead of re-implementing it.
ENGINE_DIR = KYREX_CLOUD_DIR.parent / "kyrex_engine"
from kyrex.providers import get_provider  # noqa: E402

# ── config ─────────────────────────────────────────────────────────
CHAT_DIR_NAME = "chat"
CHAT_SYSTEM_PROMPT = (
    "You are Kyrex Chat, the conversational assistant product from Kyrex. "
    "Answer clearly, directly, and in a natural conversational tone. "
    "Format responses with Markdown where it aids readability. "
    "Talk like a person: answer briefly and naturally, and for a simple "
    "greeting reply with one short friendly line (for example "
    "\"Hey — what would you like to work on?\"). Never answer a greeting with "
    "a list of your capabilities, tools, memory, file tree, providers, or "
    "modes unless the user explicitly asks about them. Never claim to have "
    "read files, memory, a workspace, or run a tool unless a tool call "
    "actually succeeded in this turn."
)

# Conversation modes surfaced to an ordinary (non-Bot) turn's dynamic system
# context. These are descriptive labels ONLY — they never select routing and
# never change any capability gate. Workspace/Bot turns keep their existing
# engine/executor paths (and their own prompts); see build_system_context.
MODE_ORDINARY = "ordinary"
MODE_WORKSPACE = "workspace"
MODE_BOT = "bot"

MAX_MESSAGE_CHARS = 32_000


# ── user-facing assistant text ──────────────────────────────────────
# The engine (kyrex_engine/kyrex/core.py) appends INTERNAL lifecycle markers to
# its final ``chat_done`` content — ``[Task Complete: …]``, the ``[continue]``
# tool-less-round nudge, loop-detector / circuit-breaker diagnostics, and the
# max-recursion notice. That content is authoritative and is what Chat streams
# and persists, so without this boundary the markers became assistant message
# text. Task-completion SEMANTICS are deliberately preserved elsewhere (the
# engine and the TUI consume the markers unchanged); only the user-facing
# rendering below changes.
#
# Deliberately NOT stripped: provider / engine error text. A real failure must
# stay visible as an error.
_MARKER_LINE_PATTERNS = (
    re.compile(r"^\s*\[Task Complete(?::[^\]]*)?\]\s*$"),
    re.compile(r"^\s*\[Task assumed complete[^\]]*\]\s*$"),
    re.compile(r"^\s*\[continue\][^\n]*$"),
    re.compile(r"^\s*\[!\]\s*Task not verified complete[^\n]*$"),
    re.compile(r"^\s*\[!\]\s*Max recursion depth reached\.?\s*$"),
    re.compile(r"^\s*\[Model produced reasoning but no display content\.[^\]]*\]\s*$"),
)

# The engine streams this divider between provider rounds of one turn
# (kyrex_engine/kyrex/core.py ``streamer("\n\n---\n")``). Collapsing it keeps
# multiple internal rounds reading as one coherent response.
_ROUND_DIVIDER_RE = re.compile(r"\n\s*\n\s*---\s*\n\s*\n")


def sanitize_assistant_text(text) -> str:
    """Strip internal control markers from assistant output for display.

    Presentation-only. Removes:

      * internal lifecycle marker lines — ``[Task Complete: …]``,
        ``[continue] …``, ``[!] Task not verified complete …`` (loop detector /
        circuit breaker), ``[!] Max recursion depth reached.``, and the
        reasoning-only diagnostic;
      * the engine's inter-round divider, so multiple internal rounds read as
        one coherent response;
      * a paragraph that merely repeats the one directly above it (the engine
        concatenates every round's content, which can otherwise render as a
        duplicated reply).

    Provider/engine error text is never removed — a real failure stays visible.
    """
    if not text:
        return ""
    cleaned = str(text).replace("\r\n", "\n")
    cleaned = _ROUND_DIVIDER_RE.sub("\n\n", cleaned)
    kept = [
        line for line in cleaned.split("\n")
        if not any(p.match(line) for p in _MARKER_LINE_PATTERNS)
    ]
    cleaned = "\n".join(kept)
    # Collapse an immediately-repeated paragraph (multi-round duplication).
    blocks = re.split(r"\n\s*\n", cleaned)
    deduped: list[str] = []
    prev_key = None
    for block in blocks:
        key = block.strip()
        if key and key == prev_key:
            continue
        deduped.append(block)
        prev_key = key
    return "\n\n".join(deduped).strip()


def sanitize_conversation(conv):
    """Return *conv* with assistant messages sanitized for the client.

    Presentation boundary for READ paths (``GET /api/conversations/{id}``): a
    conversation persisted before the sanitizer existed must still render
    cleanly. The stored record is never mutated here — a shallow copy with new
    message dicts is returned, so history fed back to the provider is
    unaffected.
    """
    if not isinstance(conv, dict):
        return conv
    messages = conv.get("messages")
    if not isinstance(messages, list):
        return conv
    out = dict(conv)
    cleaned_msgs = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant" \
                and isinstance(m.get("content"), str):
            mm = dict(m)
            mm["content"] = sanitize_assistant_text(m["content"])
            cleaned_msgs.append(mm)
        else:
            cleaned_msgs.append(m)
    out["messages"] = cleaned_msgs
    return out


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

def _provider_profiles(user: str | None = None) -> list[dict]:
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
    default_base = os.environ.get("KYREX_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""
    if "opencode.ai/zen/go/" in default_base.lower():
        go_models = [
            "grok-4.6", "glm-5.3-flash", "glm-5.3", "glm-5.2", "glm-5.1",
            "gpt-5.6-luna", "kimi-k3", "kimi-k2.7-code", "kimi-k2.6",
            "longcat-2.0", "mimo-v2.5", "mimo-v2.5-pro", "minimax-m3",
            "minimax-m2.7", "muse-spark-1.3-contributor", "muse-spark-1.2-contributor",
            "qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus",
            "qwen3.6-plus", "deepseek-v4.1-flash", "deepseek-v4-pro",
            "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "hy4", "hy3",
        ]
        if not any(p["id"] == "opencode-go" for p in profiles):
            profiles.append({"id": "opencode-go", "label": "OpenCode Go", "provider": default_provider,
                             "models": go_models, "api_key_env": "KYREX_API_KEY", "base_url": default_base})
    elif default_model and not any(p["provider"] == default_provider for p in profiles):
        profiles.append({"id": default_provider, "label": default_provider, "provider": default_provider,
                         "models": [default_model], "api_key_env": "KYREX_API_KEY", "base_url": ""})
    if user:
        for saved in user_provider_profiles._read(user):
            profiles.append({
                "id": saved["id"], "label": saved["name"], "provider": saved["provider"],
                "models": saved["models"], "api_key": saved["api_key"],
                "base_url": saved["base_url"],
            })
    return profiles

def list_provider_profiles(user: str | None = None) -> list[dict]:
    return [{k: p[k] for k in ("id", "label", "provider", "models")} for p in _provider_profiles(user)]

def _resolve_provider(provider_id: str | None = None, selected_model: str | None = None,
                      user: str | None = None) -> dict:
    default_provider = (os.environ.get("KYREX_PROVIDER") or os.environ.get("PROVIDER") or "openai").lower()
    default_model = (os.environ.get("KYREX_MODEL") or "").strip()
    provider = (provider_id or default_provider).strip().lower()
    model = (selected_model or default_model).strip()
    profile = next((p for p in _provider_profiles(user) if p["id"] == provider), None)
    if profile:
        provider = profile["provider"]
        if model not in profile["models"]:
            raise ChatUnavailable(f"model '{model}' is not available for provider '{provider}'")
        api_key = profile.get("api_key") or os.environ.get(profile.get("api_key_env", "KYREX_API_KEY"), "")
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
    # OpenCode requires an explicit endpoint: the gateway routes requests by
    # it, and a missing base_url must fail clearly — never a silent default
    # host or an inherited global KYREX_* endpoint for an OpenCode selection.
    if provider == "opencode" and not base_url:
        raise ChatUnavailable(
            "OpenCode provider has no endpoint — set the API URL "
            "(https://opencode.ai/zen/go/v1) on the provider profile"
        )
    return {"provider": provider, "profile": profile["id"] if profile else provider,
            "model": model, "api_key": api_key.strip(), "base_url": base_url.strip()}

def set_conversation_provider(user: str, conversation_id: str, provider: str, model: str) -> dict:
    conv = get_conversation(user, conversation_id)
    if conv is None: raise KeyError("conversation not found")
    if conv.get("bot_id"): raise ValueError("Bot-bound conversations use the Bot's configured model")
    cfg = _resolve_provider(provider, model, user=user)
    if not cfg["api_key"]: raise ChatUnavailable(f"provider '{cfg['provider']}' is not configured")
    conv["provider"], conv["model"] = cfg["profile"], cfg["model"]
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
# Bounded follow window for delegated target tasks streamed into a coordinator
# conversation. The target task itself has its own watchdog and the store
# recovers orphans; this only bounds the coordinator's viewer.
DELEGATION_STREAM_MAX_SECONDS = float(
    os.environ.get("KYREX_CHAT_DELEGATION_STREAM_MAX_SECONDS", "1800")
)

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


def _redacted_browser_allowlist(bot: dict) -> list:
    """The Bot's browser domain allowlist, redacted for display.

    Bare hostnames carry no secrets, but every entry is still passed through
    the Browser Host redactor so a malformed/legacy entry can never surface a
    credential-shaped value. Always a list of strings.
    """
    raw = bot.get("browser_allowlist")
    if not isinstance(raw, list):
        return []
    try:
        from browser_hosts import redact_text  # noqa: E402 — Cloud path
    except Exception:
        def redact_text(value):  # noqa: ANN001 — fallback: entries are hosts
            return value
    return [redact_text(h) for h in raw if isinstance(h, str)]


def list_bots_for_user(user: str) -> list[dict]:
    """Bots visible to the authenticated user, from the existing registry.

    Exposes only UI metadata — id, name, status, model, availability, and the
    Bot's REDACTED browser domain allowlist. Never rift paths, policy, system
    prompts, credentials, or other internals. Registry errors are NOT
    swallowed: a corrupt/unloadable registry raises (the caller surfaces it as
    a 500), never a silent empty list.
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
            # The browser domain allowlist, REDACTED (bare hostnames only; an
            # empty list is the fail-closed "deny every navigation" default).
            # Exposed so the Chat roster can gate Browser Bot eligibility and
            # render the owner's editable allowlist.
            "browser_allowlist": _redacted_browser_allowlist(bot),
            "manageable": str(bot.get("owner") or "") == user,
            # An ownerless (legacy) Bot is VISIBLE but not manageable; the UI
            # offers an explicit one-time claim for exactly these. Any Bot
            # owned by another user never reaches this list at all.
            "claimable": str(bot.get("owner") or "").strip() == "",
            "model": bot.get("model"),
            "available": _bot_rift_resolves(bot),
            # Coordinator capability (owner-scoped, explicitly granted). A
            # read-only flag safe for the UI; the policy rules behind it are
            # never exposed.
            "coordinator": serve.coordinator_granted(bot),
            # Read-only Browser Bot flag, derived from server state (the
            # read-only browser grant + non-empty allowlist + explicit Browser
            # Host binding). The UI badge renders THIS — never a local guess,
            # and it under-claims the moment any capability is added.
            "browser_bot": dev_bot.browser_bot_ready(bot),
        })
    return sorted(out, key=lambda b: b["id"] or "")


def resolve_bot_for_user(user: str, bot_id: str) -> dict:
    """Resolve *bot_id* to the Bot the user may bind, fail-closed.

    Returns the registry bot dict. Raises BotUnavailable when the Bot does
    not exist, is not visible to *user*, is not lifecycle-``running``, or its
    Rift cannot be resolved safely; raises BotRegistryError when the registry
    file cannot be loaded. Never returns a fallback Bot. The lifecycle check
    is the authoritative server-side gate for NEW work (a paused/stopped Bot
    can neither be newly bound nor serve a new turn).
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
    # Lifecycle gate (server-side, authoritative). Both binding a NEW
    # conversation and resolving an existing conversation's NEXT turn pass
    # through here, so a paused/stopped Bot rejects new turns and new
    # bindings — never relying on the UI. Kyrex runs Bots on a shared worker,
    # so "running" means eligible for new Chat/task work; it never asserts
    # that a separate process was launched. The status check is deliberately
    # last: an unresolvable Rift (a hard availability fault) is reported as
    # such even when the Bot is also stopped.
    if not bots.is_running(bot):
        raise BotUnavailable(
            f"bot '{bot_id}' is {bot.get('status') or bots.STATUS_STOPPED} — "
            "start it to use it in Kyrex Chat"
        )
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
        # Per-conversation engine session directory, carried on bot_cfg by the
        # session factory (keyed by (user, conversation_id)). The engine
        # writes/loads its session history and reasoning audit there instead
        # of the shared workspace (<rift>/.px_sessions). That is what stops
        # conversation B from loading conversation A's history when both are
        # bound to the same Bot and share the Rift as cwd. Its signature stays
        # backward-compatible with existing EngineSession stand-ins.
        self.session_dir = str(
            (bot_cfg or {}).get("session_dir") or ""
        ).strip() or None
        self.denied_requests: list[dict] = []
        self.session_state: Optional[dict] = None
        self._closed = False
        # Per-turn Kyrex Chat surface context (workspace-attached NON-Bot
        # conversations). Set by stream_chat immediately before run_turn so the
        # live engine session is refreshed every turn — never a stale
        # spawn-time snapshot. None for Bot-bound sessions (identity is the
        # Bot's own prompt) and for the pure-chat path (unused).
        self.surface_context: Optional[str] = None

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
        if provider_cfg.get("bot_profile"):
            # Per-Bot profile config: the profile is authoritative for BOTH
            # provider and model. The registry model is used only if the
            # profile somehow supplied none — a "provider:model" prefix must
            # NEVER override the profile's provider (that would be a fallback).
            if not eff_model:
                eff_model = raw_bot_model
        elif raw_bot_model:
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

        # Coordinator identity for Bot-to-Bot delegation. Present ONLY for a
        # Bot-bound conversation whose Bot holds the coordinator capability;
        # None for every read-only / writable / non-Bot session. When present,
        # a ``delegate_task`` tool call (confirm_request value "delegation") is
        # answered by the HOST (delegation.submit_delegation) instead of being
        # denied. Every other confirmation is still denied in read-only Chat.
        self.delegation_ctx: Optional[dict] = (bot_cfg or {}).get("delegation") or None

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
        # Per-conversation engine session directory: the engine's durable
        # session history + reasoning audit are written HERE (outside the
        # shared Rift) instead of <rift>/.px_sessions. Without this, two
        # conversations bound to the same Bot — sharing the Rift as cwd —
        # load each other's history. A stale inherited value must never leak
        # into a session that has no explicit directory.
        if self.session_dir:
            env["KYREX_SESSION_DIR"] = self.session_dir
        else:
            env.pop("KYREX_SESSION_DIR", None)
        # Provider config comes from the same env keys the chat service uses
        # (ConfigManager consults KYREX_* env before any config file).
        env["KYREX_PROVIDER"] = eff_provider
        env["KYREX_MODEL"] = eff_model
        env["KYREX_API_KEY"] = provider_cfg["api_key"]
        # A Bot-profile provider is authoritative. When it carries no base URL
        # we must NOT inherit an ambient KYREX_BASE_URL / OPENAI_BASE_URL /
        # ANTHROPIC_BASE_URL from the host — that would be a silent global
        # fallback. Non-Bot sessions keep the existing inheritance behaviour.
        _bot_profile = bool(provider_cfg.get("bot_profile"))
        if eff_provider == "anthropic":
            if provider_cfg["base_url"]:
                env["ANTHROPIC_BASE_URL"] = provider_cfg["base_url"]
            elif _bot_profile:
                env.pop("ANTHROPIC_BASE_URL", None)
        else:
            if provider_cfg["base_url"]:
                env["KYREX_BASE_URL"] = provider_cfg["base_url"]
                env["OPENAI_BASE_URL"] = provider_cfg["base_url"]
            elif _bot_profile:
                env.pop("KYREX_BASE_URL", None)
                env.pop("OPENAI_BASE_URL", None)
        # Approved per-Bot custom headers. The engine's ConfigManager merges
        # these and refuses to let them override Authorization or the
        # session-routing header. Only a Bot-profile config contributes them.
        _headers = provider_cfg.get("headers") or {}
        if _bot_profile and _headers:
            env["KYREX_PROVIDER_HEADERS"] = json.dumps(_headers)
        else:
            env.pop("KYREX_PROVIDER_HEADERS", None)
        # The owning Bot's system prompt reaches the engine process; the
        # bridge injects it into the session once (see core_bridge.py).
        if self.system_prompt:
            env["KYREX_CHAT_SYSTEM_PROMPT"] = self.system_prompt
        # A coordinator Bot keeps its own prompt BUT must also receive the
        # refreshed safe peer roster each turn. This flag tells the bridge that
        # the coordinator surface context IS allowed to refresh in a Bot-bound
        # session (see core_bridge._apply_surface_context).
        if self.delegation_ctx is not None:
            env["KYREX_CHAT_COORDINATOR"] = "1"

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

    def _handle_delegation(self, frame: dict) -> tuple[bool, dict]:
        """Answer a coordinator's ``delegate_task`` request (host-side).

        The host — never the engine — creates the durable delegation and the
        ordinary target task through the EXISTING store/worker path. Failures
        are returned as ``(False, {"error": ...})`` so the coordinator model
        gets a clear, safe refusal. The returned dict carries only safe ids and
        status; no target credential, Rift, prompt, or approval secret ever
        crosses this boundary.
        """
        ctx = self.delegation_ctx or {}
        try:
            safe = delegation.submit_delegation(
                ctx.get("owner"),
                ctx.get("bot") or {},
                frame.get("target_bot_id"),
                frame.get("task"),
                parent_conversation_id=ctx.get("conversation_id"),
                parent_task_id=ctx.get("parent_task_id"),
            )
            return True, dict(safe)
        except delegation.DelegationError as exc:
            return False, {"error": str(exc)}
        except Exception as exc:  # never leak a traceback to the model
            return False, {"error": f"delegation failed: {type(exc).__name__}: {exc}"}

    def _handle_delegation_status(self, frame: dict) -> tuple[bool, dict]:
        """Answer a coordinator's ``delegation_status`` request (host-side).

        Reads the EXISTING durable delegation/task records and returns the
        current safe status. Owner- and coordinator-scoped: only delegations
        THIS coordinator created for its OWN owner are returned. Read-only —
        the host never approves, denies, or cancels the target task, and no
        approval token ever crosses this boundary.

        ``frame["delegation_id"]`` optionally narrows the query to one
        delegation. ``(False, {"error": ...})`` reports a failure/refusal so the
        model can say what happened; no traceback is ever leaked.
        """
        ctx = self.delegation_ctx or {}
        try:
            bot_id = str((ctx.get("bot") or {}).get("id") or "")
            statuses = coordinator_delegation_statuses(
                ctx.get("owner"),
                bot_id,
                ctx.get("conversation_id"),
                delegation_id=frame.get("delegation_id"),
            )
            return True, {"delegations": statuses, "count": len(statuses)}
        except Exception as exc:  # never leak a traceback to the model
            return False, {
                "error": f"delegation status failed: {type(exc).__name__}: {exc}"}

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
            frame_out = {"type": "chat", "content": text}
            # Refresh the Kyrex Chat surface context on EVERY turn so a
            # long-lived workspace session never serves a stale roster. A
            # Bot-bound session has surface_context None (its identity is the
            # Bot's own prompt), so its frame is byte-for-byte unchanged.
            if self.surface_context:
                frame_out["surfaceContext"] = self.surface_context
            self._send(frame_out)
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
                    _confirm_value = str(frame.get("value"))
                    if (self.delegation_ctx is not None
                            and _confirm_value == "delegation"):
                        # Coordinator: the HOST creates the durable delegation
                        # + ordinary target task, then replies with its safe
                        # outcome. This is the ONLY confirmation a coordinator
                        # session answers affirmatively; it never executes the
                        # target inline and never touches target credentials.
                        approved, result = self._handle_delegation(frame)
                        self._send({"type": "confirm_response",
                                    "id": frame.get("id"),
                                    "approved": approved, "result": result})
                    elif (self.delegation_ctx is not None
                            and _confirm_value == "delegation_status"):
                        # Coordinator: a READ of the existing delegation/task
                        # records — owner- and coordinator-scoped. It answers
                        # "did it finish?" from durable state and never
                        # approves/denies anything.
                        approved, result = self._handle_delegation_status(frame)
                        self._send({"type": "confirm_response",
                                    "id": frame.get("id"),
                                    "approved": approved, "result": result})
                    else:
                        # Read-only chat: deny every other confirmation gate.
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

    The Kyrex Chat surface context is NOT carried here: because a session is
    reused across turns, a spawn-time context would go stale. It is refreshed
    per turn instead (EngineSession.surface_context -> the chat frame), so a
    long-lived workspace conversation always sees the current safe roster.
    """
    key = (user, conversation_id)
    bot_cfg = dict(bot_cfg or {})
    want_bot = (bot_cfg.get("bot_id") or "").strip() or None
    want_caps = _effective_caps(bot_cfg)
    # The isolation identity: this conversation's OWN durable session
    # directory, keyed by (owner, bot_id, conversation_id). The engine loads
    # its history from here, never from the shared Rift, so a reused session
    # is reused ONLY within this conversation.
    want_session_dir = serve.conversation_session_dir(
        user, want_bot or "workspace", conversation_id)
    bot_cfg["session_dir"] = want_session_dir
    sess = _engine_sessions.get(key)
    if sess is not None:
        alive = (not sess._closed) and sess._proc.poll() is None
        same_ws = sess.workspace == workspace_path
        same_bot = sess.bot_id == want_bot
        same_caps = sess.allowed_tools == want_caps
        # A session that predates the session_dir attribute (a stand-in with
        # no notion of one) is treated as matching, so reuse semantics are
        # unchanged for it. The real EngineSession always carries the
        # attribute, so a changed conversation/owner/bot still re-spawns.
        same_session = getattr(sess, "session_dir", want_session_dir) == want_session_dir
        if alive and same_ws and same_bot and same_caps and same_session:
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


# Maximum characters of the owner-typed task text carried on the sidebar's
# active-work descriptor. The text is owner-typed (never model output) and is
# used only to render one short, secondary status line, so it is capped.
_ACTIVITY_TEXT_LIMIT = 200
_ACTIVE_STATUSES = ("queued", "running", "awaiting_approval")


def _conversation_activity(user: str, conversation_id: str) -> Optional[dict]:
    """Durable active-work descriptor for one conversation, or ``None``.

    Read-only, derived EXCLUSIVELY from existing durable state:

      * a non-terminal DELEGATION linked to the conversation (Chief-of-Staff
        Bot-to-Bot work) takes precedence — the user is waiting on the TARGET
        Bot; otherwise
      * the newest non-terminal ordinary Bot TASK linked to the conversation.

    Nothing here is model-generated: only identities, the lifecycle status,
    and the owner-typed task text. Any store fault returns ``None`` so a list
    refresh can never fail because activity could not be read.
    """
    cid = str(conversation_id or "").strip()
    if not cid:
        return None
    try:
        store = _task_store()
    except Exception:
        return None

    # 1. Delegated (Bot-to-Bot) work — the user is waiting on the target.
    try:
        for rec in store.list_delegations(
                owner=user, parent_conversation_id=cid, limit=25):
            status = str(rec.get("status") or "")
            if status in _ACTIVE_STATUSES:
                return {
                    "kind": "delegation",
                    "status": status,
                    "task_id": rec.get("task_id"),
                    "delegation_id": rec.get("delegation_id"),
                    "target_bot_id": rec.get("target_bot_id"),
                    "text": str(rec.get("task_text") or "")[:_ACTIVITY_TEXT_LIMIT],
                }
    except Exception:
        pass

    # 2. An ordinary Bot task running in the conversation.
    try:
        task = store.latest_active_task_for_conversation(cid)
    except Exception:
        task = None
    if task is not None and str(task.get("status") or "") in _ACTIVE_STATUSES:
        return {
            "kind": "task",
            "status": str(task.get("status")),
            "task_id": task.get("task_id"),
            "text": str(task.get("task_text") or "")[:_ACTIVITY_TEXT_LIMIT],
        }
    return None


def list_conversations(user: str) -> list[dict]:
    d = _user_dir(user)
    out = []
    for p in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        # provider_profiles.json lives alongside conversations in the same
        # user directory but is a JSON *array* of encrypted entries — it is
        # not a conversation record and must never be treated as one.
        if p.name == "provider_profiles.json" or p.name.endswith(".tmp"):
            continue
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        # Only dictionary-shaped records are conversations; anything else
        # (arrays, strings, scalars) is skipped instead of raising.
        if not isinstance(data, dict):
            continue
        out.append({
            "conversation_id": data.get("conversation_id", p.stem),
            "title": data.get("title", "New chat"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "message_count": len(data.get("messages", [])),
            "workspace_id": data.get("workspace_id"),
            "bot_id": data.get("bot_id"),
            # Durable active work (a pending/running Bot task or delegation),
            # or None. The sidebar renders it as a secondary line and updates
            # it live from the same task's Flux stream — no polling.
            "activity": _conversation_activity(
                user, data.get("conversation_id", p.stem)),
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


def _append_message(user: str, conv: dict, role: str, content: str,
                    identity: Optional[str] = None) -> dict:
    """Append one message to *conv*, idempotent under *identity*.

    When an *identity* is supplied (an existing message/request id — the
    conversation already carries unique ids on every message), a second
    finalization for the SAME turn is a no-op instead of persisting an
    identical duplicate assistant/user message. Callers that have no
    turn-scoped identity keep the historical uuid behavior.

    The stored record keeps its ``id`` field as the equality key, so a
    replayed stream or a retried POST with the same ``request_id`` can never
    produce two identical assistant messages in one conversation.
    """
    identity = str(identity or "").strip()
    if identity:
        for existing in conv.get("messages") or []:
            if isinstance(existing, dict) and existing.get("id") == identity:
                return existing
    msg = {
        "id": identity or uuid.uuid4().hex,
        "role": role,
        "content": content,
        "created_at": _now_iso(),
    }
    conv["messages"].append(msg)
    return msg


def _title_from(user_message: str) -> str:
    t = " ".join(user_message.split())
    return t[:40] + ("..." if len(t) > 40 else "") or "New chat"


# ── coordinator awareness: per-turn dynamic system context ──────────
# An ordinary (non-Bot) Kyrex Chat turn is a generic conversational window
# unless the model is told who it is. build_system_context() gives it the
# context to behave as the Kyrex Chat coordinator: its identity, the current
# mode, its capability boundary, and the Bots the authenticated user can
# reach.
#
# Safety contract (never violated): only UI-safe Bot metadata is emitted —
# id, name, status, model string, availability, and a derived "writable
# developer Bot" boolean. Rift paths, policies, system prompts, credentials,
# provider keys, and approval tokens are never included, and never inferable
# from what is. Bot-bound turns do NOT use this builder (a Bot keeps its own
# system prompt and authority boundary); workspace-attached turns run inside
# the read-only engine session and likewise keep their own prompt.

def _bot_roster_lines(user: str) -> list[str]:
    """One safe, human-readable line per Bot visible to *user*.

    Visibility reuses the SAME rule as :func:`list_bots_for_user` (explicit
    owner match, or operator-created with no owner). The writable flag reuses
    the SAME gate the executor enforces with
    (:func:`dev_bot.is_writable_bot_policy`). A corrupt/unloadable registry
    yields no roster lines — an ordinary chat turn still answers, and nothing
    is ever invented; the /api/bots surface remains where a registry fault is
    reported.
    """
    try:
        registry = bots.load_bots()  # raises RegistryError on corruption
    except Exception:
        return []
    lines: list[str] = []
    for bot in sorted(registry.values(), key=lambda b: str(b.get("id") or "")):
        if not _bot_visible_to(bot, user):
            continue
        bot_id = str(bot.get("id") or "").strip()
        if not bot_id:
            continue
        name = str(bot.get("name") or bot_id)
        status = str(bot.get("status") or "unknown")
        model = str(bot.get("model") or "").strip()
        available = _bot_rift_resolves(bot)
        try:
            writable = bool(dev_bot.is_writable_bot_policy(bot.get("policy")))
        except Exception:
            writable = False
        fields = [
            f"status: {status}",
            f"available: {'yes' if available else 'no'}",
        ]
        if model:
            fields.append(f"model: {model}")
        fields.append(f"writable developer bot: {'yes' if writable else 'no'}")
        lines.append(f'- id: {bot_id} | name: "{name}" | ' + " | ".join(fields))
    return lines


def build_system_context(user: str, mode: str = MODE_ORDINARY) -> str:
    """Render the dynamic Kyrex Chat system context for one turn.

    *mode* only shapes the wording (MODE_ORDINARY / MODE_WORKSPACE / MODE_BOT);
    it never selects a routing path or changes a capability gate. The visible
    Bot roster is included for the ordinary and workspace modes — the
    coordinator cases — and lists only Bots visible to *user*, with UI-safe
    metadata only. MODE_BOT omits the roster: a Bot owns its own prompt and
    never uses this builder.
    """
    parts: list[str] = [
        "You are Kyrex Chat, the conversational assistant product from Kyrex."
    ]

    if mode == MODE_WORKSPACE:
        parts.append(
            "Current mode: workspace-attached read-only chat. "
            "You may inspect the attached workspace (read, list, and search "
            "files) to answer the user, but you cannot edit files, run "
            "commands, execute tasks, or approve actions — this mode is "
            "strictly read-only.")
    elif mode == MODE_BOT:
        parts.append(
            "Current mode: Bot-bound chat. This conversation runs as a Bot "
            "with its own identity and authority boundary; follow the Bot's "
            "own instructions for this conversation.")
    else:
        parts.append(
            "Current mode: ordinary chat. "
            "You can answer questions, explain how Kyrex works, and help the "
            "user coordinate the Bots available to them. "
            "You cannot edit files, execute tasks, run commands, or approve "
            "actions in this mode — those require starting a Bot-bound "
            "conversation from the Bot picker.")

    # Conversational style. This is presentation guidance only — it never
    # selects a route or changes a capability gate. It exists so a bare
    # greeting reads like a person answered, instead of eliciting the
    # capability/mode roster below as an inventory.
    parts.append(
        "Conversational style: talk like a person. Answer briefly and "
        "naturally. For a simple greeting or small talk, reply with one short "
        "friendly line — for example \"Hey — what would you like to work on?\" "
        "— and nothing else. Do NOT respond to a greeting with a list of your "
        "capabilities, tools, memory, the file tree, providers, or modes. "
        "Mention memory, .px_docs, file-tree visibility, providers, or "
        "internal tools ONLY when the user explicitly asks about them. Only "
        "state that you inspected files, memory, or a workspace, or ran a "
        "tool, when a tool call actually succeeded in this turn — never claim "
        "to have looked at something you did not. The facts below are "
        "reference material for when they are relevant, not a script to "
        "recite.")

    parts.append(
        "Kyrex Chat modes: ordinary chat (conversation only, no actions); "
        "workspace-attached read-only chat (inspect an attached workspace, "
        "never modify it); and Bot-bound chat (a Bot acts with its own "
        "identity and authority boundary). Only a Bot-bound conversation with "
        "the appropriate capabilities can perform edits, tasks, or approvals. "
        "Do not recite this list unless the user asks what Kyrex can do.")

    # The coordinator roster belongs to the non-Bot modes: an ordinary turn
    # and a workspace-attached turn can both help the user coordinate Bots. A
    # Bot-bound turn (MODE_BOT) never uses this builder at all.
    if mode != MODE_BOT:
        lines = _bot_roster_lines(user)
        if lines:
            parts.append(
                "Available Bots for this user (from the Kyrex Bot registry) — "
                "reference only, to help the user coordinate them WHEN THEY "
                "ASK; never list or describe these unprompted. Starting a "
                "conversation with one is done from the Bot "
                "picker:\n" + "\n".join(lines))
        else:
            parts.append(
                "No Bots are currently available to this user. You can still "
                "answer questions and explain how Bots work; the Bot picker "
                "starts a Bot-bound conversation once a Bot is available.")

    return "\n\n".join(parts)


def build_coordinator_context(owner: str, coordinator_bot: dict) -> str:
    """Safe, per-turn context for a coordinator ("Chief of Staff") Bot.

    Gives the coordinator a CLEAR roster of the SAME owner's other Bots — id,
    name, status, role, capabilities, model, availability only; never Rift
    paths, policies, prompts, provider references, or credentials — and states
    the delegation contract. Rendered fresh every turn, so a roster/status
    change is reflected on the next turn of the same engine process.

    The roster and every capability label come from ``delegation`` (which reuses
    the registry and host tier table), so this can never drift from what the
    host will actually permit.
    """
    bot_id = str((coordinator_bot or {}).get("id") or "")
    try:
        targets = delegation.visible_targets(owner, exclude_bot_id=bot_id)
        roster_error = ""
    except delegation.DelegationError as exc:
        targets, roster_error = [], str(exc)

    lines: list[str] = []
    for t in targets:
        bits = [
            f"id: {t.get('id')}",
            f"name: \"{t.get('name')}\"",
            f"status: {t.get('status')}",
            f"role: {t.get('role')}",
            f"available: {'yes' if t.get('available') else 'no'}",
        ]
        caps = ", ".join(t.get("capabilities") or [])
        if caps:
            bits.append(f"capabilities: {caps}")
        if t.get("model"):
            bits.append(f"model: {t.get('model')}")
        lines.append("- " + " | ".join(bits))

    if roster_error:
        roster = f"(roster unavailable: {roster_error})"
    elif lines:
        roster = "\n".join(lines)
    else:
        roster = "(no other Bots are available to delegate to)"

    return (
        "You are a COORDINATOR Bot (the owner's Chief of Staff). You may "
        "delegate a task to another Bot the SAME owner owns by calling "
        "delegate_task(target_bot_id, task). Delegated work runs as an "
        "ordinary task under the TARGET Bot, which stays authoritative for its "
        "own model, workspace, policy, browser access, and approvals — you "
        "never run its work here and you cannot approve or deny its actions; "
        "any approval is the owner's, through the target task. Delegation is "
        "ONE LEVEL ONLY: a delegated Bot cannot delegate further, and you may "
        "not delegate across owners.\n\n"
        "To answer questions about work you already delegated — for example "
        "\"did it finish?\" — call delegation_status. It reads the existing "
        "delegation record and returns the CURRENT safe status (queued, "
        "running, awaiting_approval, done, failed, cancelled, rejected) for "
        "the delegations you created. Pass delegation_id to ask about one, or "
        "omit it to list them. Answer from that result; never guess or invent "
        "a status. If a delegation shows awaiting_approval, tell the owner the "
        "TARGET Bot is awaiting THEIR approval — you cannot approve or deny "
        "it.\n\n"
        "Answer each user turn in ONE concise reply. Do not restate the same "
        "answer several times or re-announce a result you have already "
        "reported.\n\n"
        "Bots available to delegate to (safe metadata only):\n" + roster
    )


# ── engine invocation (streaming) ──────────────────────────────────

def build_messages(history: list[dict], user_content: str,
                   system_context: Optional[str] = None) -> list[dict]:
    """Assemble the provider message list, mirroring the existing chat path:
    a leading system prompt, prior turns, then the new user turn.

    *system_context* overrides the static :data:`CHAT_SYSTEM_PROMPT` for this
    turn (the per-turn coordinator context). When omitted, the static prompt
    is used unchanged — the pre-existing behavior.
    """
    messages = [{"role": "system",
                 "content": system_context or CHAT_SYSTEM_PROMPT}]
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
                                    conversation_id, cancel_event,
                                    steps=None, mode=None, calendar_intent=None):
    """Submit a Bot turn to a durable executor task and stream its events.

    *steps* is None for the writable repo path (task_text == the user's
    content) and a validated navigate/read list for the Browser Bot path
    (task_text compiled from the steps). *mode="glofox"* submits the ONE
    pinned Glofox schedule command through the same durable path, and
    *mode="level6"* the ONE pinned Level 6 weekly command — for each the
    only caller passes the fixed ``dev_bot.GLOFOX_SCHEDULE_COMMAND`` /
    ``dev_bot.LEVEL6_WEEKLY_COMMAND`` text; no other support exists. All
    are ordinary durable tasks on the same CloudTaskStore -> serve.run_task
    path; only the submission differs.

    Reuses the EXISTING durable task event stream (flux.py) and maps it to
    Chat control frames. The terminal frame is produced here from the task's
    authoritative row, exactly like the provider/engine paths produce their
    terminal ``status`` frame.
    """
    from task_store import CloudTaskStore
    import flux as flux_module
    store = _task_store()
    try:
        if mode == "level6":
            # Pinned Level 6 weekly MVP: the ONE server-defined command.
            # Submission + all gating (exact task text, running Bot, exact
            # Level 6 Weekly grant) is dev_bot's; serve.run_task re-checks.
            # The capture reuses the owner's persistent ``browser-bot``
            # Browser Host binding through serve's already-implemented
            # dispatch — this only submits the task.
            task_id = dev_bot.submit_level6_task(
                user, bot, dev_bot.LEVEL6_WEEKLY_COMMAND, store=store,
                conversation_id=conversation_id)
        elif mode == "level6_calendar":
            # Pinned Level 6 calendar read: the ONE server-defined command.
            # Submission + all gating (exact task text, running Bot, exact
            # Level 6 Calendar grant — cal:list + glofox:read and nothing
            # else) is dev_bot's; serve.run_task re-checks and runs the read
            # IN-PROCESS against the owner-scoped encrypted connector store.
            task_id = dev_bot.submit_level6_calendar_task(
                user, bot, dev_bot.LEVEL6_CALENDAR_COMMAND, store=store,
                conversation_id=conversation_id)
        elif mode == "level6_message":
            task_id = dev_bot.submit_level6_message_task(
                user, bot, dev_bot.LEVEL6_MESSAGE_COMMAND, store=store,
                conversation_id=conversation_id)
        elif mode == "glofox":
            # Pinned Level 6 schedule read: the ONE server-defined command.
            # Submission + all gating (exact task text, running Bot, exact
            # glofox:read grant) is dev_bot's; serve.run_task re-checks.
            task_id = dev_bot.submit_glofox_task(
                user, bot, dev_bot.GLOFOX_SCHEDULE_COMMAND, store=store,
                conversation_id=conversation_id)
        elif mode == "calendar":
            # Calendar Reader: one of the three pinned commands. Submission +
            # all gating (exact task text, running Bot, exact cal:list grant,
            # owner scope) is dev_bot's; serve.run_task re-checks and runs the
            # read in-process against the owner-scoped connector store.
            task_id = dev_bot.submit_calendar_task(
                user, bot, str(user_content or "").strip(), store=store,
                conversation_id=conversation_id)
        elif steps is not None:
            # Browser Bot turn: a pre-validated, bounded navigate/read
            # operation list. Submission + all gating is dev_bot's.
            task_id = dev_bot.submit_browser_task(
                user, bot, steps, store=store,
                conversation_id=conversation_id)
        elif calendar_intent is not None:
            # Calendar Writer turn: an ALREADY-normalised, validated create
            # intent (cal_writer) submitted to the writer bridge; the executor
            # holds the mandatory confirmation gate before any provider call.
            task_id = dev_bot.submit_calendar_writer_task(
                user, bot, json.dumps(calendar_intent), store=store,
                conversation_id=conversation_id)
        else:
            task_id = dev_bot.submit_bot_task(
                user, bot, user_content, store=store,
                conversation_id=conversation_id)
    except dev_bot.DevBotError as exc:
        raise ChatUnavailable(str(exc))
    # The result's message identity is the durable task id — already
    # unique per turn and well known to the store, so a repeated viewer or a
    # re-driven turn can never append the same result text twice. (Computed
    # AFTER the submission produces the id.)
    turn_writable_identity = f"task-{task_id}-result"

    yield {"type": "conversation", "conversation_id": conversation_id}

    loop = asyncio.get_running_loop()
    q = _queue.Queue()
    sentinel = object()
    final_result = None
    # Set once this generator is abandoned (client disconnect / reload) before
    # the durable task reached a terminal state. The pump observes it via its
    # bounded queue timeout and exits, so a reload retires the viewer instead
    # of parking a thread on q.get for up to BOT_TASK_STREAM_MAX_SECONDS.
    abandoned = threading.Event()

    def pump():
        try:
            for event in flux_module.stream_events(
                store, task_id,
                after_event_id=0,
                max_seconds=BOT_TASK_STREAM_MAX_SECONDS,
            ):
                # Durable-task events must still be yielded even while the
                # viewer is absent: a re-attached viewer replays them from the
                # store, and a full in-process queue would otherwise block the
                # pump. Bounded put keeps the pump responsive to abandonment.
                while not abandoned.is_set():
                    try:
                        q.put(event, True, BOT_TASK_POLL_SECONDS)
                        break
                    except _queue.Full:  # pragma: no cover — unbounded in practice
                        continue
                if abandoned.is_set():
                    # The task keeps running: we drop only the LOCAL copies of
                    # its events. The store is the single source of truth and a
                    # later viewer replays them by cursor.
                    return
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
            # Presentation boundary: the executor-path formatter echoes the
            # engine's final_response, which carries the same internal control
            # markers ("[Task Complete: …]"). Strip them so a writable-Bot turn
            # reads as prose, not telemetry. Real errors are left intact.
            content = sanitize_assistant_text(content)
            if content:
                conv_now = get_conversation(user, conversation_id) or conv
                _append_message(user, conv_now, "assistant", content,
                                identity=turn_writable_identity)
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
        # A Chat SSE connection is only a viewer of this durable task.
        # Changing conversations aborts the browser request, which must not
        # cancel repository work. Explicit Stop still sets cancel_event above
        # and requests cancellation through the normal task-store path.
        #
        # Abandonment therefore signals the LOCAL pump to stop and retires this
        # viewer thread; it never calls store.request_cancel and never yields a
        # terminal frame. The task continues to its own conclusion in the
        # shared worker, and the conversation's persisted last_task_id lets a
        # reload re-attach as a fresh viewer.
        abandoned.set()


def _safe_result_summary(store, task_id: str) -> tuple[str, str]:
    """Return ``(final_status, sanitized_summary)`` for a target task.

    The summary reuses ``serve.format_result`` (the executor-path formatter the
    writable-Bot Chat path already uses) and is bounded. No provider key,
    header, token, Rift path, prompt, or approval secret is included because
    none of those are ever present on a task's result payload.
    """
    task = store.get(task_id) or {}
    status = str(task.get("status") or "")
    result = task.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            result = {}
    if not isinstance(result, dict):
        result = {}
    summary = ""
    if result:
        try:
            summary = serve.format_result(result)
        except Exception:
            summary = str(result.get("final_response") or "")
    # Presentation boundary: the formatter echoes the engine's final_response,
    # which carries the same internal control markers. Strip them so a
    # delegated result reads as prose; real errors are left intact.
    summary = sanitize_assistant_text(summary)
    return status, (summary or "")[:4000]


# ── delegated-work reconciliation + one-time relay ────────────────────
# A delegation row mirrors its linked target task's lifecycle, but the TARGET
# task is authoritative. These helpers read the EXISTING task record so a
# status query is answered from durable state (never from memory or a guess),
# and so a terminal result is relayed into the parent coordinator conversation
# EXACTLY ONCE — even when the target finishes while nobody is watching.

_TERMINAL_TASK_STATUSES = ("done", "failed", "cancelled")
_DELEGATION_OF_TASK_STATUS = {
    "queued": "queued",
    "running": "running",
    "awaiting_approval": "awaiting_approval",
    "done": "done",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _reconcile_delegation(store, rec: dict) -> dict:
    """Return *rec* reconciled against its linked target task (authoritative).

    * A non-terminal delegation is brought up to date from the task's live
      status (queued / running / awaiting_approval).
    * A delegation whose task has reached a terminal state is finalized with
      the SANITIZED summary from the existing formatter. Finalization is
      idempotent: an already-finalized row with a summary is left untouched so
      ``finished_at`` does not churn on every poll.

    NEVER approves, denies, cancels, or otherwise mutates the target task — it
    reads the task and mirrors its lifecycle onto the delegation row.
    """
    if not rec:
        return rec
    task_id = rec.get("task_id")
    if not task_id:
        return rec
    task = store.get(task_id) or {}
    status = str(task.get("status") or "")
    mapped = _DELEGATION_OF_TASK_STATUS.get(status)
    if mapped is None:
        return rec
    delegation_id = rec.get("delegation_id")

    if status in _TERMINAL_TASK_STATUSES:
        already = (
            str(rec.get("status") or "") == mapped
            and bool(rec.get("finished_at"))
            and bool(str(rec.get("result_summary") or "").strip())
        )
        if already:
            return rec
        _, summary = _safe_result_summary(store, task_id)
        store.set_delegation_result(delegation_id, summary, status=mapped)
        return store.get_delegation(delegation_id) or rec

    if str(rec.get("status") or "") != mapped:
        store.set_delegation_status(delegation_id, mapped)
        return store.get_delegation(delegation_id) or rec
    return rec


def coordinator_delegation_statuses(
    owner: str,
    coordinator_bot_id: Optional[str],
    conversation_id: Optional[str] = None,
    *,
    delegation_id: Optional[str] = None,
) -> list[dict]:
    """Owner- and coordinator-scoped CURRENT status of delegated work.

    The host side of the coordinator ``delegation_status`` tool. Reads the
    EXISTING delegation/task records, reconciles each against its linked task,
    and returns ONLY safe public views. A delegation belonging to another owner
    or another coordinator is never returned — a single-id query for one is
    reported as an empty result rather than leaking its existence. Read-only:
    this never approves, denies, or cancels anything.
    """
    store = _task_store()
    owner = str(owner or "").strip()
    if not owner:
        return []
    did = str(delegation_id or "").strip()
    if did:
        rec = delegation.fetch_delegation(
            owner, did, store=store, coordinator_bot_id=coordinator_bot_id)
        if rec is None:
            return []
        recs = [rec]
    else:
        recs = delegation.owner_scoped_delegations(
            owner, store=store, coordinator_bot_id=coordinator_bot_id,
            conversation_id=conversation_id, limit=25)
    out: list[dict] = []
    for rec in recs:
        rec = _reconcile_delegation(store, rec) or rec
        out.append(delegation.public_view(rec))
    return out


def _delegation_notice(target_bot_id: str, status: str, summary: str) -> str:
    """One concise, safe line announcing a terminal delegated result."""
    target = str(target_bot_id or "target Bot")
    summary = str(summary or "").strip()
    if status == "done":
        return f"[Delegated to {target}] {summary or 'completed.'}".strip()
    if status == "cancelled":
        return f"[Delegated to {target}] cancelled: {summary or 'was cancelled.'}".strip()
    if status == "rejected":
        return f"[Delegated to {target}] rejected: {summary or 'was rejected.'}".strip()
    return f"[Delegated to {target}] failed: {summary or 'failed.'}".strip()


def sync_delegated_work(user: str, conversation_id: str) -> dict:
    """Owner-scoped sync of a conversation's delegated work.

    Reconciles every delegation linked to *conversation_id* against its target
    task and relays each terminal result into the conversation EXACTLY ONCE
    (``mark_delegation_relayed`` is the durable compare-and-set, so the
    announce-once guarantee survives reloads, reconnects, and repeated polls).
    Returns the reconciled safe views plus the notices relayed by THIS call.

    Idempotent: a second call returns the same views and relays nothing new.
    """
    store = _task_store()
    recs = delegation.owner_scoped_delegations(
        user, store=store, conversation_id=conversation_id, limit=25)
    views: list[dict] = []
    relayed: list[dict] = []
    for rec in recs:
        rec = _reconcile_delegation(store, rec) or rec
        view = delegation.public_view(rec)
        task_id = rec.get("task_id")
        if task_id and str(rec.get("status")) == "awaiting_approval":
            pending = store.get_pending_approval(task_id) or {}
            if pending:
                # Owner-facing controls need only the safe display fields and
                # exact task id. Never expose approval tokens or raw payloads.
                view["approval"] = {
                    "task_id": task_id,
                    "tier": pending.get("tier"),
                    "summary": pending.get("summary") or "",
                    "detail": pending.get("detail") or "",
                }
        if delegation.is_terminal(rec.get("status")):
            did = rec.get("delegation_id")
            if store.mark_delegation_relayed(did):
                target = rec.get("target_bot_id")
                summary = rec.get("result_summary") or rec.get("error") or ""
                notice = _delegation_notice(
                    target, str(rec.get("status") or ""), summary)
                conv_now = get_conversation(user, conversation_id)
                if conv_now is not None:
                    _append_message(user, conv_now, "assistant", notice[:4000])
                    _write(user, conv_now)
                view["relayed"] = True
                relayed.append({
                    "delegation_id": did,
                    "target_bot_id": target,
                    "status": rec.get("status"),
                    "summary": summary,
                    "message": notice[:4000],
                })
        views.append(view)
    return {"delegations": views, "relayed": relayed}


async def _stream_delegated_work(user, conv, conversation_id):
    """Yield the CURRENT safe status of this conversation's delegated work.

    Coordinator turns only. For each durable delegation linked to
    *conversation_id*, this reconciles the row against its target task and
    yields:

      * ``approval_request`` — when the TARGET task has a pending approval,
        WITHOUT the approval token (a delegated approval is the OWNER's to
        resolve through the target task; the coordinator may only report it);
      * ``delegation`` — the safe delegation view (identities + status);
      * ``delegation_result`` — the sanitized final summary, once terminal.

    It deliberately does NOT append an assistant message and does NOT tail the
    target to completion: a coordinator turn answers the user ONCE, and the
    Delegated Work card follows the target through the UI's bounded polling
    (``sync_delegated_work`` performs the one-time relay when the work becomes
    terminal). This removes the duplicate/paraphrased coordinator replies and
    keeps a long-running target from holding the turn open.
    """
    store = _task_store()
    recs = delegation.owner_scoped_delegations(
        user, store=store, conversation_id=conversation_id, limit=25)
    for rec in recs:
        rec = _reconcile_delegation(store, rec) or rec
        delegation_id = rec.get("delegation_id")
        target_bot_id = rec.get("target_bot_id")
        task_id = rec.get("task_id")

        # A pending TARGET approval is REPORTED (never resolved here) so the
        # coordinator can say "the target is awaiting your approval". The token
        # is never relayed: approving is the owner's, through the target task.
        if task_id and str(rec.get("status")) == "awaiting_approval":
            pending = store.get_pending_approval(task_id) or {}
            if pending:
                yield {
                    "type": "approval_request",
                    "task_id": task_id,
                    "approval_id": pending.get("approval_id"),
                    "tier": pending.get("tier"),
                    "summary": pending.get("summary") or "",
                    "detail": pending.get("detail") or "",
                    "delegation_id": delegation_id,
                    "target_bot_id": target_bot_id,
                }

        yield {"type": "delegation", "delegation": delegation.public_view(rec)}

        if delegation.is_terminal(rec.get("status")):
            yield {
                "type": "delegation_result",
                "delegation_id": delegation_id,
                "target_bot_id": target_bot_id,
                "status": rec.get("status"),
                "summary": rec.get("result_summary") or rec.get("error") or "",
            }


async def stream_chat(
    user: str,
    conversation_id: str,
    user_content: str,
    cancel_event: Optional[asyncio.Event] = None,
    workspace_id=_WORKSPACE_UNSET,
    request_id: Optional[str] = None,
) -> AsyncIterator[dict]:
    # ``request_id`` is the turn's existing identity (the same id the cancel
    # registry already keys on). The user message and the assistant
    # finalization are persisted keyed on it, so a retried POST, replay, or
    # repeated terminal delivery can never double-commit either side of the
    # turn. Suffixes keep the two identities distinct within one turn.
    turn_user_identity = f"turn-{request_id}-user" if request_id else None
    turn_assistant_identity = (
        f"turn-{request_id}-assistant" if request_id else None)
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
    # ── per-Bot LLM configuration ──────────────────────────────────────
    # provider_cfg is resolved per branch, NEVER eagerly from the environment:
    # a Bot-bound conversation is served ONLY with its own resolved provider
    # profile (resolved in the binding block below, fail-closed), so it can
    # never fall back to KYREX_PROVIDER / KYREX_API_KEY / KYREX_MODEL. A
    # non-Bot conversation keeps the existing provider resolution.
    provider_cfg = None
    bot_binding = conv.get("bot_id") or None
    if not bot_binding:
        provider_cfg = _resolve_provider(
            conv.get("provider"), conv.get("model"), user=user)
        if not provider_cfg["model"]:
            raise ChatUnavailable("KYREX_MODEL is not configured")
        if not provider_cfg["api_key"]:
            raise ChatUnavailable(
                f"provider '{provider_cfg['provider']}' is not configured")

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
    # Three-way selected-Bot route (assigned only for Bot-bound turns;
    # non-Bot conversations are always "engine" below).
    route = "engine"
    coordinator_ctx = None
    if bot_binding:
        try:
            bot = resolve_bot_for_user(user, bot_binding)
        except (BotUnavailable, BotRegistryError) as exc:
            raise ChatUnavailable(str(exc))
        resolved_ws = Path(str(bot["rift"])).resolve()
        # Per-Bot LLM configuration (fail-closed). The Bot stores only an
        # owner-scoped profile reference plus the exact model; the profile's
        # provider, base URL, API key and approved headers live in the
        # encrypted per-user store and are resolved here. A Bot whose
        # configuration is missing, references a foreign/missing profile, or
        # has a model outside its profile fails the turn closed — it is NEVER
        # served with the host's KYREX_PROVIDER / KYREX_API_KEY / KYREX_MODEL.
        # Resolved with the Bot's OWNER (the owner-scoped store), matching the
        # executor path in serve.py. resolve_bot_for_user has already proven
        # the requesting user may bind this Bot.
        bot_owner = str(bot.get("owner") or "").strip()
        try:
            llm = bot_provider.resolve_bot_provider(bot_owner, bot)
        except bot_provider.BotProviderError as exc:
            raise ChatUnavailable(str(exc))
        provider_cfg = {
            "provider": llm["provider"],
            "profile": llm["profile"],
            "model": llm["model"],
            "api_key": llm["api_key"],
            "base_url": llm["base_url"],
            "headers": llm["headers"],
            # Marks the config as Bot-profile-sourced so the engine session
            # scrubs ambient base URLs — no silent global fallback.
            "bot_profile": True,
        }
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
            # The registry model, carried for identity/telemetry. The resolved
            # provider config (provider_cfg) is authoritative for the actual
            # provider + model — see EngineSession's bot_profile branch.
            "model": bot.get("model") or "",
            "system_prompt": bot.get("system_prompt") or "",
            "allowed_tools": caps["tools"],
        }
        # Coordinator capability (owner-scoped, explicitly granted). When the
        # Bot holds ``bot:delegate``, this conversation may delegate work to the
        # owner's other Bots. The context carries ONLY the owner and the
        # coordinator's own registry record; the target and all of its
        # authority are resolved host-side at delegation time.
        if serve.coordinator_granted(bot):
            coordinator_ctx = {
                "owner": bot_owner,
                "bot": bot,
                "conversation_id": conversation_id,
            }
            bot_cfg["delegation"] = coordinator_ctx
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
        # Three-way selected-Bot route:
        #   repo   — a write-capable Developer Bot: the EXISTING repo
        #            executor path, byte-identical to before.
        #   browser— an explicitly bound Browser Bot (non-empty allowlist,
        #            live host binding, NOT write-capable): the EXISTING
        #            durable browser path via dev_bot.submit_browser_task.
        #   engine — everything else keeps the ordinary read-only engine
        #            session. A browser route is only ever ADDED; the
        #            registry fault / missing binding case falls to engine
        #            only for non-browser Bots (browser_route_ready returns
        #            False for a write-capable Bot, so no widening ever
        #            happens).
        try:
            repo_route = dev_bot.is_writable_bot_policy(bot.get("policy"))
        except Exception:
            repo_route = False
        try:
            browser_route = (not repo_route
                             and dev_bot.browser_route_ready(bot))
        except Exception:
            browser_route = False
        # Fourth route: the EXACT owner-facing Level 6 schedule command.
        # Only the ONE pinned text reaches it — on a Bot holding the exact
        # server-defined glofox:read grant. Anything else a glofox-capable
        # Bot sends stays on the ordinary engine path; no widening.
        try:
            glofox_route = (
                not repo_route and not browser_route
                and dev_bot.glofox_route_ready(bot)
                and str(user_content or "").strip()
                == dev_bot.GLOFOX_SCHEDULE_COMMAND)
        except Exception:
            glofox_route = False
        # Fifth route: the EXACT owner-facing Level 6 weekly command. Checked
        # BEFORE repo/browser/glofox so the ONE pinned text ``level6: weekly``
        # is intercepted on a Bot holding the exact Level 6 Weekly grant and
        # can never fall through to the LLM/engine or the writable executor.
        # A suffix, an alternate URL, a date, or any other variant fails the
        # byte-exact comparison and stays on the ordinary path; a write-capable
        # Bot never holds the grant, so repo routing is unaffected.
        try:
            level6_route = (
                dev_bot.level6_route_ready(bot)
                and str(user_content or "").strip()
                == dev_bot.LEVEL6_WEEKLY_COMMAND)
        except Exception:
            level6_route = False
        # Route for the EXACT owner-facing Level 6 calendar command: the
        # deterministic ``level6: calendar`` read on a Bot holding the exact
        # Level 6 Calendar grant. Checked alongside level6 (weekly) so the
        # pinned text is intercepted BEFORE any LLM/repo path and can never
        # fall through to the engine/LLM, the writable executor, or the
        # browser.
        try:
            level6_calendar_route = (
                (dev_bot.level6_calendar_route_ready(bot)
                 or dev_bot.calendar_bot_route_ready(bot))
                and str(user_content or "").strip()
                == dev_bot.LEVEL6_CALENDAR_COMMAND)
        except Exception:
            level6_calendar_route = False
        try:
            level6_message_route = (
                dev_bot.level6_message_route_ready(bot)
                and str(user_content or "").strip()
                == dev_bot.LEVEL6_MESSAGE_COMMAND)
        except Exception:
            level6_message_route = False
        # Calendar Reader: the three byte-exact commands, routed on a Bot
        # holding the exact cal:list grant. Checked alongside level6/glofox so
        # the pinned text is intercepted BEFORE any LLM/repo path. Any OTHER
        # message in the reserved ``calendar:`` namespace fails closed below.
        try:
            _calendar_text = str(user_content or "").strip()
            calendar_route = (
                dev_bot.calendar_route_ready(bot)
                and _calendar_text in dev_bot.CALENDAR_COMMANDS)
        except Exception:
            calendar_route = False
        try:
            calendar_unsupported = (
                str(user_content or "").strip().lower().startswith("calendar:")
                and str(user_content or "").strip()
                not in dev_bot.CALENDAR_COMMANDS)
        except Exception:
            calendar_unsupported = False
        # Calendar WRITER: a Bot holding the EXACT, distinct cal:create write
        # grant routes EVERY turn through the writer bridge, which normalises
        # the request into ONE safe create intent and submits it to the
        # confirmation-gated executor. Ambiguous/unsupported requests are
        # answered with usage and NO task is created.
        try:
            calendar_write_route = dev_bot.calendar_writer_route_ready(bot)
        except Exception:
            calendar_write_route = False
        route = ("calendar" if calendar_route
                 else "calendar_unsupported" if calendar_unsupported
                 else "level6" if level6_route
                 else "level6_message" if level6_message_route
                 else "level6_calendar" if level6_calendar_route
                 else "glofox" if glofox_route
                 else "calendar_write" if calendar_write_route
                 else "repo" if repo_route
                 else "browser" if browser_route
                 else "engine")

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
        # Ordinary Kyrex Chat (no Bot, no workspace): inject the dynamic
        # coordinator context — identity, current mode, capability boundary,
        # and the user's visible Bot roster. Bot-bound and workspace-attached
        # turns never reach here; they keep their own prompts and authority
        # boundaries.
        messages = build_messages(
            history, user_content,
            system_context=build_system_context(user, MODE_ORDINARY))

    # Identity-keyed user message: one POST == one stored user turn. A retried
    # request carrying the same request_id (or a replayed turn) is a no-op.
    _append_message(user, conv, "user", user_content,
                    identity=turn_user_identity)
    _write(user, conv)

    cancel = cancel_event if cancel_event is not None else asyncio.Event()

    if route == "repo":
        async for frame in _stream_writable_bot_task(
                user, conv, bot, user_content, conversation_id, cancel):
            yield frame
        return

    if route == "browser":
        # Deliberately explicit, safe first UX. Only `read <url>` and
        # `browse <url>` (one URL, allowlisted domain) are accepted; anything
        # ambiguous or unsupported is answered with a short usage message —
        # the turn is stored in the transcript like any assistant reply, and
        # NO task is EVER submitted on a guess.
        try:
            steps = dev_bot.parse_browser_request(user_content)
        except dev_bot.DevBotError as exc:
            content = sanitize_assistant_text(str(exc)) or str(exc)
            _append_message(user, conv, "assistant", content,
                            identity=f"{turn_user_identity}-usage")
            _write(user, conv)
            yield {"type": "status", "status": "complete", "content": content}
            return
        async for frame in _stream_writable_bot_task(
                user, conv, bot, user_content, conversation_id, cancel,
                steps=steps):
            yield frame
        return

    if route == "level6":
        # Pinned Level 6 weekly MVP: the EXACT `level6: weekly` command routed
        # here only for a running, non-write-capable Bot holding the exact
        # Level 6 Weekly grant (level6_route above, checked FIRST). No
        # repository steps are passed; the durable submission + all gating
        # live in dev_bot.submit_level6_task and are re-checked in
        # serve.run_task, which drives the pinned Facebook capture through the
        # owner's persistent ``browser-bot`` Browser Host binding and the
        # pinned Glofox read.
        async for frame in _stream_writable_bot_task(
                user, conv, bot, user_content, conversation_id, cancel,
                mode="level6"):
            yield frame
        return

    if route == "level6_calendar":
        # Pinned Level 6 calendar read: the EXACT `level6: calendar` command
        # routed here only for a running, non-write-capable Bot holding the
        # exact Level 6 Calendar grant (level6_calendar_route above). No
        # repository steps, no browser; the durable submission + all gating
        # live in dev_bot.submit_level6_calendar_task and are re-checked in
        # serve.run_task, which reads the OWNER's primary calendar through
        # the owner-scoped encrypted connector store and joins it with the
        # pinned Glofox trusted-date read.
        async for frame in _stream_writable_bot_task(
                user, conv, bot, user_content, conversation_id, cancel,
                mode="level6_calendar"):
            yield frame
        return

    if route == "level6_message":
        async for frame in _stream_writable_bot_task(
                user, conv, bot, user_content, conversation_id, cancel,
                mode="level6_message"):
            yield frame
        return

    if route == "glofox":
        # Pinned Level 6 schedule read: the EXACT `glofox: schedule` command
        # routed here only for a running, non-write-capable Bot holding the
        # exact glofox:read grant (glofox_route above). No repository steps
        # are passed; the durable submission + all gating live in
        # dev_bot.submit_glofox_task and are re-checked in serve.run_task.
        async for frame in _stream_writable_bot_task(
                user, conv, bot, user_content, conversation_id, cancel,
                mode="glofox"):
            yield frame
        return

    if route == "calendar_unsupported":
        # A message in the reserved ``calendar:`` namespace that is NOT one of
        # the three byte-exact commands. Fail closed with a usage message --
        # NEVER the LLM/repo path.
        content = ("Unsupported calendar command. Supported (exact): "
                   "calendar: today, calendar: tomorrow, calendar: week")
        _append_message(user, conv, "assistant", content,
                        identity=f"{turn_user_identity}-calendar-usage")
        _write(user, conv)
        yield {"type": "status", "status": "complete", "content": content}
        return

    if route == "calendar":
        # Calendar Reader: one of the three byte-exact commands on a running,
        # non-write-capable Bot holding the exact cal:list grant. No steps;
        # the durable submission + all gating live in
        # dev_bot.submit_calendar_task and are re-checked in serve.run_task,
        # which runs the reader IN-PROCESS against the OWNER-SCOPED encrypted
        # connector store (never a global refresh token).
        async for frame in _stream_writable_bot_task(
                user, conv, bot, user_content, conversation_id, cancel,
                mode="calendar"):
            yield frame
        return

    if route == "calendar_write":
        # Calendar Writer: normalise the owner's request into ONE safe create
        # intent DETERMINISTICALLY (never model output), then submit it to the
        # confirmation-gated executor. An ambiguous/unsupported request is
        # answered with usage and NOTHING is created -- no task row, no call.
        try:
            intent = cal_writer.parse_create_request(user_content)
        except cal_writer.CalendarWriterError as exc:
            content = sanitize_assistant_text(str(exc)) or str(exc)
            _append_message(user, conv, "assistant", content,
                            identity=f"{turn_user_identity}-calendar-writer-usage")
            _write(user, conv)
            yield {"type": "status", "status": "complete", "content": content}
            return
        async for frame in _stream_writable_bot_task(
                user, conv, bot, user_content, conversation_id, cancel,
                calendar_intent=intent):
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
                # Refresh the coordinator identity every turn so a reused
                # engine session always carries the LIVE coordinator context
                # (never a stale spawn-time snapshot), and refresh the safe
                # peer roster the same way (a Bot started/stopped between turns
                # is reflected immediately).
                if coordinator_ctx is not None:
                    engine_session.delegation_ctx = coordinator_ctx
                    engine_session.surface_context = build_coordinator_context(
                        user, coordinator_ctx.get("bot") or {})
            else:
                # Workspace-attached, non-Bot conversation: hand the engine the
                # CURRENT Kyrex Chat identity / read-only capability context.
                # Set immediately before the turn (not at spawn), so a reused
                # engine session always reflects the live safe Bot roster. A
                # Bot-bound session keeps its own prompt and never gets this.
                engine_session = _get_engine_session(
                    user, conversation_id, resolved_ws, bot_cfg, provider_cfg)
                engine_session.surface_context = \
                    build_system_context(user, MODE_WORKSPACE)
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
        # Pure-chat turn: use the per-conversation provider config resolved
        # above (from the persisted conv["provider"]/conv["model"]) — never
        # re-resolve environment defaults here, or a user's saved provider
        # selection is silently ignored for non-repo conversations.
        cfg = provider_cfg if provider_cfg is not None else _resolve_provider()
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

        # Presentation boundary: strip internal control markers (and collapse
        # engine rounds) from what the user sees AND what is persisted, so the
        # markers can never become assistant message text. Error text is not
        # touched (a real failure stays visible). Task-completion semantics
        # live in the engine and are unaffected by this sanitization.
        final_text = sanitize_assistant_text(final_text)

        # Persistence: only a successfully-completed turn persists an assistant
        # message. Failed and cancelled streams are never recorded as a completed
        # assistant reply (no duplicate/false assistant messages).
        if outcome is _SENTINEL and final_text:
            # Finalization is idempotent under the turn identity: a repeated
            # final event / retried turn cannot append the answer twice.
            conv_now = get_conversation(user, conversation_id) or conv
            _append_message(user, conv_now, "assistant", final_text,
                            identity=turn_assistant_identity)
            _write(user, conv_now)

        # Terminal status frame. An async generator cannot ``return`` a value,
        # so the terminal outcome is yielded as the final control frame, which
        # the caller (_drive_stream) maps to the matching explicit SSE event.
        # This yield lives INSIDE the try: once finalization has begun (the
        # finally below / GeneratorExit from aclose) no further yield is legal,
        # and a close must never leave the async_generator_athrow finalizer
        # task waiting on this stream.
        # Coordinator turns: relay this conversation's delegated work — target
        # status, target-owned approval prompts (token stripped), and the
        # sanitized final result — back into the coordinator conversation. Only
        # on a clean turn; a failed/cancelled coordinator turn reports its own
        # terminal state without pretending the delegated work ran.
        if coordinator_ctx is not None and outcome is _SENTINEL:
            async for frame in _stream_delegated_work(
                    user, conv, conversation_id):
                yield frame

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
