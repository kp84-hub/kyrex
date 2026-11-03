"""Authenticated private budget setup. No raw Plaid responses or credentials."""
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

import bots
import budget_bank
import budget_api
import chat_service

router = APIRouter(prefix='/api/automations/budget/bank', tags=['automations'])


def owner(request, mutation=False):
    user = budget_api.owner(request, mutation)
    if mutation:
        from account_api import same_origin
        same_origin(request)
    return user


def store():
    return budget_bank.BankStore(chat_service._task_store())


def view(user):
    value = store().view(user)
    choices = []
    for bot in bots.load_bots().values():
        if bot.get('owner') != user: continue
        try: budget_bank.eligible(user, bot['id'], require_running=False)
        except ValueError: continue
        choices.append({key: bot.get(key, '') for key in ('id', 'name', 'model', 'status')})
    return {**value, 'bots': choices,
        'bot_template': {'name': 'Budget Bot', 'systemPrompt': budget_bank.PROMPT, 'policy': budget_bank.POLICY}}


@router.get('')
def status(request: Request):
    return budget_api.response(budget_api.call(view, owner(request)))


@router.post('/link')
async def link(request: Request):
    user = owner(request, True)
    return budget_api.response(await run_in_threadpool(budget_api.call, store().start, user))


class Flow(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    flow: str = Field(pattern=r'^[a-f0-9]{32}$')


@router.post('/finish')
async def finish(request: Request, body: Flow):
    user = owner(request, True)
    await run_in_threadpool(budget_api.call, store().finish, user, body.flow)
    return budget_api.response(budget_api.call(view, user))


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: str = Field(pattern=r'^[a-f0-9]{32}$')
    enabled: bool
    bot_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')
    selected: list[str] = Field(min_length=1, max_length=100)
    start_date: str = Field(min_length=10, max_length=10)
    starting_balance: str = Field(default='', max_length=32)
    currency: str = Field(default='USD', max_length=3)
    time_zone: str = Field(default='UTC', max_length=64)
    interval_seconds: int = 21600


@router.put('')
async def configure(request: Request, body: Settings):
    user = owner(request, True)
    await run_in_threadpool(budget_api.call, store().configure, user, **body.model_dump())
    for (session_owner, conversation), session in list(chat_service._engine_sessions.items()):
        if session_owner == user and session.bot_id == body.bot_id:
            chat_service.close_engine_session(user, conversation)
    budget_api.call(budget_api.ensure_conversation, user, body.bot_id)
    return budget_api.response(budget_api.call(view, user))


class Revision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: str = Field(pattern=r'^[a-f0-9]{32}$')


@router.post('/pause')
def pause(request: Request, body: Revision):
    user = owner(request, True)
    budget_api.call(store().pause, user, body.revision)
    return budget_api.response(budget_api.call(view, user))


@router.post('/disconnect')
async def disconnect(request: Request, body: Revision):
    user = owner(request, True)
    await run_in_threadpool(budget_api.call, store().disconnect, user, body.revision)
    return budget_api.response(budget_api.call(view, user))


@router.get('/totals')
def totals(request: Request, month: str = ''):
    return budget_api.response(budget_api.call(store().model_view, owner(request), month))
