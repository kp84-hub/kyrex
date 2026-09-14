"""chat_api.py — HTTP/SSE surface for Kyrex Chat.

Mounted into the existing Kyrex Cloud FastAPI app. Endpoints:

  POST   /api/chat                     stream an assistant reply (SSE)
  POST   /api/chat/cancel              cancel an in-flight generation
  GET    /api/bots                     discover Bots visible to the user
  POST   /api/bots                     create a user-owned Bot
  PATCH  /api/bots/{id}                update a Bot's lifecycle status
  GET    /api/bots/presets             named Bot configuration presets
  POST   /api/bots/{id}/configure      owner-scoped Bot configuration
  POST   /api/bots/{id}/claim          one-time claim of an OWNERLESS legacy Bot
  GET    /api/bots/{id}/browser-session            managed browser session view
  POST   /api/bots/{id}/browser-session/reconnect  reconnect/start the session
  POST   /api/bots/{id}/browser-session/end        end the session
  GET    /api/conversations            list conversations (metadata only)
  POST   /api/conversations            create a conversation (optional bot_id)
  GET    /api/conversations/{id}       fetch one conversation + messages
  DELETE /api/conversations/{id}       delete a conversation

Authentication reuses the existing Cloud session/bearer model via
``require_user``, so Chat inherits whatever auth boundary the Cloud already
enforces without inventing a new system.

SSE event protocol (explicit and stable):
  * ``conversation`` — {type, conversation_id}        (once, first frame)
  * ``delta``        — {type, content}                (0..N incremental tokens)
  * ``done``         — {type, content, conversation_id} (terminal, success)
  * ``error``        — {type, message}                (terminal, provider/engine failure)
  * ``cancelled``    — {type, content}                (terminal, user/client cancelled)

Exactly one terminal frame (``done`` | ``error`` | ``cancelled``) is emitted
per request. ``conversation`` always precedes the first ``delta``. Client
request bodies may carry a ``request_id``; if omitted one is generated so the
``/api/chat/cancel`` endpoint can target the active generation.
"""

from __future__ import annotations

import asyncio
import json
import uuid
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import chat_service
import dev_bot
import serve as kyrex_serve  # host tier table + coordinator preset/gate
import provider_profiles
# Per-Bot LLM configuration: resolves a Bot's provider profile reference to
# the exact (provider, base_url, api_key, headers, model) it must run with.
import bot_provider

router = APIRouter()

# In-flight generation registry: request_id -> {"user", "event"}. A cancel
# request sets the event, which the active /api/chat generator observes to
# stop streaming and unwind the provider worker thread. Entries are scoped to
# the authenticated user so one user cannot cancel another's generation.
_active_streams: dict[str, dict] = {}


