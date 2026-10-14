"""Owner-authenticated HTTPS sending consent; this API never sends mail."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
import connectors
import zoho_send
from email_inbox_api import owner, payload

router = APIRouter(prefix='/api/connections/zoho')


def perform(fn):
    try:
        return fn()
    except connectors.ConnectorError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(503, 'Zoho sending setup is unavailable. Try again from Connections.') from None


@router.get('')
def status(request: Request):
    user = owner(request)
    return JSONResponse(perform(lambda: zoho_send.SendStore().view(user)), headers={'Cache-Control': 'no-store'})


@router.post('/connect')
async def connect(request: Request):
    user = owner(request, True)
    await payload(request, set())
    return await run_in_threadpool(perform, lambda: zoho_send.SendStore().begin(user))


@router.post('/disconnect')
async def disconnect(request: Request):
    user = owner(request, True)
    await payload(request, set())
    return await run_in_threadpool(perform, lambda: zoho_send.SendStore().disconnect(user))


@router.get('/callback')
async def callback(request: Request):
    user = owner(request)
    await run_in_threadpool(perform, lambda: zoho_send.SendStore().complete(user,
        request.query_params.get('state'), request.query_params.get('code')))
    # Never reflect provider input, tokens or authorization codes into HTML.
    return HTMLResponse('<!doctype html><title>Zoho sending connected</title><p>Zoho sending is connected. Return to Kyrex Chat → Connections. Each email still needs your review.</p><a href="/">Return to Kyrex Chat</a>',
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                 'Content-Security-Policy': "default-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
