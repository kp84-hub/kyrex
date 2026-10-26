"""Fail-closed HTTP admission for newly registered and managed identities.

Legacy operator routes keep their existing policy. Managed sessions cannot
reach a connector, Bot, upload, provider secret, task, approval or automation.
"""
from fastapi.responses import JSONResponse
import account_store as accounts
from starlette.requests import HTTPConnection


async def boundary(request, call_next):
    path = request.url.path
    if not path.startswith('/api/'):
        return await call_next(request)
    import main
    user = main.get_session_user(request) or main._bearer_user(request)
    if not accounts.isolated(user):
        return await call_next(request)
    account = accounts.get(user)
    if not accounts.enabled() or not account or not account['active']:
        return JSONResponse({'detail': 'This account is paused or unavailable.'}, 403)
    if account['kind'] == 'managed':
        allowed = (
            (path in ('/api/me', '/api/account') and request.method == 'GET')
            or path == '/api/chat' and request.method == 'POST'
            or path == '/api/chat/cancel' and request.method == 'POST'
            or path == '/api/conversations' and request.method in ('GET', 'POST')
            or path.startswith('/api/conversations/') and len(path.split('/')) == 4 and request.method in ('GET', 'DELETE')
            or path == '/api/account/desktop/finish' and request.method == 'POST'
        )
        if not accounts.managed_enabled() or not allowed:
            return JSONResponse({'detail': 'This capability is not enabled for managed learning accounts.'}, 403)
    else:
        if not account['profile'].get('complete') and path not in ('/api/me', '/api/account', '/api/account/profile'):
            return JSONResponse({'detail': 'Complete your account profile first.'}, 403)
        # These paths use the deployment-wide repository/workspace, rather than
        # an explicitly selected owner-scoped Developer Bot repository.
        if path == '/api/task':
            return JSONResponse({'detail': 'Choose your own Developer Bot and repository for coding tasks.'}, 403)
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not main._bearer_user(request):
        origin = request.headers.get('origin')
        if origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Open this action from your Kyrex page.'}, 403)
    return await call_next(request)


class AccountSocketBoundary:
    """Managed sessions have no native/browser/voice websocket surface."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            import main
            connection = HTTPConnection(scope)
            raw_owner = main.sessions.get(connection.cookies.get('session')) or main._bearer_user(connection)
            if accounts.isolated(raw_owner):
                account = accounts.get(raw_owner)
                if not accounts.enabled() or not account or not account['active'] or account['kind'] == 'managed':
                    await send({'type': 'websocket.close', 'code': 1008})
                    return
        await self.app(scope, receive, send)