def _sse_frame(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _require_user(request: Request) -> str:
    """Reuse the Cloud's own auth resolution. Imported lazily to avoid a
    hard import during module load in test environments."""
    # main.require_user supports session cookie OR bearer token.
    import main
    return main.require_user(request)


async def _drive_stream(gen, request_id: str, conversation_id: str):
    """Drive ``stream_chat`` to completion, emitting ordered SSE frames.

    ``stream_chat`` yields control dicts: ``{type: conversation|delta}`` for
    progressive content, and finally a ``{type: status, status: complete|error|
    cancelled, ...}`` terminal frame (an async generator cannot ``return`` a
    value). This maps them onto the stable public SSE events.
    """
    try:
        async for frame in gen:
            t = frame.get("type")
            if t == "conversation":
                yield _sse_frame({"type": "conversation",
                                  "conversation_id": frame["conversation_id"]})
            elif t == "delta":
                yield _sse_frame({"type": "delta", "content": frame["content"]})
            elif t == "task":
                yield _sse_frame({"type": "task",
                                  "task_id": frame.get("task_id"),
                                  "status": frame.get("status")})
            elif t == "progress":
                yield _sse_frame({"type": "progress",
                                  "payload": frame.get("payload") or {}})
            elif t == "approval_request":
                yield _sse_frame({
                    "type": "approval_request",
                    "task_id": frame.get("task_id"),
                    "approval_id": frame.get("approval_id"),
                    "tier": frame.get("tier"),
                    "summary": frame.get("summary", ""),
                    "detail": frame.get("detail", ""),
                    "token": frame.get("token", ""),
                })
            elif t == "approval_result":
                yield _sse_frame({"type": "approval_result",
                                  "task_id": frame.get("task_id"),
                                  "decision": frame.get("decision")})
            elif t == "delegation":
                # Safe delegation view (identities + status); never secrets.
                yield _sse_frame({"type": "delegation",
                                  "delegation": frame.get("delegation") or {}})
            elif t == "delegation_result":
                yield _sse_frame({
                    "type": "delegation_result",
                    "delegation_id": frame.get("delegation_id"),
                    "target_bot_id": frame.get("target_bot_id"),
                    "status": frame.get("status"),
                    "summary": frame.get("summary", ""),
                })
            elif t == "status":
                status = frame.get("status")
                if status == "complete":
                    yield _sse_frame({"type": "done",
                                      "content": frame.get("content", ""),
                                      "conversation_id": conversation_id})
                elif status == "cancelled":
                    yield _sse_frame({"type": "cancelled",
                                      "content": frame.get("content", "")})
                else:
                    yield _sse_frame({"type": "error",
                                      "message": frame.get("message", "provider error")})
                return
    except chat_service.ChatUnavailable as exc:
        yield _sse_frame({"type": "error", "message": str(exc)})
    except Exception as exc:
        yield _sse_frame({"type": "error", "message": f"engine failure: {exc}"})


@router.post("/api/chat")
async def chat(request: Request):
    user = _require_user(request)
    body = await request.json()
    conversation_id = (body.get("conversation_id") or "").strip()
    message = (body.get("message") or "").strip()
    request_id = (body.get("request_id") or "").strip() or uuid.uuid4().hex

    if not message:
        raise HTTPException(status_code=400, detail="message is required (and must be non-empty)")
    if len(message) > chat_service.MAX_MESSAGE_CHARS:
        raise HTTPException(status_code=400, detail="message too long")

    # Workspace binding. The body may carry "workspace_id":
    #   * absent            → sentinel (conversation keeps its stored binding)
    #   * "" / null         → explicit detach (pure conversation turn)
    #   * "<registry id>"   → attach/verify against the SERVER-SIDE registry.
    # A browser can never submit a filesystem path: only ids that exist in the
    # server-configured registry are accepted, and only by authenticated users.
    if "workspace_id" in body and body.get("workspace_id") is not None \
            and not isinstance(body.get("workspace_id"), str):
        raise HTTPException(status_code=400, detail="workspace_id must be a string")
    if body.get("workspace_id") is None and "workspace_id" in body:
        ws_value = ""  # explicit detach
    else:
        ws_value = (body.get("workspace_id") or "").strip()
    if ws_value and chat_service.resolve_workspace(ws_value) is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown or unavailable workspace '{ws_value}'")

    cancel_event = asyncio.Event()
    _active_streams[request_id] = {"user": user, "event": cancel_event}

    async def event_stream():
        try:
            gen = chat_service.stream_chat(
                user, conversation_id, message, cancel_event,
                workspace_id=(
                    chat_service._WORKSPACE_UNSET
                    if "workspace_id" not in body else ws_value))
            async for frame in _drive_stream(gen, request_id, conversation_id):
                yield frame
        except chat_service.ChatUnavailable as exc:
            yield _sse_frame({"type": "error", "message": str(exc)})
        except Exception as exc:
            yield _sse_frame({"type": "error", "message": f"engine failure: {exc}"})
        finally:
            _active_streams.pop(request_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/bots")
def list_bots(request: Request):
    """Discover the Bots visible to the authenticated user.

    Reads the EXISTING Bot registry (bots.py) — no second registry, no
    invented auth. Ownership mirrors the registry's own owner field: a Bot
    owned by the user, or operator-created (no owner), is visible; any other
    owner's Bot is not. Only UI metadata is exposed (id/name/status/model/
    availability) — never rift paths, policy, system prompts, or
    credentials. Registry errors are surfaced as 500, never silently
    swallowed into an empty list.
    """
    user = _require_user(request)
    try:
        return {"bots": chat_service.list_bots_for_user(user)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"bot registry unavailable: {exc}")

_BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _bot_public(bot: dict, user: str) -> dict:
    return {
        "id": bot.get("id"),
        "name": bot.get("name"),
        "status": bot.get("status"),
        "model": bot.get("model"),
        "available": chat_service._bot_rift_resolves(bot),
        # Coordinator capability (owner-scoped, explicitly granted). Read-only
        # flag for the UI; it never reveals the underlying policy rules.
        "coordinator": kyrex_serve.coordinator_granted(bot),
        "manageable": str(bot.get("owner") or "") == user,
        # Visible-but-ownerless (legacy) Bot: the UI offers a one-time claim,
        # nothing else. An ownerless Bot is never "manageable" until claimed.
        "claimable": str(bot.get("owner") or "").strip() == "",
        # Per-Bot LLM configuration, read-only and NON-SECRET: the referenced
        # profile id (a reference, never a secret) plus a summary the UI can
        # render — profile name/provider/base URL/model list/last-four. The
        # API key and any header VALUE are never included.
        "provider_profile_id": bot.get("provider_profile_id") or "",
        "provider": bot_provider.bot_provider_view(user, bot),
    }


def _validate_provider_selection(user: str, provider_profile_id, model):
    """Validate an owner's provider-profile + model selection, fail closed.

    Both values are optional individually, but:
      * A non-empty ``provider_profile_id`` MUST reference a profile owned by
        *user* (an unknown or another user's profile is a 400 — never a silent
        fallback to globals).
      * When a profile is selected AND a ``model`` is given, the model must
        belong to that profile's model list.

    Returns ``(provider_profile_id_or_"", model_or_None)``. Raises
    ``HTTPException(400)`` on any violation.
    """
    from bots import validate_provider_profile_id
    try:
        pid = validate_provider_profile_id(provider_profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    model_val = None
    if model is not None:
        model_val = str(model).strip()
        if not model_val:
            raise HTTPException(status_code=400, detail="model must be non-empty")

    if pid:
        profile = provider_profiles.get_profile(user, pid)
        if profile is None:
            raise HTTPException(
                status_code=400,
                detail=f"provider profile {pid!r} is not configured for this user",
            )
        if model_val is not None:
            bare = model_val.partition(":")[2].strip() if ":" in model_val else model_val
            models = [str(m).strip() for m in (profile.get("models") or [])]
            if bare not in models:
                raise HTTPException(
                    status_code=400,
                    detail=f"model {bare!r} is not available on provider "
                           f"profile {pid!r}",
                )
    return pid, model_val


def _web_operator() -> str:
    """The single configured Kyrex web operator (allowed GitHub username).

    Read live from the Cloud app so tests may reseed it; an empty value fails
    closed (nobody is an operator).
    """
    import main
    return str(getattr(main, "ALLOWED_USERNAME", "") or "").strip()


def _require_operator(user: str) -> None:
    """Fail closed unless *user* is the configured Kyrex web operator."""
    operator = _web_operator()
    if not operator or user != operator:
        raise HTTPException(
            status_code=403,
            detail="Only the configured Kyrex web operator can claim a legacy Bot",
        )


def _owned_bot(user: str, bot_id: str) -> dict:
    try:
        bot = chat_service.bots.get_bot(bot_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Bot not found")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"bot registry unavailable: {exc}")
    if str(bot.get("owner") or "") != user:
        raise HTTPException(status_code=403, detail="Bot is not managed by this user")
    return bot


# ── Managed browser session (persistent Bot "computer") ────────────────
#
# Lifecycle only — these endpoints NEVER run a browser action and NEVER
# accept a client-supplied command. Each is owner-scoped through _owned_bot,
# so only the Bot's owner may inspect, reconnect, or end its session, and the
# session key is derived server-side from (owner, bot_id). Every response is a
# browser_sessions.public_view: the sealed credential is never serialized.

def _browser_sessions():
    """Lazy import so chat_api stays importable without the Cloud path set."""
    import browser_sessions  # noqa: E402 — resolved via the Cloud path
    return browser_sessions


@router.get("/api/bots/{bot_id}/browser-session")
def get_browser_session(bot_id: str, request: Request):
    """The NON-SECRET managed-session view for an owned Bot (or ``null``)."""
    user = _require_user(request)
    bot = _owned_bot(user, bot_id)
    sessions = _browser_sessions()
    session = sessions.get_session(bot.get("owner"), bot_id)
    return {"session": sessions.public_view(session) if session else None}


@router.post("/api/bots/{bot_id}/browser-session/reconnect")
async def reconnect_browser_session(bot_id: str, request: Request):
    """Reconnect to the live session, or start a fresh one (owner-scoped)."""
    user = _require_user(request)
    bot = _owned_bot(user, bot_id)
    sessions = _browser_sessions()
    session = sessions.reconnect(bot.get("owner"), bot_id)
    return {"session": sessions.public_view(session)}


@router.post("/api/bots/{bot_id}/browser-session/end")
async def end_browser_session(bot_id: str, request: Request):
    """Explicitly end the Bot's managed session (owner-scoped)."""
    user = _require_user(request)
    bot = _owned_bot(user, bot_id)
    sessions = _browser_sessions()
    return {"ended": sessions.end_session(bot.get("owner"), bot_id)}


# ── Delegated work (read-only, owner-scoped) ───────────────────────────
# Status-only for this phase. These endpoints NEVER approve, cancel, or
# otherwise mutate delegated work: an approval belongs to the TARGET task and
# is resolved only by the owner through the existing task-respond flow. The
# coordinator cannot approve its own or another Bot's restricted action.

def _delegation():
    """Lazy import so chat_api stays importable without the Cloud path set."""
    import delegation  # noqa: E402 — resolved via the Cloud path
    return delegation


@router.get("/api/delegations")
def list_delegations(request: Request, conversation_id: Optional[str] = None):
    """Owner-scoped, read-only list of delegated work (public views only)."""
    user = _require_user(request)
    delegation = _delegation()
    conv = (conversation_id or "").strip() or None
    return {"delegations": delegation.list_delegations(
        user, parent_conversation_id=conv)}


@router.post("/api/bots/{bot_id}/claim")
async def claim_bot(bot_id: str, request: Request):
    """One-time claim of an OWNERLESS legacy Bot by the Kyrex web operator.

    A legacy Bot (owner empty/missing) is visible to the operator but is not
    manageable, because lifecycle/configuration require ``bot.owner == user``.
    A successful claim records the operator as ``owner`` — which is the ONLY
    change — so the EXISTING owner-scoped Start/Pause/Stop and
    Configure-as-Developer-Bot controls then apply unchanged.

    Fail closed:
      * Only the configured Kyrex web operator may claim (anyone else gets
        403, including anonymous 401).
      * Only an ownerless Bot may be claimed. A Bot owned by anyone else — or
        already owned by the operator — gets 409 and is never overwritten.
      * Claiming does not start the Bot, change its policy/status, or touch
        its Rift. It grants control only.
    """
    user = _require_user(request)
    _require_operator(user)
    try:
        updated = chat_service.bots.claim_bot(bot_id, user)
    except KeyError:
        raise HTTPException(status_code=404, detail="Bot not found")
    except chat_service.bots.BotAlreadyOwned as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not claim bot: {exc}")
    return _bot_public(updated, user)


# Upper bound on a create-time system prompt. The registry stores free text;
# this keeps a create from writing an unbounded blob, and matches the
# configure endpoint's system_prompt limit.
_MAX_SYSTEM_PROMPT_CHARS = 8000

# Create-time lifecycle restriction. A NEW Bot may be created stopped (the
# default) or paused — both mean "not eligible for new work". "running" is
# deliberately NOT an accepted initial status: a new Bot starts stopped and
# must be explicitly started through the lifecycle endpoint
# (PATCH /api/bots/{id}), so eligibility for new work is always an explicit,
# auditable owner action rather than a side effect of creation.
_CREATE_INITIAL_STATUSES = frozenset({
    chat_service.bots.STATUS_STOPPED,
    chat_service.bots.STATUS_PAUSED,
})


def _slugify_bot_id(name: str) -> str:
    """Derive a registry-safe Bot slug from a human name, server-side.

    Lowercases, collapses every run of non-[a-z0-9] characters into a single
    hyphen, trims leading/trailing hyphens, and caps the length at the
    registry's slug limit. Returns "" when the name has no usable characters
    (the caller then rejects the create rather than inventing an id).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:64].strip("-")


def _unique_bot_id(base: str) -> str:
    """Return a registry-free id derived from *base* (collision-safe).

    The base itself when free; otherwise ``<base>-2``, ``<base>-3`` … The
    registry lock inside add_bot is the final authority (a racing create still
    raises), so this only picks the preferred candidate.
    """
    existing = chat_service.bots.load_bots()
    if base not in existing:
        return base
    stem = base[:60].rstrip("-") or "bot"
    n = 2
    while f"{stem}-{n}" in existing:
        n += 1
    return f"{stem}-{n}"


def _default_provider_selection(user: str):
    """The caller's default (provider_profile_id, model), or ``None``.

    Derived ONLY from the caller's own encrypted provider profiles (the store
    behind Provider settings) — never from a global/env provider. Picks the
    lowest-id profile and that profile's first model, deterministically.
    Returns ``None`` when the user has no configured profile, so the caller
    fails the create closed instead of borrowing a global default.
    """
    profiles = provider_profiles.list_profiles(user)
    if not profiles:
        return None
    first = sorted(profiles, key=lambda p: str(p.get("id") or ""))[0]
    models = [str(m).strip() for m in (first.get("models") or []) if str(m).strip()]
    if not models:
        return None
    return str(first.get("id") or ""), models[0]


@router.post("/api/bots")
async def create_bot(request: Request):
    """Create a user-owned Bot in the existing Cloud registry.

    Basic (Grok-style) creation: the caller supplies a NAME and, optionally, a
    role/prompt ("what should this Bot do?"). The stable id is DERIVED
    server-side from the name (collision-safe), the provider profile/model are
    defaulted from the caller's OWN configured profile when omitted, and the
    Rift is a safe server-generated directory — the user is never asked for a
    raw id, provider secret, or path. Advanced callers may still supply an
    explicit id, provider-profile/model, capability policy (named preset or
    explicit policy), browser domain allowlist, initial lifecycle status, and a
    SERVER-REGISTERED workspace id.

    Fail closed, at the API boundary:
      * The caller is authenticated (401 otherwise) and becomes the OWNER;
        ownership is never taken from the request body.
      * The id is a clean slug and must be UNIQUE — a duplicate is 409.
      * ``provider_profile_id`` is owner-scoped: an unknown or another user's
        profile is 400 (never a silent fallback to a global provider), and the
        exact ``model`` must belong to that profile. A Bot created with no
        profile stores NO reference and NO global default — it is unconfigured
        and fails closed at turn time.
      * ``policy``/``preset`` are shape-validated with the SAME engine the
        executor enforces with. A configuration that makes the Bot writable
        (grants ``fs:write``) requires a Rift that is a real git repository —
        the configure endpoint's rule, applied here too.
      * ``browser_allowlist`` is bare hostnames only (no scheme/path/whitespace).
      * A Rift is resolved ONLY from the server-side workspace registry; a raw
        filesystem path is never accepted (any client ``rift``/``path`` key is
        ignored) and the default Rift is a server-generated directory.

    No API key or decrypted secret is ever stored: the registry holds only the
    profile REFERENCE and the exact model. The response is the standard
    non-secret ``_bot_public`` view.
    """
    user = _require_user(request)
    body = await request.json()

    # Identity — a bounded name, plus an OPTIONAL explicit slug id. This is the
    # Grok-style basic flow: the caller names the Bot and (at most) describes
    # it; the id is DERIVED server-side from the name, so a user is never asked
    # for a raw identifier. An explicit id is still supported (advanced /
    # programmatic callers) and validated exactly as before.
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Bot name is required")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="Bot name is too long")

    provided_id = str(body.get("id") or "").strip().lower()
    if provided_id and not _BOT_ID_RE.fullmatch(provided_id):
        raise HTTPException(
            status_code=400,
            detail="Bot id must use lowercase letters, numbers, hyphens, or underscores",
        )
    derived_id = "" if provided_id else _slugify_bot_id(name)
    if not provided_id and not derived_id:
        raise HTTPException(
            status_code=400,
            detail="Could not derive a Bot id from the name — provide an id",
        )

    # Role / system prompt: the basic form's "What should this Bot do?" is the
    # Bot's role, stored as its system prompt. ``role`` and ``system_prompt``
    # are accepted as aliases so existing clients keep working unchanged.
    system_prompt = ""
    raw_prompt = body.get("system_prompt")
    if raw_prompt is None:
        raw_prompt = body.get("role")
    if raw_prompt is not None:
        system_prompt = str(raw_prompt)
        if len(system_prompt) > _MAX_SYSTEM_PROMPT_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"system_prompt is too long (max {_MAX_SYSTEM_PROMPT_CHARS} characters)",
            )

    # Provider/model selection. An exact ``model`` is optional: when omitted
    # (the basic flow) it is DEFAULTED from the caller's own configured provider
    # profile — never from a global KYREX_* default. A caller with no configured
    # profile and no explicit model fails closed with a clear message rather
    # than storing a Bot that can never serve a turn.
    model = str(body.get("model") or "").strip()
    if len(model) > 200:
        raise HTTPException(status_code=400, detail="Bot model is too long")

    provider_profile_id = ""
    if "provider_profile_id" in body and body.get("provider_profile_id") is not None:
        from bots import validate_provider_profile_id
        try:
            provider_profile_id = validate_provider_profile_id(
                body.get("provider_profile_id"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if not model:
        if provider_profile_id:
            profile = provider_profiles.get_profile(user, provider_profile_id)
            if profile is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"provider profile {provider_profile_id!r} is not "
                           "configured for this user",
                )
            models = [str(m).strip() for m in (profile.get("models") or [])
                      if str(m).strip()]
            if not models:
                raise HTTPException(
                    status_code=400,
                    detail=f"provider profile {provider_profile_id!r} has no models",
                )
            model = models[0]
        else:
            default_selection = _default_provider_selection(user)
            if default_selection is None:
                raise HTTPException(
                    status_code=400,
                    detail="A Bot needs a model — configure a provider profile "
                           "(Provider settings) or pass an explicit model",
                )
            provider_profile_id, model = default_selection

    # Owner-scoped validation of the final (profile, model) pair. When a profile
    # is referenced the model MUST belong to it; when no profile is referenced
    # an explicit model is stored as an unconfigured Bot (fails closed at turn
    # time) — never a silent fallback to globals.
    if provider_profile_id:
        provider_profile_id, model = _validate_provider_selection(
            user, provider_profile_id, model)

    # Capability/policy selection: a named preset XOR an explicit policy, both
    # validated with the existing policy engine's exact value space.
    preset = str(body.get("preset") or "").strip().lower()
    has_policy = "policy" in body and body.get("policy") is not None
    if preset and has_policy:
        raise HTTPException(
            status_code=400,
            detail="provide either a preset or an explicit policy, not both")
    policy: dict = {}
    if preset:
        if preset != dev_bot.DEVELOPER_PRESET_ID:
            raise HTTPException(status_code=400, detail=f"unknown preset '{preset}'")
        policy = dev_bot.developer_preset_policy()
    elif has_policy:
        try:
            dev_bot.validate_bot_policy(body.get("policy"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        policy = dict(body.get("policy"))

    # Optional browser domain allowlist (bare hostnames; the Browser Operator
    # enforces it server-side on every navigation).
    try:
        browser_allowlist = chat_service.bots.validate_browser_allowlist(
            body.get("browser_allowlist"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Initial lifecycle status. Defaults to stopped; only a non-running status
    # may be chosen at create (see _CREATE_INITIAL_STATUSES).
    status = chat_service.bots.STATUS_STOPPED
    if body.get("status") is not None:
        status = str(body.get("status")).strip().lower()
        if status not in _CREATE_INITIAL_STATUSES:
            raise HTTPException(
                status_code=400,
                detail="initial status must be 'stopped' or 'paused' — a new "
                       "Bot starts stopped and must be started explicitly",
            )

    # Workspace selection (advanced). The client may name a SERVER-REGISTERED
    # workspace id (the same server-controlled registry the workspace surface
    # uses); the real path is resolved server-side and never trusted from the
    # body. Any raw "rift"/"path" a client sends is ignored. With no workspace
    # id — the default — the Rift is a server-generated directory under the
    # Cloud data root; the user is never asked for a path.
    workspace_id = body.get("workspace_id")
    if workspace_id is not None and not isinstance(workspace_id, str):
        raise HTTPException(status_code=400, detail="workspace_id must be a string")
    workspace_id = (workspace_id or "").strip()
    resolved_ws = None
    if workspace_id:
        resolved_ws = chat_service.resolve_workspace(workspace_id)
        if resolved_ws is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown or unavailable workspace '{workspace_id}'")

    # Registration. A caller-provided id is used verbatim (a duplicate is 409 —
    # add_bot never overwrites). A DERIVED id picks a collision-free variant;
    # the loop re-picks if a racing create won the slug first, so a
    # server-generated id never fails on a collision.
    attempts = 8 if not provided_id else 1
    last_dup = None
    for _attempt in range(attempts):
        bot_id = _unique_bot_id(derived_id) if not provided_id else provided_id

        manage_rift = False
        if workspace_id:
            rift = resolved_ws
        else:
            rift = chat_service.bots.DATA_DIR / "rifts" / bot_id
            manage_rift = True

        # A configuration that makes the Bot writable requires a Rift that is a
        # real git repository — exactly the configure endpoint's fail-closed
        # rule. A fresh server-generated Rift is an empty directory and is
        # therefore rejected, so a writable Bot must be created against a repo
        # workspace.
        if dev_bot.is_writable_bot_policy(policy):
            try:
                dev_bot.validate_developer_rift({"id": bot_id, "rift": str(rift)})
            except dev_bot.DevBotError as exc:
                raise HTTPException(status_code=409, detail=str(exc))

        # Create the safe Rift directory (server-managed case only) before
        # registration so the stored Rift always resolves.
        if manage_rift:
            try:
                rift.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(
                    status_code=500, detail=f"could not create bot rift: {exc}")

        try:
            bot = chat_service.bots.add_bot(
                bot_id, name, model, str(rift),
                policy=policy, status=status, owner=user,
                system_prompt=system_prompt,
                browser_allowlist=browser_allowlist,
                provider_profile_id=provider_profile_id,
            )
        except ValueError as exc:
            # add_bot refuses to overwrite. A derived id may retry a fresh
            # candidate; an explicit id is a hard 409.
            if not provided_id and "already exists" in str(exc):
                last_dup = exc
                continue
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"could not create bot: {exc}")
        return _bot_public(bot, user)
    raise HTTPException(
        status_code=409,
        detail=str(last_dup) if last_dup else "could not allocate a unique Bot id",
    )


@router.patch("/api/bots/{bot_id}")
async def update_bot(bot_id: str, request: Request):
    """Update a user-owned Bot's lifecycle status and/or LLM configuration.

    Accepts ``status`` (running/paused/stopped), and optionally the per-Bot
    LLM configuration: ``provider_profile_id`` (owner-scoped reference) and
    ``model``. A provided profile/model pair is validated exactly as on
    create — the model must belong to the referenced profile — so an invalid
    selection never lands on the record.
    """
    user = _require_user(request)
    bot = _owned_bot(user, bot_id)
    body = await request.json()

    fields: dict = {}
    if "model" in body and body.get("model") is not None:
        model = str(body.get("model")).strip()
        if not model:
            raise HTTPException(status_code=400, detail="model must be non-empty")
        if len(model) > 200:
            raise HTTPException(status_code=400, detail="model is too long")
        fields["model"] = model
    if "provider_profile_id" in body:
        eff_model = fields.get("model", bot.get("model"))
        pid, _ = _validate_provider_selection(
            user, body.get("provider_profile_id"), eff_model)
        fields["provider_profile_id"] = pid

    status = str(body.get("status") or "").strip().lower()
    if status and status not in {"running", "stopped", "paused"}:
        raise HTTPException(
            status_code=400, detail="status must be running, stopped, or paused")
    if not status and not fields:
        raise HTTPException(
            status_code=400,
            detail="status, model, or provider_profile_id is required")

    try:
        if fields:
            bot = chat_service.bots.update_bot(bot_id, **fields)
        if status:
            bot = chat_service.bots.set_status(bot_id, status)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not update bot: {exc}")
    return _bot_public(bot, user)


# ── Bot configuration (explicit, owner-scoped) ─────────────────────
# The ONLY convenience grant is the named Developer preset. Everything else
# is explicit owner input, validated against the existing registry/policy
# model. No unsafe defaults: a Bot is writable only when its policy
# explicitly grants fs:write, and only when its Rift is a real repo.

def _preset_view() -> list[dict]:
    """Named configuration presets with their effective, host-derived
    permissions. Permission values come from the SAME policy engine the
    executor enforces with — the UI shows exactly what the host will act on.
    """
    return [{
        "id": dev_bot.DEVELOPER_PRESET_ID,
        "label": dev_bot.DEVELOPER_PRESET_LABEL,
        "policy": dev_bot.developer_preset_policy(),
        "permissions": dev_bot.effective_permissions(dev_bot.DEVELOPER_PRESET),
    }, {
        # Coordinator ("Chief of Staff"): may delegate work to the owner's
        # other Bots. Grants NO write/delete/push/shell — a coordinator only
        # observes and delegates; the delegated target stays authoritative.
        "id": kyrex_serve.COORDINATOR_PRESET_ID,
        "label": kyrex_serve.COORDINATOR_PRESET_LABEL,
        "policy": kyrex_serve.coordinator_preset_policy(),
        "permissions": dev_bot.effective_permissions(
            kyrex_serve.COORDINATOR_PRESET),
    }]


@router.get("/api/bots/presets")
def list_bot_presets(request: Request):
    """Named Bot configuration presets (currently just "developer")."""
    _require_user(request)
    return {"presets": _preset_view()}


@router.post("/api/bots/{bot_id}/configure")
async def configure_bot(bot_id: str, request: Request):
    """Explicitly configure a user-owned Bot (owner-scoped).

    Body (all optional, at least one required):

      * ``preset``        — a named preset id (``"developer"``).
      * ``policy``        — an explicit policy dict (validated shape).
      * ``system_prompt`` — the Bot's system prompt (registry-supported).
      * ``model``         — the Bot's ``provider:model`` string.

    Fail closed:
      * ``preset`` and ``policy`` are mutually exclusive — an explicit policy
        is never silently overwritten by (or merged with) a preset.
      * A configuration that makes the Bot writable (fs:write granted) is
        only accepted when the Bot's Rift is a real git repository — an
        empty or arbitrary directory is rejected with a clear error.
      * Non-owners (including operator-created Bots) get 403.

    Write-class operations still require the EXISTING approval flow at
    execution time; this endpoint grants capability, never approval.
    """
    user = _require_user(request)
    bot = _owned_bot(user, bot_id)
    body = await request.json()

    preset = str(body.get("preset") or "").strip().lower()
    has_policy = "policy" in body and body.get("policy") is not None
    if preset and has_policy:
        raise HTTPException(
            status_code=400,
            detail="provide either a preset or an explicit policy, not both")

    fields: dict = {}
    if preset:
        if preset == dev_bot.DEVELOPER_PRESET_ID:
            fields["policy"] = dev_bot.developer_preset_policy()
        elif preset == kyrex_serve.COORDINATOR_PRESET_ID:
            fields["policy"] = kyrex_serve.coordinator_preset_policy()
        else:
            raise HTTPException(
                status_code=400, detail=f"unknown preset '{preset}'")
    elif has_policy:
        policy = body.get("policy")
        try:
            dev_bot.validate_bot_policy(policy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        fields["policy"] = dict(policy)

    if "system_prompt" in body and body.get("system_prompt") is not None:
        prompt = str(body.get("system_prompt"))
        if len(prompt) > 8000:
            raise HTTPException(status_code=400, detail="system_prompt is too long")
        fields["system_prompt"] = prompt

    if "model" in body and body.get("model") is not None:
        model = str(body.get("model")).strip()
        if not model:
            raise HTTPException(status_code=400, detail="model must be non-empty")
        if len(model) > 200:
            raise HTTPException(status_code=400, detail="model is too long")
        fields["model"] = model

    # Per-Bot LLM configuration: an owner-scoped provider-profile reference.
    # Validated together with the effective model (the one just supplied, or
    # the Bot's current model) so a profile whose model list does not contain
    # it is rejected — never silently accepted.
    if "provider_profile_id" in body:
        eff_model = fields.get("model", bot.get("model"))
        pid, _ = _validate_provider_selection(
            user, body.get("provider_profile_id"), eff_model)
        fields["provider_profile_id"] = pid

    if not fields:
        raise HTTPException(status_code=400, detail="no configuration fields supplied")

    # A configuration that makes the Bot writable requires a real repo Rift.
    target_policy = fields.get("policy", bot.get("policy"))
    if dev_bot.is_writable_bot_policy(target_policy):
        try:
            dev_bot.validate_developer_rift(bot)
        except dev_bot.DevBotError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    try:
        updated = chat_service.bots.update_bot(bot_id, **fields)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not configure bot: {exc}")

    out = _bot_public(updated, user)
    out["policy"] = updated.get("policy") or {}
    out["writable"] = dev_bot.is_writable_bot_policy(updated.get("policy"))
    out["permissions"] = dev_bot.effective_permissions(updated.get("policy"))
    return out


@router.post("/api/chat/cancel")
async def cancel_chat(request: Request):
    """Cancel an in-flight generation by request_id.

    The active /api/chat generator observes the set event and stops streaming,
    emits a ``cancelled`` frame, and unwinds the provider worker thread.
    Cancellation is cooperative: it stops token delivery immediately but cannot
    abort an in-flight provider HTTP request mid-flight. Not-found is
    idempotent (200) so a client that already received the terminal frame is
    not errored.
    """
    user = _require_user(request)
    body = await request.json()
    request_id = (body.get("request_id") or "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id is required")
    entry = _active_streams.get(request_id)
    if entry is None:
        return {"cancelled": False, "message": "no active stream for request_id"}
    if entry["user"] != user:
        raise HTTPException(status_code=403, detail="request belongs to another user")
    entry["event"].set()
    return {"cancelled": True}


@router.get("/api/conversations")
def list_conversations(request: Request):
    user = _require_user(request)
    return {"conversations": chat_service.list_conversations(user)}


@router.post("/api/conversations")
async def create_conversation(request: Request):
    """Create a conversation, optionally bound to a Bot.

    Body may carry ``bot_id``. The binding is validated here, fail-closed:
    the Bot must exist, be visible to the requesting user, and have a
    resolvable Rift. Unknown/unauthorized/unresolvable Bots are rejected
    with a 400; a corrupted registry is a 500. ``bot_id`` absent/empty
    creates ordinary Kyrex Chat exactly as before.
    """
    user = _require_user(request)
    body = await request.json() if await request.body() else {}
    title = (body.get("title") or "").strip()
    bot_id = str(body.get("bot_id") or "").strip() \
        if body.get("bot_id") is not None else ""
    try:
        conv = chat_service.create_conversation(user, title=title, bot_id=bot_id)
    except chat_service.BotUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except chat_service.BotRegistryError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return conv


@router.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, request: Request):
    user = _require_user(request)
    conv = chat_service.get_conversation(user, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Presentation boundary: a conversation stored before the sanitizer
    # existed (or written by any other path) must still render without
    # internal control markers. The stored record is not mutated — only the
    # response copy is cleaned.
    return chat_service.sanitize_conversation(conv)


@router.patch("/api/conversations/{conversation_id}/settings")
async def update_conversation_settings(conversation_id: str, request: Request):
    user = _require_user(request)
    body = await request.json()
    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    if not provider or not model:
        raise HTTPException(status_code=400, detail="provider and model are required")
    try:
        return chat_service.set_conversation_provider(user, conversation_id, provider, model)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except chat_service.ChatUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request):
    user = _require_user(request)
    deleted = chat_service.delete_conversation(user, conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True}


# ── workspace registry surface (server-controlled ids only) ──────
@router.get("/api/chat/workspaces")
def workspaces(request: Request):
    """List the server-registered workspaces the user may attach.

    Only ids/names/availability are exposed — never filesystem paths. The
    registry itself comes exclusively from server environment configuration,
    so a browser request cannot add or select an arbitrary server path.
    """
    _require_user(request)
    return {"workspaces": chat_service.list_workspaces()}


@router.post("/api/chat/workspace")
async def attach_workspace(request: Request):
    """Attach (or detach) a registered workspace on a conversation.

    Body: {"conversation_id": "...", "workspace_id": "<registry id>" | null}
    A null/empty workspace_id detaches (pure-conversation mode). The id is
    validated against the server-side registry — unknown ids are rejected
    and raw paths are never accepted from the client.
    """
    user = _require_user(request)
    body = await request.json()
    conversation_id = (body.get("conversation_id") or "").strip()
    if not conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id is required")
    raw = body.get("workspace_id")
    if raw is not None and not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="workspace_id must be a string or null")
    ws_value = (raw or "").strip()

    conv = chat_service.get_conversation(user, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # A Bot-bound conversation's binding is authoritative — a workspace must
    # never be layered onto it silently.
    if conv.get("bot_id"):
        raise HTTPException(
            status_code=400,
            detail="a bot-bound conversation cannot attach a workspace")

    if ws_value:
        resolved = chat_service.resolve_workspace(ws_value)
        if resolved is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown or unavailable workspace '{ws_value}'")
        conv["workspace_id"] = ws_value
        name = next((w["name"] for w in chat_service.list_workspaces()
                     if w["id"] == ws_value), ws_value)
    else:
        conv.pop("workspace_id", None)
        name = None

    chat_service._write(user, conv)
    return {"conversation_id": conversation_id,
            "workspace_id": ws_value or None, "workspace_name": name}


@router.post("/api/chat/workspace/provision")
async def provision_workspace(request: Request):
    """Clone a repo into a server-owned Chat workspace, then auto-register it.

    Body: {"id": "<slug>", "repo_url": "https://github.com/owner/repo.git"}
    The repo_url is authorized against the same own-repo / allowlist gate the
    executor uses; the clone target path is server-generated (never supplied by
    the client). Once cloned it is discovered by list_workspaces automatically.
    """
    user = _require_user(request)  # any authenticated user (single-operator for now)
    body = await request.json()
    workspace_id = (body.get("id") or "").strip()
    repo_url = (body.get("repo_url") or "").strip()
    if not workspace_id or not repo_url:
        raise HTTPException(status_code=400, detail="id and repo_url are required")

    # Authorize the repo: only own or allowlisted repos may be cloned.
    import git_workflow
    if not (git_workflow.is_own_repo(repo_url)
            or git_workflow.is_allowlisted_external_repo(repo_url)):
        raise HTTPException(
            status_code=403,
            detail="repo_url is not an owned or allowlisted repository")

    try:
        result = chat_service.provision_workspace(workspace_id, repo_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"provisioned": True, "id": result["id"]}


# ── availability probe (used by the UI to surface config state) ──
# Semantics (do not regress): "available" means the LLM PROVIDER is
# configured — it is NOT an engine/workspace indicator. The UI renders it
# as "Provider ready"; workspace attachment is reported per conversation.
@router.get("/api/chat/providers")
def chat_providers(request: Request):
    user = _require_user(request)
    return {"providers": chat_service.list_provider_profiles(user)}

@router.get("/api/chat/provider-profiles")
def provider_profiles_list(request: Request):
    user = _require_user(request)
    try:
        return {"profiles": provider_profiles.list_profiles(user)}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

@router.post("/api/chat/provider-profiles")
async def provider_profiles_save(request: Request):
    user = _require_user(request)
    body = await request.json()
    try:
        return provider_profiles.save_profile(user, body)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.delete("/api/chat/provider-profiles/{profile_id}")
def provider_profiles_delete(profile_id: str, request: Request):
    user = _require_user(request)
    try:
        deleted = provider_profiles.delete_profile(user, profile_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider profile not found")
    return {"deleted": True}


@router.get("/api/chat/status")
def chat_status(request: Request):
    _require_user(request)
    ok, detail = chat_service.engine_available()
    return {
        "available": ok,
        "detail": detail,
        "provider": detail,
        "workspaces": len(chat_service.list_workspaces()),
    }
