"""Owner-authenticated connection and exact review decisions; no model approvals."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
import gmail_cleanup as cleanup
import chat_service
from email_inbox_api import owner as inbox_owner

router = APIRouter(prefix='/api/gmail-cleanup')


def owner(request, mutation=False):
    user = inbox_owner(request,mutation)
    try: cleanup.bot_allowed(user,'')
    except ValueError as exc: raise HTTPException(403,str(exc)) from None
    return user


def store(): return cleanup.CleanupStore(chat_service._task_store())


def call(fn,*args):
    try: return fn(*args)
    except (ValueError,cleanup.connectors.ConnectorError) as exc: raise HTTPException(409,str(exc)) from None
    except Exception: raise HTTPException(503,'Gmail cleanup is unavailable. Refresh its saved status before retrying.') from None


def response(value): return JSONResponse(value,headers={'Cache-Control':'no-store'})


@router.get('')
def status(request: Request): return response(call(store().connection,owner(request)))


@router.post('/connect')
def connect(request: Request):
    from connections_api import _configured_redirect_uri
    return response(call(store().begin,owner(request,True),_configured_redirect_uri()))


@router.post('/disconnect')
def disconnect(request: Request):
    user = owner(request,True); call(store().disconnect,user)
    return response(store().connection(user))


@router.get('/reviews/{identity}')
def get(request: Request, identity: str): return response(call(store().view,owner(request),identity))


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: str = Field(pattern=r'^[a-f0-9]{32}$')
    approval_id: str = Field(min_length=1,max_length=128)
    decision: str = Field(pattern=r'^(trash|cancel)$')


@router.post('/reviews/{identity}/decision')
async def decision(request: Request, identity: str, body: Decision):
    return response(await run_in_threadpool(call,store().decision,owner(request,True),identity,
                    body.revision,body.approval_id,body.decision))
