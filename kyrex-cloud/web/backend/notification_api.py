"""Owner-bound inbox, explicit browser consent, and saved-event projection."""
from contextlib import asynccontextmanager
import importlib.util
import json
import logging
import os
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
import chat_notifications as notifications

router = APIRouter(prefix='/api/notifications', tags=['notifications'])
log = logging.getLogger(__name__)
DEVICE_COOKIE = 'kyrex_notification_device'


def owner(request):
    import main
    return main.require_user(request)


def browser_session(request, user):
    import main
    token = request.cookies.get('session')
    if not token or main.sessions.get(token) != user:
        raise HTTPException(401, 'Sign in to Kyrex in this browser to enable device notifications.')
    return main.sessions.reference(token)


def session_owner(reference):
    import main
    return main.sessions.owner_for_reference(reference)


def cookie_device(value):
    if not isinstance(value, str) or len(value) > 256:
        return None
    try:
        identity = notifications.box().decrypt(value.encode()).decode()
        return identity if notifications.ID.fullmatch(identity) else None
    except Exception:
        return None


def remember_device(response, device):
    # This sealed, HttpOnly cookie can only revoke/rebind this browser's saved
    # push consent. It cannot read notifications or authenticate an account.
    response.set_cookie(DEVICE_COOKIE, notifications.box().encrypt(device.encode()).decode(),
                        max_age=365 * 86400, secure=True, httponly=True, samesite='lax')
    return response


def logout_browser(token, device_cookie=None):
    import main
    try:
        store = notifications.NotificationStore()
        if token:
            store.revoke_session(main.sessions.reference(token))
        device = cookie_device(device_cookie)
        device_owner = store.device_owner(device) if device else None
        if device_owner:
            store.unsubscribe(device_owner, device)
    except (sqlite3.Error, OSError):
        log.error('Device notification revocation could not be saved. Browser sign-out will continue.')


def rebind_browser(previous, current, user, device_cookie=None):
    import main
    try:
        store = notifications.NotificationStore()
        reference = main.sessions.reference(current)
        if previous:
            store.rebind_session(main.sessions.reference(previous), reference, user)
        device = cookie_device(device_cookie)
        device_owner = store.device_owner(device) if device else None
        if device_owner == user:
            store.rebind_device(device, user, reference)
        elif device_owner:
            store.unsubscribe(device_owner, device)
    except (sqlite3.Error, OSError):
        log.error('Device notification binding could not be saved. Browser sign-in will continue.')


def same_origin(request):
    origin = request.headers.get('origin')
    if origin:
        url = urlsplit(origin)
        if (url.scheme not in ('http', 'https') or url.netloc.lower() != request.headers.get('host', '').lower()
                or url.username or url.password or url.path or url.query or url.fragment):
            raise HTTPException(403, 'Open notifications from your Kyrex Chat page.')
    if request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, 'Open notifications from your Kyrex Chat page.')


async def body(request):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16000:
            raise HTTPException(413, 'Notification request is too large.')
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except (ValueError, UnicodeDecodeError):
        pass
    raise HTTPException(400, 'Invalid notification request.')


def identity(value):
    if not isinstance(value, str) or not notifications.ID.fullmatch(value):
        raise HTTPException(400, 'Invalid notification identity.')
    return value


def reply(value):
    return JSONResponse(value, headers={'Cache-Control': 'no-store'})


@router.get('')
def inbox(request: Request):
    return reply(notifications.NotificationStore().list(owner(request)))


@router.get('/settings')
def settings(request: Request):
    owner(request)
    try:
        if not importlib.util.find_spec('pywebpush'):
            raise ValueError('Device notifications are unavailable in this server build.')
        _, public = notifications.NotificationStore().vapid()
        return reply({'available': True, 'public_key': public})
    except Exception:
        return reply({'available': False, 'message': 'Device notifications are unavailable. Check the server notification setup.'})


@router.post('/read')
async def mark_read(request: Request):
    user = owner(request); same_origin(request); data = await body(request)
    ids = data.get('ids')
    if not isinstance(ids, list) or len(ids) > 100:
        raise HTTPException(400, 'Choose up to 100 notifications.')
    notifications.NotificationStore().mark_read(user, [identity(i) for i in ids])
    return reply({'updated': True})


