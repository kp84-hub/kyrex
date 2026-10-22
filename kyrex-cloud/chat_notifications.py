"""Durable notification inbox and Web Push delivery, never a task executor.

Consumes saved task events; delivery retries may repeat a notification but can
never repeat the task. Push payloads contain only an opaque inbox identity.
"""
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from paths import data_dir

ID = re.compile(r'[a-f0-9]{32}\Z')
RETENTION_SECONDS = 30 * 86400
MAX_DEVICES = 10


def box():
    secret = os.environ.get('WEB_SESSION_SECRET') or os.environ.get('KYREX_PROVIDER_SECRETS_KEY')
    if not secret:
        raise ValueError('Device notifications need a stable server session secret.')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('kyrex-notifications:' + secret).encode()).digest()))


def unbase64(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value):
        raise ValueError('Invalid push encryption key.')
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def validate_endpoint(endpoint):
    """Only known browser push services; no arbitrary user-selected HTTP calls."""
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 4096 or any(c.isspace() for c in endpoint):
        raise ValueError('Invalid browser push endpoint.')
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ''
        valid = (host in {'fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com'}
                 or host.endswith('.notify.windows.com'))
        if (url.scheme != 'https' or not valid or url.port not in (None, 443)
                or url.username or url.password or url.fragment or not url.path.startswith('/')
                or url.path == '/'):
            raise ValueError()
    except ValueError:
        raise ValueError('This browser push service is not supported.') from None
    return endpoint


def validate_subscription(value):
    if not isinstance(value, dict):
        raise ValueError('Expected a browser push subscription.')
    endpoint = validate_endpoint(value.get('endpoint'))
    keys = value.get('keys')
    if not isinstance(keys, dict):
        raise ValueError('Missing browser push encryption keys.')
    try:
        raw = unbase64(keys.get('p256dh'))
        if len(raw) != 65 or raw[0] != 4:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
        if len(unbase64(keys.get('auth'))) != 16:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Invalid browser push encryption keys.') from None
    return {'endpoint': endpoint, 'keys': {'p256dh': keys['p256dh'], 'auth': keys['auth']}}


class NotificationStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / 'chat_notifications.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, source TEXT NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, conversation TEXT,
                    task TEXT, created REAL NOT NULL, read REAL,
                    UNIQUE(owner,source));
                CREATE INDEX IF NOT EXISTS inbox_owner ON inbox(owner,created DESC);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, session TEXT NOT NULL,
                    endpoint_hash TEXT UNIQUE NOT NULL, sealed TEXT NOT NULL,
                    subject TEXT NOT NULL, created REAL NOT NULL, last_test REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS deliveries (
                    notification TEXT NOT NULL, device TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL DEFAULT 0, claim TEXT, lease REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(notification,device));
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, latest_event):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('task_cursor', str(latest_event)))
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('started', str(time.time())))

    def meta(self, key):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

    def vapid(self):
        cipher = box()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM meta WHERE key='vapid'").fetchone()
            if row:
                private = serialization.load_pem_private_key(cipher.decrypt(row['value'].encode()), password=None)
            else:
                private = ec.generate_private_key(ec.SECP256R1())
                pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption())
                db.execute('INSERT INTO meta VALUES (?,?)', ('vapid', cipher.encrypt(pem).decode()))
        public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return private, base64.urlsafe_b64encode(public).rstrip(b'=').decode()

    def _record(self, db, owner, source, kind, title, conversation=None, task=None, device=None):
        identity = uuid.uuid4().hex
        inserted = db.execute('''INSERT OR IGNORE INTO inbox
            (id,owner,source,kind,title,conversation,task,created) VALUES (?,?,?,?,?,?,?,?)''',
            (identity, owner, source, kind, title[:100], conversation, task, time.time())).rowcount
        if not inserted:
            return None
        db.execute('''INSERT OR IGNORE INTO deliveries(notification,device)
            SELECT ?,id FROM devices WHERE owner=?''' + (' AND id=?' if device else ''),
            (identity, owner, device) if device else (identity, owner))
        return identity

    def record(self, owner, source, kind, title, conversation=None, task=None):
        with self.db() as db:
            return self._record(db, owner, source, kind, title, conversation, task)

    def advance(self, event_id, notification=None):
        # Inbox/outbox and replay cursor commit together. Duplicate observers
        # cannot lose or multiply an alert when either process restarts.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if notification:
                self._record(db, **notification)
            db.execute("UPDATE meta SET value=CAST(max(CAST(value AS INTEGER),?) AS TEXT) WHERE key='task_cursor'", (event_id,))

    def list(self, owner):
        with self.db() as db:
            rows = db.execute('''SELECT id,kind,title,conversation,task,created,read FROM inbox
                WHERE owner=? ORDER BY created DESC,id DESC LIMIT 100''', (owner,)).fetchall()
            count = db.execute('SELECT count(*) FROM inbox WHERE owner=? AND read IS NULL', (owner,)).fetchone()[0]
        return {'notifications': [dict(r) for r in rows], 'unread': count}

    def get(self, owner, identity):
        with self.db() as db:
            row = db.execute('''SELECT id,kind,title,conversation,task,created,read FROM inbox
                WHERE owner=? AND id=?''', (owner, identity)).fetchone()
        return dict(row) if row else None

    def mark_read(self, owner, ids):
        with self.db() as db:
            for identity in ids:
                db.execute('UPDATE inbox SET read=? WHERE owner=? AND id=? AND read IS NULL',
                           (time.time(), owner, identity))

    def device(self, owner, identity, session):
        with self.db() as db:
            row = db.execute('SELECT id,last_error FROM devices WHERE owner=? AND id=? AND session=?',
                             (owner, identity, session)).fetchone()
        return dict(row) if row else None

    def subscribe(self, owner, identity, session, subscription, subject, session_owner):
        subscription = validate_subscription(subscription)
        digest = hashlib.sha256(subscription['endpoint'].encode()).hexdigest()
        sealed = box().encrypt(json.dumps(subscription).encode()).decode()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # An expired binding cannot block explicit enrollment after a
            # fresh sign-in. Logout removes consent through revoke_session.
            rows = db.execute('SELECT id,owner,session FROM devices WHERE id=? OR endpoint_hash=?', (identity, digest)).fetchall()
            for row in rows:
                if session_owner(row['session']) != row['owner']:
                    self._delete_device(db, row['id'])
                elif row['owner'] != owner or row['session'] != session:
                    raise ValueError('Disable notifications in the other signed-in session first.')
                elif row['id'] != identity:
                    self._delete_device(db, row['id'])
            count = db.execute('SELECT count(*) FROM devices WHERE owner=? AND id!=?', (owner, identity)).fetchone()[0]
            if count >= MAX_DEVICES:
                raise ValueError('The account already has ten notification devices.')
            db.execute('''INSERT INTO devices(id,owner,session,endpoint_hash,sealed,subject,created)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                session=excluded.session,endpoint_hash=excluded.endpoint_hash,sealed=excluded.sealed,
                subject=excluded.subject,last_error='' ''',
                (identity, owner, session, digest, sealed, subject, time.time()))

    @staticmethod
    def _delete_device(db, identity):
        db.execute('DELETE FROM deliveries WHERE device=?', (identity,))
        db.execute('DELETE FROM devices WHERE id=?', (identity,))

    def unsubscribe(self, owner, identity):
        with self.db() as db:
            row = db.execute('SELECT id FROM devices WHERE owner=? AND id=?', (owner, identity)).fetchone()
            if row:
                self._delete_device(db, identity)

    def revoke_session(self, reference):
        with self.db() as db:
            for row in db.execute('SELECT id FROM devices WHERE session=?', (reference,)).fetchall():
                self._delete_device(db, row['id'])

    def rebind_session(self, previous, current, owner):
        with self.db() as db:
            for row in db.execute('SELECT id,owner FROM devices WHERE session=?', (previous,)).fetchall():
                if row['owner'] != owner:
                    self._delete_device(db, row['id'])
                else:
                    db.execute('UPDATE devices SET session=? WHERE id=?', (current, row['id']))

    def test(self, owner, identity, session):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT last_test FROM devices WHERE owner=? AND id=? AND session=?', (owner, identity, session)).fetchone()
            if not row:
                raise ValueError('Enable device notifications in this browser first.')
            if row[0] > time.time() - 60:
                raise ValueError('Wait a minute before sending another test notification.')
            db.execute('UPDATE devices SET last_test=? WHERE id=?', (time.time(), identity))
            return self._record(db, owner, 'test:' + uuid.uuid4().hex, 'test', 'Test notification', device=identity)

    def claim(self):
        now = time.time(); token = uuid.uuid4().hex
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT q.notification,q.device,d.owner,d.session,d.sealed,d.subject,q.attempts
                FROM deliveries q JOIN devices d ON d.id=q.device JOIN inbox n ON n.id=q.notification AND n.owner=d.owner
                WHERE q.state IN ('pending','sending') AND q.due<=? AND q.lease<=? AND n.created>?
                ORDER BY n.created LIMIT 1''', (now, now, now - 86400)).fetchone()
            if not row:
                return None
            db.execute("UPDATE deliveries SET state='sending',claim=?,lease=?,attempts=attempts+1 WHERE notification=? AND device=?",
                       (token, now + 60, row['notification'], row['device']))
        return {**dict(row), 'claim': token}

    def finish(self, row, error='', *, permanent=False):
        attempts = row['attempts'] + 1
        state = 'sent' if not error else 'failed' if permanent or attempts >= 4 else 'pending'
        with self.db() as db:
            changed = db.execute('''UPDATE deliveries SET state=?,due=?,claim=NULL,lease=0
                WHERE notification=? AND device=? AND claim=?''',
                (state, time.time() + min(600, 30 * 2 ** attempts), row['notification'], row['device'], row['claim'])).rowcount
            if changed:
                db.execute('UPDATE devices SET last_error=? WHERE id=?', (error, row['device']))

    def prune(self):
        with self.db() as db:
            db.execute('DELETE FROM inbox WHERE created<?', (time.time() - RETENTION_SECONDS,))
            db.execute('DELETE FROM deliveries WHERE notification NOT IN (SELECT id FROM inbox)')


def send_push(subscription, identity, private, subject):
    import requests
    from py_vapid import Vapid
    from pywebpush import webpush
    validate_subscription(subscription)
    class PushSession(requests.Session):
        def request(self, method, url, **kwargs):
            validate_endpoint(url)
            kwargs['allow_redirects'] = False
            return super().request(method, url, **kwargs)
    # A library transport with redirects disabled keeps an allowlisted push
    # endpoint from redirecting to an internal address or another service.
    with PushSession() as transport:
        response = webpush(subscription_info=subscription,
            data=json.dumps({'id': identity}), vapid_private_key=Vapid(private_key=private),
            vapid_claims={'sub': subject}, ttl=3600, timeout=8, requests_session=transport)
        try:
            return response.status_code
        finally:
            response.close()


def deliver_one(store, session_owner, sender=send_push):
    row = store.claim()
    if not row:
        return False
    # Device consent survives ordinary cookie expiry. Explicit logout/revoke
    # removes the subscription; sign-in is still required to read any content.
    live_owner = session_owner(row['session'])
    if live_owner is not None and live_owner != row['owner']:
        store.unsubscribe(row['owner'], row['device'])
        return True
    try:
        subscription = json.loads(box().decrypt(row['sealed'].encode()))
        private, _ = store.vapid()
        # Revocation is checked again immediately before outbound delivery.
        if not store.device(row['owner'], row['device'], row['session']):
            return True
        status = sender(subscription, row['notification'], private, row['subject'])
    except Exception as exc:
        response = getattr(exc, 'response', None)
        status = getattr(response, 'status_code', None)
        if response is not None:
            response.close()
    if status in (404, 410):
        store.unsubscribe(row['owner'], row['device'])
    elif status is not None and 200 <= status < 300:
        store.finish(row)
    else:
        store.finish(row, 'Device delivery failed. Check browser notification settings.',
                     permanent=status is not None and status < 500 and status != 429)
    return True
