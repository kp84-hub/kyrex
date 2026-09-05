"""chat_api.py — HTTP/SSE surface for Kyrex Chat.

Mounted into the existing Kyrex Cloud FastAPI app. Endpoints:

  POST   /api/chat                     stream an assistant reply (SSE)
  POST   /api/chat/cancel              cancel an in-flight generation
  GET    /api/conversations            list conversations (metadata only)
  POST   /api/conversations            create a conversation
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
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

import chat_service

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

    cancel_event = asyncio.Event()
    _active_streams[request_id] = {"user": user, "event": cancel_event}

    async def event_stream():
        try:
            gen = chat_service.stream_chat(
                user, conversation_id, message, cancel_event)
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
    user = _require_user(request)
    body = await request.json() if await request.body() else {}
    title = (body.get("title") or "").strip()
    conv = chat_service.create_conversation(user, title=title)
    return conv


@router.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, request: Request):
    user = _require_user(request)
    conv = chat_service.get_conversation(user, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@router.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request):
    user = _require_user(request)
    deleted = chat_service.delete_conversation(user, conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"deleted": True}


# ── availability probe (used by the UI to surface config state) ──
@router.get("/api/chat/status")
def chat_status(request: Request):
    _require_user(request)
    ok, detail = chat_service.engine_available()
    return {"available": ok, "detail": detail}