@router.get('/devices/{device}')
def device_status(request: Request, device: str):
    user = owner(request); identity(device)
    session = browser_session(request, user)
    row = notifications.NotificationStore().device(user, device, session)
    return reply({'enabled': bool(row), 'error': (row or {}).get('last_error', '')})


@router.post('/devices/{device}')
async def subscribe(request: Request, device: str):
    user = owner(request); same_origin(request); identity(device)
    session = browser_session(request, user); data = await body(request)
    # VAPID's contact is metadata, never a URL we fetch. Derive it from the
    # same-origin browser request unless the operator supplies a contact.
    subject = os.environ.get('KYREX_WEB_PUSH_SUBJECT') or request.headers.get('origin') or str(request.base_url).rstrip('/')
    parsed = urlsplit(subject)
    if parsed.scheme not in ('https', 'mailto') or len(subject) > 300:
        raise HTTPException(503, 'Device notifications need HTTPS or a configured push contact.')
    store = notifications.NotificationStore()
    if not importlib.util.find_spec('pywebpush'):
        raise HTTPException(503, 'Device notifications are unavailable in this server build.')
    try:
        store.vapid()
        store.subscribe(user, device, session, data.get('subscription'), subject, session_owner)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return remember_device(reply({'enabled': True}), device)


@router.delete('/devices/{device}')
def unsubscribe(request: Request, device: str):
    user = owner(request); same_origin(request); identity(device)
    notifications.NotificationStore().unsubscribe(user, device)
    response = reply({'enabled': False})
    if cookie_device(request.cookies.get(DEVICE_COOKIE)) == device:
        response.delete_cookie(DEVICE_COOKIE, secure=True, httponly=True, samesite='lax')
    return response


@router.post('/devices/{device}/test')
def test_device(request: Request, device: str):
    user = owner(request); same_origin(request); identity(device)
    try:
        nid = notifications.NotificationStore().test(user, device, browser_session(request, user))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return reply({'queued': True, 'id': nid})


@router.get('/{notification}')
def detail(request: Request, notification: str):
    row = notifications.NotificationStore().get(owner(request), identity(notification))
    if not row:
        raise HTTPException(404, 'Notification not found for this account.')
    return reply(row)


