"""Optional Google/email sign-in and parent-managed text-only pilot accounts.

No Calendar/Gmail scope is requested by login. Sessions continue to use the
existing durable SessionStore; repositories and connectors remain separate.
"""
import json
import os
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlencode, urlsplit

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

import account_store as accounts

router = APIRouter()
BINDING_COOKIE = 'kyrex_signin'


def configured_google():
    return bool(os.environ.get('KYREX_LOGIN_GOOGLE_CLIENT_ID') and os.environ.get('KYREX_LOGIN_GOOGLE_CLIENT_SECRET'))


def configured_email():
    return bool(os.environ.get('KYREX_LOGIN_SMTP_HOST') and os.environ.get('KYREX_LOGIN_EMAIL_FROM'))


def require_enabled():
    if not accounts.enabled(): raise HTTPException(404, 'Account setup is not enabled.')
    try: accounts.digest('configuration-check')
    except accounts.AccountError as exc: raise HTTPException(503, str(exc)) from None


def same_origin(request):
    origin = request.headers.get('origin')
    if not origin or origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Open account settings from your Kyrex page.')


async def body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 4096: raise HTTPException(413, 'Account request is too large.')
    try:
        data = json.loads(raw)
        if not isinstance(data, dict): raise ValueError()
        return data
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Expected an account object.') from None


def owner(request):
    require_enabled()
    import main
    user = main.require_user(request)
    if user == main.ALLOWED_USERNAME:
        accounts.bootstrap_legacy(user)
    return user


def parent(request):
    user = owner(request)
    account = accounts.get(user)
    if not account or account['kind'] != 'independent' or not account['profile'].get('adult'):
        raise HTTPException(403, 'Sign in to an independent adult account.')
    return user


def limited(request, action, limit=20):
    try: accounts.throttle(action + ':' + str(request.client.host if request.client else 'unknown'), limit, 3600)
    except accounts.AccountError as exc: raise HTTPException(429, str(exc)) from None


def result(value, status=200):
    return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})


