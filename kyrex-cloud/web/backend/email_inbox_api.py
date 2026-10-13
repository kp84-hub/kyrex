"""Authenticated settings for the read-only, dedicated forwarding inbox."""

import json
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import agent_inbox
import connectors

router = APIRouter(prefix="/api/connections/email-inbox")


def owner(request, mutation=False):
    from connections_api import _require_user

    user = _require_user(request)
    if mutation:
        origin = request.headers.get("origin")
        if origin:
            source = urlsplit(origin)
            if (
                source.scheme not in ("http", "https")
                or source.netloc.lower() != request.headers.get("host", "").lower()
            ):
                raise HTTPException(
                    403, "Open Email inbox settings from your Kyrex Chat page."
                )
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(
                403, "Open Email inbox settings from your Kyrex Chat page."
            )
    return user


async def payload(request, fields):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096:
            raise HTTPException(413, "Email inbox settings are too large.")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) - fields:
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid email inbox settings.") from None


def perform(fn, *args):
    try:
        return fn(*args)
    except connectors.ConnectorError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(
            503, "Email inbox settings are unavailable. No other inbox was used."
        ) from None


@router.get("")
def status(request: Request):
    user = owner(request)
    return perform(lambda: agent_inbox.default_store().view(user))


@router.post("/connect")
async def connect(request: Request):
    user = owner(request, True)
    body = await payload(request, {"inbox_id", "api_key"})
    return await run_in_threadpool(
        perform,
        lambda: agent_inbox.default_store().connect(
            user, body.get("inbox_id"), body.get("api_key", "")
        ),
    )


@router.post("/source")
async def select(request: Request):
    user = owner(request, True)
    body = await payload(request, {"source"})
    return await run_in_threadpool(
        perform, lambda: agent_inbox.default_store().select(user, body.get("source"))
    )


@router.post("/disconnect")
async def disconnect(request: Request):
    user = owner(request, True)
    await payload(request, set())
    return await run_in_threadpool(
        perform, lambda: agent_inbox.default_store().disconnect(user)
    )
