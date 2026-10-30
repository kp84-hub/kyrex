"""Authenticated paper-only experiment controls. No live trading endpoints."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

import bots
import crypto_paper as paper
import chat_service
from email_inbox_api import owner as authenticated_owner

router = APIRouter(prefix='/api/automations/crypto-paper', tags=['automations'])


def owner(request, mutation=False):
    user = authenticated_owner(request, mutation)
    import account_store
    if account_store.isolated(user):
        account = account_store.get(user)
        if not account_store.enabled() or not account or not account.get('active') or account.get('kind') == 'managed':
            raise HTTPException(403, 'Paper trading requires an active personal account.')
    return user


def store(): return paper.PaperStore(chat_service._task_store())


def call(fn, *args, **kwargs):
    try: return fn(*args, **kwargs)
    except ValueError as exc: raise HTTPException(409, str(exc)) from None
    except Exception: raise HTTPException(503, 'Paper trading is unavailable. Refresh its saved status.') from None


def view(user, offset=0):
    result = store().dashboard(user, offset)
    available = []
    for bot in bots.load_bots().values():
        try: paper.eligible(user, bot['id'], require_running=False)
        except ValueError: continue
        available.append({key: bot.get(key, '') for key in ('id','name','model','status')})
    return {**result, 'bots': available,
            'bot_template': {'name': 'Crypto Bot', 'systemPrompt': paper.PROMPT, 'policy': paper.POLICY}}


def response(value): return JSONResponse(value, headers={'Cache-Control': 'no-store'})


@router.get('')
async def get_settings(request: Request, offset: int = 0):
    return response(await run_in_threadpool(call, view, owner(request), offset))


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    bot_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    interval_seconds: int = 900
    strategy: str = Field(min_length=1, max_length=2000)
    time_zone: str = Field(default='UTC', max_length=64)
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=8)


@router.put('')
async def configure(request: Request, body: Settings):
    user = owner(request, True)
    await run_in_threadpool(call, store().configure, user, **body.model_dump())
    return response(await run_in_threadpool(call, view, user))


@router.post('/pause')
async def pause(request: Request):
    user = owner(request, True)
    await run_in_threadpool(call, store().pause, user)
    return response(await run_in_threadpool(call, view, user))
