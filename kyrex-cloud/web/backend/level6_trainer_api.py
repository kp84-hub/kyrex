"""Authenticated owner controls for the fixed Level 6 trainer monitor."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

import bots
import chat_service
import level6_trainer_monitor as monitor
from level6_trainer_store import TrainerStore, conversation_id, starts

router = APIRouter(prefix='/api/automations/level6-trainers', tags=['automations'])


def owner(request):
    import main
    return main.require_user(request)


def view(user):
    import serve
    cfg = TrainerStore().settings(user)
    choices = [{'id': b['id'], 'name': b.get('name') or b['id']}
        for b in bots.load_bots().values()
        if b.get('owner') == user and bots.is_running(b) and serve.is_calendar_bot_policy(b.get('policy'))]
    return {'settings': {key: cfg[key] for key in ('enabled', 'delivery', 'bot_id', 'interval_seconds',
        'horizon_days', 'last_read', 'next_read', 'last_error')},
        'bots': choices, 'service_ready': monitor.enabled(),
        'group_note': monitor.group_ready(user, cfg['bot_id']) if cfg['bot_id'] else 'Choose a Calendar Bot',
        'conversation_id': conversation_id(user)}


@router.get('')
def get_settings(request: Request):
    return view(owner(request))


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool
    delivery: str = Field(pattern=r'^(chat|group)$')
    bot_id: str = Field(max_length=128, pattern=r'^[A-Za-z0-9_-]*$')
    interval_seconds: int = Field(default=3600, ge=900, le=86400)
    horizon_days: int = Field(default=14)


@router.put('')
def save_settings(request: Request, body: Settings):
    user = owner(request)
    try:
        if body.enabled:
            monitor.calendar_context(user, body.bot_id)
        elif body.bot_id:
            bot = bots.get_bot(body.bot_id)
            if not bot or bot.get('owner') != user:
                raise ValueError('Calendar Bot is not owned by this account')
        if body.horizon_days not in (7, 14):
            raise ValueError('Choose a 7 or 14 day horizon')
        chat_service.ensure_level6_trainer_conversation(user)
        TrainerStore().configure(user, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return view(user)


@router.get('/history')
def history(request: Request):
    user = owner(request)
    return {'alerts': [{key: row[key] for key in ('id', 'day', 'version', 'message', 'observed_at',
        'state', 'attempt', 'detail') } | {'starts_at': starts(row['day'])} for row in TrainerStore().history(user)]}


@router.post('/reset')
def reset(request: Request):
    user = owner(request)
    try:
        TrainerStore().reset(user)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {'reset': True}


class Resend(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    confirm: bool
    request_id: str = Field(min_length=16, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


@router.post('/history/{change_id}/resend')
def resend(request: Request, change_id: str, body: Resend):
    user = owner(request)
    if not body.confirm:
        raise HTTPException(422, 'Confirm the manual resend after checking the group')
    try:
        TrainerStore().resend(user, change_id, body.request_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {'queued': True}
