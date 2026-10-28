"""Owner-scoped event scanning preferences and exact suggestion review."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

import bots
import calendar_watch as watch
import chat_service
from email_inbox_api import owner as inbox_owner

router = APIRouter(prefix='/api/automations/calendar-events', tags=['automations'])


def owner(request, mutation=False):
    user = inbox_owner(request, mutation)
    import account_store
    account = account_store.get(user) if account_store.isolated(user) else None
    if account and account.get('kind') == 'managed':
        raise HTTPException(403, 'Personal event review is unavailable for managed learning accounts.')
    return user


def call(fn, *args, **kwargs):
    try: return fn(*args, **kwargs)
    except LookupError as exc: raise HTTPException(404, str(exc)) from None
    except ValueError as exc: raise HTTPException(409, str(exc)) from None
    except Exception: raise HTTPException(503, 'Event review is unavailable. Check its saved status before retrying.') from None


def response(value):
    return JSONResponse(value, headers={'Cache-Control':'no-store'})


def settings(user):
    store = watch.WatchStore(chat_service._task_store())
    cfg = store.settings(user)
    return {'settings':{key:cfg[key] for key in ('enabled','emails','messages','bot_id','interval_seconds','last_scan','next_scan','detail')},
        'bots':[{'id':b['id'],'name':b.get('name') or b['id']} for b in bots.load_bots().values()
                if b.get('owner') == user and bots.is_running(b)],
        'conversation_id':watch.conversation_id(user), 'configured':bool(cfg['revision']),
        'timezone':watch.cal_writer.TIMEZONE}


@router.get('')
def get_settings(request: Request):
    return response(call(settings, owner(request)))


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool
    emails: bool
    messages: bool
    bot_id: str = Field(max_length=128, pattern=r'^[A-Za-z0-9_-]*$')
    interval_seconds: int = Field(default=3600)


@router.put('')
async def configure(request: Request, body: Settings):
    user = owner(request, True)
    store = watch.WatchStore(chat_service._task_store())
    await run_in_threadpool(call, store.configure, user, **body.model_dump())
    chat_service.ensure_calendar_suggestions_conversation(user)
    return response(settings(user))


@router.get('/suggestions/{identity}')
def get_suggestion(request: Request, identity: str):
    return response(call(watch.WatchStore(chat_service._task_store()).view, owner(request), identity))


class Revision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: str = Field(pattern=r'^[a-f0-9]{32}$')


class Fields(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    title: str = Field(min_length=1, max_length=200)
    date: str = Field(max_length=10)
    start: str = Field(max_length=5)
    end: str = Field(max_length=5)
    all_day: bool
    location: str = Field(max_length=500)


class Edit(Revision):
    fields: Fields


@router.post('/suggestions/{identity}/edit')
async def edit(request: Request, identity: str, body: Edit):
    return response(await run_in_threadpool(call, watch.WatchStore(chat_service._task_store()).edit,
        owner(request, True), identity, body.revision, body.fields.model_dump()))


@router.post('/suggestions/{identity}/dismiss')
def dismiss(request: Request, identity: str, body: Revision):
    return response(call(watch.WatchStore(chat_service._task_store()).dismiss,
        owner(request, True), identity, body.revision))


@router.post('/suggestions/{identity}/review')
async def review(request: Request, identity: str, body: Revision):
    return response(await run_in_threadpool(call, watch.WatchStore(chat_service._task_store()).review,
        owner(request, True), identity, body.revision))


class Decision(Revision):
    approval_id: str = Field(min_length=1, max_length=128)
    decision: str = Field(pattern=r'^(approve|deny)$')


@router.post('/suggestions/{identity}/decision')
def decision(request: Request, identity: str, body: Decision):
    user = owner(request, True)
    store = watch.WatchStore(chat_service._task_store())
    row = call(store.get, user, identity)
    if row['revision'] != body.revision or row['state'] != 'reviewing' or row['attempted']:
        raise HTTPException(409, 'The saved event review changed. Reload it.')
    if body.decision == 'approve':
        call(store.valid, user, row['settings_revision'])
    task = store.tasks.get(row['task_id']) or {}
    approval = store.tasks.get_pending_approval(row['task_id']) or {}
    if (task.get('chat_id') != user or task.get('executor_prefix') != 'event_calendar'
            or task.get('conversation_id') != watch.conversation_id(user)
            or task.get('status') != 'awaiting_approval' or approval.get('approval_id') != body.approval_id):
        raise HTTPException(409, 'This exact calendar approval is no longer pending.')
    if not store.tasks.record_operator_reply(row['task_id'], 'y' if body.decision == 'approve' else 'n'):
        raise HTTPException(409, 'The calendar approval expired. Reload its status.')
    if body.decision == 'deny':
        store.outcome(user, identity, body.revision, 'dismissed', 'Calendar suggestion declined. No event was added.')
    return response(store.view(user, identity))