def new_session(request, user, target='/', epoch=None):
    import main
    previous = request.cookies.get('session')
    token = secrets.token_hex(32)
    # Persist before revoking the previous session; storage failure cannot log
    # the owner out or exchange a credential for a missing session.
    if not main._live_account(user, epoch): raise HTTPException(403, 'This account session is unavailable.')
    main.sessions.create(token, user, epoch)
    if previous: main.sessions.pop(previous, None)
    main.notification_api.rebind_browser(previous, token, user,
        request.cookies.get(main.notification_api.DEVICE_COOKIE))
    response = RedirectResponse(target, status_code=303,
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
    response.set_cookie('session', token, max_age=main.sessions.ttl_seconds,
                        secure=True, httponly=True, samesite='lax', path='/')
    response.delete_cookie(BINDING_COOKIE, secure=True, httponly=True, samesite='lax')
    return response


def _login_binding(response, binding):
    response.set_cookie(BINDING_COOKIE, binding, max_age=600, secure=True, httponly=True, samesite='lax', path='/')
    return response


def session_result(request, user, epoch=None):
    response = new_session(request, user, epoch=epoch)
    done = result({'signed_in': True})
    for cookie in response.raw_headers:
        if cookie[0] == b'set-cookie': done.raw_headers.append(cookie)
    return done


def _link_context(request, linking):
    if not linking: return {}
    import main
    user = parent(request)
    cookie = request.cookies.get('session')
    if not cookie: raise HTTPException(403, 'Link sign-in methods from your browser account settings.')
    return {'link_owner': user, 'session': main.sessions.reference(cookie)}


def finish_identity(request, value, provider, subject, verified_email=None):
    if value.get('link_owner'):
        import main
        cookie = request.cookies.get('session')
        if not cookie or main.sessions.reference(cookie) != value.get('session') or main.sessions.get(cookie) != value['link_owner']:
            raise accounts.AccountError('Your account session changed. Sign in again before linking.')
        return accounts.link(value['link_owner'], provider, subject)
    if value.get('adult') is not True:
        raise accounts.AccountError('A parent must set up accounts for children.')
    return accounts.admit(provider, subject, verified_email=verified_email)


def github_owner(user_data):
    import main
    login = user_data.get('login')
    if not accounts.enabled():
        if login != main.ALLOWED_USERNAME:
            raise HTTPException(403, 'This GitHub account is not authorized.')
        return login
    if not isinstance(user_data.get('id'), int) or not isinstance(login, str):
        raise HTTPException(502, 'GitHub identity could not be verified.')
    try:
        return accounts.admit('github', str(user_data['id']), github_login=login,
            legacy_owner=login if login == main.ALLOWED_USERNAME else None)['owner']
    except accounts.AccountError as exc:
        raise HTTPException(403, str(exc)) from None


@router.get('/auth/options')
def options():
    import main
    active = accounts.enabled()
    return result({'enabled': active, 'github': bool(main.GITHUB_CLIENT_ID and main.GITHUB_CLIENT_SECRET),
        'google': active and configured_google(), 'email': active and configured_email(),
        'signup_mode': os.environ.get('KYREX_SIGNUP_MODE', 'invite') if active else 'closed',
        'managed': accounts.managed_enabled()})


@router.get('/auth/google/login')
def google_login(request: Request, adult: bool = False, link: bool = False):
    require_enabled()
    if not configured_google(): raise HTTPException(503, 'Google sign-in is not configured.')
    limited(request, 'google-login')
    import main
    redirect = main.login_base_url(request) + '/auth/google/callback'
    binding, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    import base64, hashlib
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    state = accounts.transaction('google', binding, {'nonce': nonce, 'verifier': verifier,
        'redirect': redirect, 'adult': adult, **_link_context(request, link)})
    response = RedirectResponse('https://accounts.google.com/o/oauth2/v2/auth?' + urlencode({
        'client_id': os.environ['KYREX_LOGIN_GOOGLE_CLIENT_ID'], 'redirect_uri': redirect,
        'response_type': 'code', 'scope': 'openid email', 'state': state, 'nonce': nonce,
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'prompt': 'select_account',
    }))
    return _login_binding(response, binding)


@router.get('/auth/google/callback')
def google_callback(request: Request, state: str, code: str = '', error: str = ''):
    require_enabled()
    if not configured_google(): raise HTTPException(503, 'Google sign-in is not configured.')
    try:
        value = accounts.consume(state, 'google', request.cookies.get(BINDING_COOKIE, ''))
        if error or not code: raise accounts.AccountError('Google sign-in was cancelled. Start again when ready.')
        response = requests.post('https://oauth2.googleapis.com/token', data={
            'client_id': os.environ['KYREX_LOGIN_GOOGLE_CLIENT_ID'],
            'client_secret': os.environ['KYREX_LOGIN_GOOGLE_CLIENT_SECRET'],
            'code': code, 'code_verifier': value['verifier'], 'redirect_uri': value['redirect'],
            'grant_type': 'authorization_code'}, timeout=15, allow_redirects=False)
        try:
            if response.status_code != 200 or len(response.content) > 65536:
                raise accounts.AccountError('Google sign-in could not be verified. Start again.')
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2.id_token import verify_oauth2_token
            identity = verify_oauth2_token(response.json().get('id_token', ''), GoogleRequest(),
                audience=os.environ['KYREX_LOGIN_GOOGLE_CLIENT_ID'])
        finally:
            response.close()
        if (identity.get('iss') not in ('https://accounts.google.com', 'accounts.google.com')
                or identity.get('nonce') != value['nonce'] or identity.get('email_verified') is not True
                or not isinstance(identity.get('sub'), str) or not identity['sub']):
            raise accounts.AccountError('Google sign-in could not be verified. Start again.')
        account = finish_identity(request, value, 'google', identity['sub'], identity.get('email'))
        return new_session(request, account['owner'])
    except accounts.AccountError as exc:
        raise HTTPException(400, str(exc)) from None
    except (requests.RequestException, ValueError, KeyError):
        raise HTTPException(502, 'Google sign-in is unavailable. No account was linked.') from None


def send_code(address, code):
    message = EmailMessage()
    message['From'] = os.environ['KYREX_LOGIN_EMAIL_FROM']
    message['To'] = address
    message['Subject'] = 'Your Kyrex sign-in code'
    message.set_content(f'Your Kyrex sign-in code is {code}. It expires in 10 minutes.\n\n'
                        'Enter it only on the Kyrex page where you requested it. If you did not request it, ignore this email.')
    host = os.environ['KYREX_LOGIN_SMTP_HOST']
    port = int(os.environ.get('KYREX_LOGIN_SMTP_PORT', '465'))
    context = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=15, context=context)
    else:
        smtp = smtplib.SMTP(host, port, timeout=15)
    with smtp:
        if port != 465: smtp.starttls(context=context)
        username = os.environ.get('KYREX_LOGIN_SMTP_USERNAME')
        if username: smtp.login(username, os.environ.get('KYREX_LOGIN_SMTP_PASSWORD', ''))
        smtp.send_message(message)


