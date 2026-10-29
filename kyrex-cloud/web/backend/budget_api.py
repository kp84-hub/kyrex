"""Authenticated Budget Bot setup and encrypted email ledger review."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

import bots
import budget_watch as budget
import chat_service
from email_inbox_api import owner as inbox_owner

router = APIRouter(prefix='/api/automations/budget', tags=['automations'])


def owner(request, mutation=False):
    user = inbox_owner(request, mutation)
    import account_store
    account = account_store.get(user) if account_store.isolated(user) else None
    if account and account.get('kind') == 'managed':
        raise HTTPException(403, 'Budget tracking is unavailable for managed learning accounts.')
    return user


def call(fn, *args, **kwargs):
    try: return fn(*args, **kwargs)
    except (ValueError, LookupError) as exc: raise HTTPException(409, str(exc)) from None
    except Exception: raise HTTPException(503, 'Budget tracking is unavailable. Refresh its saved status before retrying.') from None


def response(value):
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


def store():
    return budget.BudgetStore(chat_service._task_store())


def settings(user):
    cfg = store().settings(user)
    owned = [b for b in bots.load_bots().values() if b.get('owner') == user]
    def view(bot):
        return {key: bot.get(key, '') for key in ('id','name','model','provider_profile_id','status')}
    eligible = []
    for bot in owned:
        try:
            budget.eligible(user, bot['id'], require_running=False)
            eligible.append(view(bot))
        except ValueError: pass
    return {'settings': {key: cfg[key] for key in ('enabled','bot_id','interval_seconds','dollar_currency','time_zone','since','last_scan','detail')},
            'bots': eligible,
            'conversation_id': budget.cid(user), 'configured': bool(cfg['revision']),
            'conversation_ready': chat_service._conv_path(user, budget.cid(user)).exists(),
            'bot_template': {'name': 'Budget Bot', 'systemPrompt': budget.BOT_PROMPT, 'policy': budget.POLICY}}


def ensure_conversation(user, bot_id):
    identity = budget.cid(user)
    conv = chat_service.get_conversation(user, identity)
    if not conv:
        now = chat_service._now_iso()
        conv = {'conversation_id': identity, 'title': 'Budget Bot', 'created_at': now,
                'updated_at': now, 'messages': []}
    conv['bot_id'] = bot_id
    chat_service._write(user, conv)
    return identity


@router.get('')
def get_settings(request: Request):
    return response(call(settings, owner(request)))


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool
    bot_id: str = Field(max_length=128, pattern=r'^[A-Za-z0-9_-]*$')
    interval_seconds: int = 3600
    dollar_currency: str = 'USD'
    time_zone: str = Field(default='UTC', max_length=64)


@router.put('')
async def configure(request: Request, body: Settings):
    user = owner(request, True)
    # Verify the chat binding before enabling scans. Pausing does not require
    # an available Bot or provider: owners can always stop new collection.
    if body.enabled:
        call(chat_service.resolve_bot_for_user, user, body.bot_id)
        call(ensure_conversation, user, body.bot_id)
    await run_in_threadpool(call, store().configure, user, **body.model_dump())
    return response(call(settings, user))


@router.get('/ledger')
def ledger(request: Request, month: str = ''):
    return response(call(store().ledger, owner(request), month))


class Fields(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    merchant: str = Field(min_length=1, max_length=200)
    kind: str
    category: str
    date: str = Field(max_length=10)
    amount: str = Field(max_length=64)
    currency: str = Field(max_length=3)
    cadence: str = Field(max_length=20)
    renewal_date: str = Field(max_length=10)
    state: str


class Edit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: str = Field(pattern=r'^[a-f0-9]{32}$')
    fields: Fields


class Revision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: str = Field(pattern=r'^[a-f0-9]{32}$')


@router.patch('/entries/{identity}')
def edit(request: Request, identity: str, body: Edit):
    user = owner(request, True)
    call(store().edit, user, identity, body.revision, body.fields.model_dump())
    return response({'saved': True})


@router.delete('/entries/{identity}')
def delete(request: Request, identity: str, body: Revision):
    call(store().delete, owner(request, True), identity, body.revision)
    return response({'deleted': True})
