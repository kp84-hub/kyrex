"""Owner-only manual worksheet operations using Budget Bot's existing binding."""
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

import budget_api
import budget_bank_api
import budget_worksheet
import chat_service

router = APIRouter(prefix='/api/automations/budget/worksheet', tags=['automations'])


class Edit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    op: Literal['save', 'balance', 'clear_balance', 'settle', 'undo', 'stop', 'resume']
    revision: str = Field(pattern=r'^(?:[a-f0-9]{32})?$')
    id: str = Field(default='', pattern=r'^(?:[a-f0-9]{32})?$')
    title: str = Field(default='', max_length=80)
    amount: str = Field(default='', max_length=24)
    date: str = Field(default='', max_length=10)
    kind: Literal['expense', 'income'] = 'expense'
    repeat: Literal['once', 'weekly', 'biweekly', 'monthly'] = 'once'


@router.get('')
def get(request: Request):
    owner = budget_bank_api.owner(request)
    return budget_api.response(budget_api.call(view, owner))


@router.patch('')
def edit(request: Request, body: Edit):
    owner = budget_bank_api.owner(request, True)
    return budget_api.response(budget_api.call(view, owner, body))


def view(owner, edit=None):
    store = budget_worksheet.WorksheetStore(chat_service._task_store())
    if edit:
        store.mutate(owner, None, [edit.model_dump(exclude={'revision'})], revision=edit.revision)
    return store.view(owner)
