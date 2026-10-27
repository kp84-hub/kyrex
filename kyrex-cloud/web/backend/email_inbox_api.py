"""Authenticated settings for the read-only, dedicated forwarding inbox."""

import json
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import agent_inbox
import connectors
import email_accounts

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


def connection_view(user):
    import zoho_send
    inbox = agent_inbox.default_store()
    view = inbox.view(user)
    view['sending'] = zoho_send.SendStore(inbox).view(user)
    return view


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
    return JSONResponse(perform(lambda: connection_view(user)), headers={'Cache-Control': 'no-store'})


@router.post("/connect")
async def connect(request: Request):
    user = owner(request, True)
    body = await payload(
        request, {"provider", "inbox_id", "api_key", "email", "password", "host"}
    )
    provider = body.get("provider", "agentmail")
    if provider == "zoho":
        if set(body) - {"provider", "email", "password", "host"}:
            raise HTTPException(
                400, "Use only Zoho mailbox settings for this provider."
            )
        await run_in_threadpool(
            perform,
            lambda: agent_inbox.default_store().connect_zoho(
                user,
                body.get("email"),
                body.get("password", ""),
                body.get("host", "imappro.zoho.com"),
            ),
        )
        return JSONResponse(perform(lambda: connection_view(user)), headers={'Cache-Control': 'no-store'})
    if provider != "agentmail" or set(body) - {"provider", "inbox_id", "api_key"}:
        raise HTTPException(400, "Choose a supported email inbox provider.")
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


@router.get('/accounts')
def accounts(request: Request):
    user = owner(request)
    return JSONResponse(perform(lambda: email_accounts.default_store().view(user)), headers={'Cache-Control':'no-store'})


@router.post('/accounts/gmail')
async def add_gmail(request: Request):
    user = owner(request, True)
    body = await payload(request, {'account_id'})
    from connections_api import _configured_redirect_uri
    return await run_in_threadpool(perform, lambda: email_accounts.default_store().begin_gmail(
        user, body.get('account_id'), redirect_uri=_configured_redirect_uri()))


@router.post('/accounts')
async def add_account(request: Request):
    user = owner(request, True)
    body = await payload(request, {'provider','email','password','host','inbox_id','api_key'})
    provider = body.get('provider')
    fields = {'provider','email','password','host'} if provider == 'zoho' else {'provider','inbox_id','api_key'}
    if set(body) - fields:
        raise HTTPException(400, 'Use only settings for the selected mailbox provider.')
    return await run_in_threadpool(perform, lambda: email_accounts.default_store().add_dedicated(user, provider, body))


@router.post('/read-scope')
async def read_scope(request: Request):
    user = owner(request, True)
    body = await payload(request, {'selection'})
    return await run_in_threadpool(perform, lambda: email_accounts.default_store().select(user, body.get('selection')))


@router.post('/accounts/{account_id}/disconnect')
async def remove_account(account_id: str, request: Request):
    user = owner(request, True)
    await payload(request, set())
    return await run_in_threadpool(perform, lambda: email_accounts.default_store().disconnect(user, account_id))