@router.post('/auth/email/start')
async def email_start(request: Request):
    require_enabled(); same_origin(request)
    if not configured_email(): raise HTTPException(503, 'Email sign-in is not configured.')
    limited(request, 'email-start', 10)
    data = await body(request)
    try:
        address = accounts.email(data.get('email'))
        accounts.throttle('email:' + address, 3, 3600)
        binding = secrets.token_urlsafe(32)
        code = f'{secrets.randbelow(100000000):08d}'
        context = _link_context(request, data.get('link') is True)
        token = accounts.transaction('email', binding, {'email': address,
            'code_digest': accounts.digest('email-code:' + code), 'adult': data.get('adult') is True,
            **context}, attempts=5)
        await run_in_threadpool(send_code, address, code)
        return _login_binding(result({'challenge': token, 'message': 'Check your inbox for a sign-in code.'}), binding)
    except accounts.AccountError as exc: raise HTTPException(400, str(exc)) from None
    except (OSError, smtplib.SMTPException, ValueError):
        raise HTTPException(503, 'The sign-in email could not be delivered. Try again later.') from None


@router.post('/auth/email/verify')
async def email_verify(request: Request):
    require_enabled(); same_origin(request); limited(request, 'email-verify', 30)
    data = await body(request)
    try:
        if not isinstance(data.get('challenge'), str) or not isinstance(data.get('code'), str) or len(data['code']) != 8:
            raise accounts.AccountError('Enter the eight-digit code from your email.')
        value = accounts.consume(data['challenge'], 'email', request.cookies.get(BINDING_COOKIE, ''), code=data['code'])
        account = finish_identity(request, value, 'email', value['email'], value['email'])
        return session_result(request, account['owner'])
    except accounts.AccountError as exc: raise HTTPException(400, str(exc)) from None


@router.get('/api/account')
def account(request: Request):
    import main
    user = owner(request)
    return result({'account': accounts.public(accounts.get(user)), 'managed_enabled': accounts.managed_enabled(),
                   'can_invite': user == main.ALLOWED_USERNAME})


@router.put('/api/account/profile')
async def profile(request: Request):
    same_origin(request)
    try: return result({'account': accounts.public(accounts.save_profile(owner(request), await body(request)))})
    except accounts.AccountError as exc: raise HTTPException(400, str(exc)) from None


@router.post('/api/account/invitations')
async def invitation(request: Request):
    same_origin(request)
    import main
    if parent(request) != main.ALLOWED_USERNAME: raise HTTPException(403, 'Only the operator can invite pilot accounts.')
    data = await body(request)
    try:
        accounts.invite(data.get('recipient', ''))
        return result({'invited': True, 'expires_in_days': 7})
    except accounts.AccountError as exc: raise HTTPException(400, str(exc)) from None


@router.get('/api/account/family')
def family(request: Request):
    return result({'members': [accounts.public(a) for a in accounts.children(parent(request))]})


@router.post('/api/account/family')
async def create_child(request: Request):
    same_origin(request)
    try: return result({'account': accounts.public(accounts.create_child(parent(request), await body(request)))}, 201)
    except accounts.AccountError as exc: raise HTTPException(400, str(exc)) from None


@router.patch('/api/account/family/{child}')
async def update_child(child: str, request: Request):
    same_origin(request)
    try: return result({'account': accounts.public(accounts.configure_child(parent(request), child, await body(request)))})
    except accounts.AccountError as exc: raise HTTPException(400, str(exc)) from None


@router.post('/api/account/family/{child}/pair')
def pair_child(child: str, request: Request):
    same_origin(request)
    try:
        if not accounts.managed_enabled(): raise accounts.AccountError('Managed accounts are not enabled.')
        account = accounts.owned_child(parent(request), child)
        if not account['active']: raise accounts.AccountError('Resume this account before pairing a device.')
        token = accounts.transaction('pair', 'managed-device', {'owner': child, 'epoch': accounts.session_epoch(child)}, ttl=600)
        return result({'pairing_code': token, 'expires_in': 600})
    except accounts.AccountError as exc: raise HTTPException(400, str(exc)) from None


