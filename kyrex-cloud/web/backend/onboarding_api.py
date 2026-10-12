"""Account-authenticated first-run setup, available before an LLM is configured."""
import json
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
import chat_api
import chat_onboarding as setup

router = APIRouter()


def owner(request):
    origin = request.headers.get('origin')
    if origin and (urlsplit(origin).scheme not in ('http', 'https') or urlsplit(origin).netloc.lower() != request.headers.get('host', '').lower()):
        raise HTTPException(403, 'Open setup from your Kyrex Chat page.')
    return chat_api._require_user(request)


async def body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 12000: raise HTTPException(413, 'Setup request is too large.')
    try:
        data = json.loads(raw)
        if not isinstance(data, dict): raise ValueError()
        return data
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Expected a setup object.') from None


async def invoke(action, user, *args):
    try:
        return JSONResponse(await run_in_threadpool(action, user, *args), headers={'Cache-Control': 'no-store'})
    except setup.SetupError as exc:
        raise HTTPException(400, str(exc)) from None
    except (RuntimeError, OSError, ValueError):
        raise HTTPException(503, 'Account setup is unavailable. Check server storage and credential encryption.') from None


@router.get('/api/chat/onboarding')
async def status(request: Request):
    return await invoke(setup.status, owner(request))


@router.post('/api/chat/onboarding/provider')
async def provider(request: Request):
    user = owner(request)
    return await invoke(setup.configure, user, await body(request))


@router.get('/api/chat/onboarding/models')
async def models(request: Request):
    return await invoke(setup.models, owner(request))


@router.post('/api/chat/onboarding/test')
async def test(request: Request):
    user = owner(request)
    return await invoke(setup.test, user, await body(request))


@router.post('/api/chat/onboarding/complete')
async def complete(request: Request):
    return await invoke(setup.complete, owner(request))
