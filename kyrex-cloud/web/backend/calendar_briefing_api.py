"""Authenticated Calendar Briefing preferences."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import calendar_briefing as briefing
import chat_service
import connectors
from email_inbox_api import owner as inbox_owner

router = APIRouter(prefix='/api/automations/calendar-briefing', tags=['automations'])


def owner(request, mutation=False):
    user = inbox_owner(request, mutation)
    import account_store
    account = account_store.get(user) if account_store.isolated(user) else None
    if account and account.get('kind') == 'managed':
        raise HTTPException(403, 'Calendar Briefing is unavailable for managed learning accounts.')
    return user


def view(store, user):
    cfg = store.settings(user)
    return JSONResponse({
        'settings': {key: cfg[key] for key in briefing.DEFAULTS},
        'last_scan': cfg.get('last_scan'), 'detail': cfg.get('detail', ''),
    }, headers={'Cache-Control': 'no-store'})


@router.get('')
def settings(request: Request):
    return view(briefing.BriefingStore(chat_service._task_store()), owner(request))


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    enabled: bool
    calendars: list[str] = Field(max_length=5)
    briefing_at: str = Field(max_length=5)
    quiet_from: str = Field(max_length=5)
    quiet_until: str = Field(max_length=5)


@router.put('')
def save(request: Request, body: Settings):
    user = owner(request, True)
    store = briefing.BriefingStore(chat_service._task_store())
    try:
        store.configure(user, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except connectors.ConnectorError:
        raise HTTPException(409, 'Google Calendar could not be checked. Try again from Connections.') from None
    return view(store, user)
