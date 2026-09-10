"""chat_api.py — HTTP/SSE surface for Kyrex Chat.

Mounted into the existing Kyrex Cloud FastAPI app. Endpoints:

  POST   /api/chat                     stream an assistant reply (SSE)
  POST   /api/chat/cancel              cancel an in-flight generation
  GET    /api/bots                     discover Bots visible to the user
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
import provider_profiles

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
        "manageable": str(bot.get("owner") or "") == user,
    }


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


@router.post("/api/bots")
async def create_bot(request: Request):
    """Create a user-owned Bot in the existing Cloud registry."""
    user = _require_user(request)
    body = await request.json()
    bot_id = str(body.get("id") or "").strip().lower()
    name = str(body.get("name") or "").strip()
    model = str(body.get("model") or "").strip()
    if not _BOT_ID_RE.fullmatch(bot_id):
        raise HTTPException(
            status_code=400,
            detail="Bot id must use lowercase letters, numbers, hyphens, or underscores",
        )
    if not name or not model:
        raise HTTPException(status_code=400, detail="Bot name and model are required")
    if len(name) > 100 or len(model) > 200:
        raise HTTPException(status_code=400, detail="Bot name or model is too long")

    rift = chat_service.bots.DATA_DIR / "rifts" / bot_id
    try:
        bot = chat_service.bots.add_bot(
            bot_id, name, model, str(rift), owner=user, status="stopped")
        rift.mkdir(parents=True, exist_ok=True)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not create bot: {exc}")
    return _bot_public(bot, user)


@router.patch("/api/bots/{bot_id}")
async def update_bot(bot_id: str, request: Request):
    """Update the lifecycle state of a user-owned Bot."""
    user = _require_user(request)
    _owned_bot(user, bot_id)
    body = await request.json()
    status = str(body.get("status") or "").strip().lower()
    if status not in {"running", "stopped", "paused"}:
        raise HTTPException(
            status_code=400, detail="status must be running, stopped, or paused")
    try:
        bot = chat_service.bots.set_status(bot_id, status)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not update bot: {exc}")
    return _bot_public(bot, user)


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
    return conv


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
