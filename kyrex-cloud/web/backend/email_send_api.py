"""Private, same-origin review and approval of exact dedicated-mailbox drafts."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import connectors
import email_send
from email_inbox_api import owner

router = APIRouter(prefix='/api/email-drafts')


def call(fn, *args):
    try:
        return fn(*args)
    except (email_send.EmailSendError, connectors.ConnectorError) as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, 'Email draft is unavailable. Check its status before retrying a send.') from None


def response(value):
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


async def payload(request, fields):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 32768:
            raise HTTPException(413, 'Email review is too large.')
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != set(fields):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Invalid email review.') from None


@router.get('/{draft_id}')
def get(request: Request, draft_id: str):
    return response(call(email_send.DraftStore().get, owner(request), draft_id))


@router.post('/{draft_id}/edit')
async def edit(request: Request, draft_id: str):
    who = owner(request, True)
    body = await payload(request, {'revision', 'to', 'subject', 'body'})
    return response(await run_in_threadpool(call, email_send.DraftStore().edit,
        who, draft_id, body['revision'], body['to'], body['subject'], body['body']))


@router.post('/{draft_id}/decision')
async def decide(request: Request, draft_id: str):
    who = owner(request, True)
    body = await payload(request, {'revision', 'decision'})
    return response(await run_in_threadpool(call, email_send.DraftStore().decide,
        who, draft_id, body['revision'], body['decision']))


@router.post('/{draft_id}/replies')
async def replies(request: Request, draft_id: str):
    who = owner(request, True)
    await payload(request, set())
    drafts = email_send.DraftStore()
    view = call(drafts.get, who, draft_id)
    result = await run_in_threadpool(call, drafts.replies, who, draft_id)
    # A subsequent "reply to that" or calendar request uses the actual reply.
    if result.get('selected'):
        import chat_service
        conv = chat_service.get_conversation(who, view['conversation_id'])
        if conv:
            chat_service._remember_selected_email(conv, result['selected'])
            chat_service._write(who, conv)
    return response(result)