@router.post('/auth/managed/pair')
async def redeem_pair(request: Request):
    require_enabled(); same_origin(request); limited(request, 'pair', 20)
    if not accounts.managed_enabled(): raise HTTPException(403, 'Managed accounts are not enabled.')
    data = await body(request)
    try:
        if not isinstance(data.get('code'), str) or len(data['code']) > 100: raise accounts.AccountError('Enter a pairing code.')
        value = accounts.consume(data['code'], 'pair', 'managed-device')
        account = accounts.get(value['owner'])
        if not account or not account['active'] or value.get('epoch') != accounts.session_epoch(value['owner']):
            raise accounts.AccountError('This managed account or pairing request is unavailable.')
        return session_result(request, account['owner'], epoch=value['epoch'])
    except accounts.AccountError as exc: raise HTTPException(400, str(exc)) from None


@router.post('/api/account/family/{child}/revoke-devices')
def revoke_devices(child: str, request: Request):
    same_origin(request)
    try: accounts.owned_child(parent(request), child)
    except accounts.AccountError as exc: raise HTTPException(404, str(exc)) from None
    import main
    accounts.revoke_epoch(child)
    main.sessions.revoke_owner(child)
    accounts.revoke_pairs(child)
    return result({'revoked': True})


@router.delete('/api/account/family/{child}')
async def delete_child(child: str, request: Request):
    same_origin(request)
    user = parent(request)
    try:
        account = accounts.owned_child(user, child)
        data = await body(request)
        if data.get('confirm_name') != account['profile']['name']:
            raise accounts.AccountError('Enter the managed account nickname to confirm deletion.')
        # Pause before removal so existing pairing links and all API sessions
        # stop working, including on another process using this database.
        accounts.configure_child(user, child, {'active': False})
        accounts.revoke_epoch(child)
        import main, chat_service, shutil
        main.sessions.revoke_owner(child)
        shutil.rmtree(chat_service._user_dir(child))
        with accounts.database() as db:
            db.execute('UPDATE accounts SET profile=? WHERE owner=?',
                (json.dumps({'name': 'Deleted managed account', 'complete': True, 'deleted': True}), child))
        return result({'deleted': True})
    except accounts.AccountError as exc: raise HTTPException(400, str(exc)) from None


@router.get('/api/account/family/{child}/conversations')
def child_conversations(child: str, request: Request):
    try: accounts.owned_child(parent(request), child)
    except accounts.AccountError as exc: raise HTTPException(404, str(exc)) from None
    import chat_service
    return result({'conversations': chat_service.list_conversations(child)})


@router.get('/api/account/family/{child}/conversations/{conversation}')
def child_conversation(child: str, conversation: str, request: Request):
    try: accounts.owned_child(parent(request), child)
    except accounts.AccountError as exc: raise HTTPException(404, str(exc)) from None
    import chat_service
    import re
    if not re.fullmatch(r'[0-9a-f]{32}', conversation): raise HTTPException(404, 'Conversation not found.')
    value = chat_service.get_conversation(child, conversation)
    if value is None: raise HTTPException(404, 'Conversation not found.')
    return result(value)


@router.post('/api/account/desktop/finish')
async def desktop_finish(request: Request):
    same_origin(request)
    user = owner(request)
    data = await body(request)
    import main, hashlib, time
    try:
        tx = main._consume_desktop_transaction(str(data.get('transaction', '')))
        if tx['redirect_uri'] != main.CHAT_DESKTOP_REDIRECT_URI:
            raise HTTPException(400, 'Unsupported desktop handoff.')
        code = secrets.token_urlsafe(32)
        with main.desktop_lock:
            main.desktop_handoffs[hashlib.sha256(code.encode()).hexdigest()] = {
                'user': user, 'transaction': data['transaction'], 'redirect_uri': tx['redirect_uri'],
                'epoch': accounts.session_epoch(user),
                'code_challenge': tx['code_challenge'], 'created_at': time.time(),
                'expires_at': time.time() + main.DESKTOP_CODE_TTL_SECONDS, 'consumed': False}
        return result({'url': tx['redirect_uri'] + '?' + urlencode({'code': code, 'state': tx['ide_state']})})
    except KeyError: raise HTTPException(400, 'Invalid desktop handoff.') from None