class NotificationService:
    """Observe existing events and deliver alerts; does not execute work."""
    def __init__(self, tasks, chat, *, store=None, sessions=session_owner, sender=notifications.send_push):
        self.tasks = tasks; self.chat = chat; self.store = store or notifications.NotificationStore()
        self.sessions = sessions; self.sender = sender
        self.store.initialize(tasks.notification_cursor())
        self.stopped = threading.Event(); self.thread = None
        self.next_prune = 0

    def destination(self, task):
        user, cid, bot = task.get('chat_id'), task.get('conversation_id'), task.get('bot_id')
        if not isinstance(user, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', user):
            return None
        registry = self.chat.bots.load_bots()
        if bot and (registry.get(bot) or {}).get('owner') != user:
            return None
        if task.get('parent_delegation_id'):
            delegation = self.tasks.get_delegation(task['parent_delegation_id']) or {}
            if (delegation.get('owner') != user or delegation.get('task_id') != task['task_id']
                    or delegation.get('target_bot_id') != bot):
                return None
            cid, bot = delegation.get('parent_conversation_id'), delegation.get('coordinator_bot_id')
            if (registry.get(bot) or {}).get('owner') != user:
                return None
        if not isinstance(cid, str) or not notifications.ID.fullmatch(cid):
            return None
        try:
            # Verify the persisted owner path directly: reading an inbox must
            # not run Chat's result-recovery or tool/summary projection hooks.
            conv = json.loads(self.chat._conv_path(user, cid).read_text())
        except (OSError, ValueError):
            return None
        # Owner-scoped automation threads may deliberately be Bot-free (the
        # weekly preview is one example). Honor a binding when it exists;
        # do not invent one as a new requirement for observing an owned task.
        if conv.get('conversation_id') != cid or (conv.get('bot_id') is not None and conv.get('bot_id') != bot):
            return None
        return user, cid, str((registry.get(bot) or {}).get('name') or 'Kyrex')[:60]

    def project(self, event):
        if event['type'] != 'status' or not isinstance(event.get('payload'), dict):
            return None
        status = event['payload'].get('status')
        titles = {'done': 'Task finished', 'failed': 'Task needs attention',
                  'awaiting_approval': 'Task review needed', 'cancelled': 'Task stopped'}
        if status not in titles:
            return None
        task = self.tasks.get(event['task_id']) or {}
        if task.get('status') != status:
            return None  # Superseded approval/old transition: no stale alarm.
        target = self.destination(task)
        if not target:
            return None
        source = 'task:' + event['task_id'] + ':' + status
        if status == 'awaiting_approval':
            approval = self.tasks.get_pending_approval(event['task_id']) or {}
            source += ':' + (approval.get('approval_id') or str(event['event_id']))
        return {'owner': target[0], 'source': source, 'kind': status, 'title': target[2] + ' · ' + titles[status],
                'conversation': target[1], 'task': event['task_id']}

    def trainer_alerts(self):
        from paths import data_dir
        from level6_trainer_store import conversation_id
        path = data_dir() / 'level6_trainers.sqlite3'
        if not path.exists():
            return
        # A workflow adapter consumes only its already-visible saved alerts.
        # No new trainer read, group send or browser task is created.
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5) as db:
            rows = db.execute('''SELECT id,owner FROM changes WHERE chat_visible=1
                AND observed_at>=? ORDER BY observed_at DESC LIMIT 500''', (float(self.store.meta('started')),)).fetchall()
        for alert, user in rows:
            if not re.fullmatch(r'[A-Za-z0-9-]{1,39}', user):
                continue
            cid = conversation_id(user)
            if not self.chat._conv_path(user, cid).exists():
                continue
            self.store.record(user, 'trainer:' + alert, 'automation', 'Trainer alert ready', cid)

    def reviewed_calendar_alerts(self):
        # Repair old alerts and interrupted cleanup before push delivery. Only
        # the saved owner-bound outcome can retire an alert, never a mere read.
        from calendar_watch import RESOLVED
        with self.store.db() as db:
            candidates = db.execute("SELECT owner,source,task FROM inbox WHERE archived=0 AND (source LIKE 'calendar-suggestion:%' OR task LIKE 'event-calendar-%') LIMIT 500").fetchall()
        if not candidates:
            return
        with sqlite3.connect(f'file:{self.tasks.db_path}?mode=ro', uri=True, timeout=5) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='calendar_suggestions'").fetchone():
                return
            for owner, source, task in candidates:
                identity = source.removeprefix('calendar-suggestion:') if source.startswith('calendar-suggestion:') else ''
                row = db.execute('SELECT state FROM calendar_suggestions WHERE owner=? AND (id=? OR task_id=?)', (owner, identity, task)).fetchone()
                if row and row[0] in RESOLVED:
                    self.store.resolve(owner, source=source)

    def tick(self):
        for event in self.tasks.notification_events(int(self.store.meta('task_cursor'))):
            self.store.advance(event['event_id'], self.project(event))
        try:
            self.trainer_alerts()
        except (sqlite3.Error, ValueError):
            log.error('Saved workflow alerts could not be checked; task notifications remain available.')
        try:
            self.reviewed_calendar_alerts()
        except (OSError, sqlite3.Error, ValueError):
            log.error('Reviewed Calendar alerts could not be reconciled; cleanup will retry.')
        if time.time() >= self.next_prune:
            self.store.prune(); self.next_prune = time.time() + 3600
        # Bounded batch; provider delays cannot block Chat or task execution.
        for _ in range(5):
            if self.stopped.is_set() or not notifications.deliver_one(self.store, self.sessions, self.sender):
                break

    def start(self):
        def run():
            while not self.stopped.is_set():
                try:
                    self.tick()
                except Exception:
                    log.error('Notification observation/delivery failed; saved events will be checked again.')
                self.stopped.wait(3)
        self.thread = threading.Thread(target=run, name='chat-notifications', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=10)


def install(app, tasks):
    """Compose with existing lifespan services, including browser recovery."""
    app.include_router(router)
    previous = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            import chat_service
            service = NotificationService(tasks, chat_service)
            service.start()
            try:
                yield
            finally:
                service.stop()
    app.router.lifespan_context = lifespan
