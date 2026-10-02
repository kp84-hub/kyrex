"""Read-only Android SMS snapshots. Pairing and revocation are transactional.

The phone credential can only replace its owner's bounded snapshot; it cannot
read Cloud data. Message bodies are encrypted with the existing connector key.
"""
import hashlib
import re
import secrets
import sqlite3
import time
from pathlib import Path

from connectors import seal_tokens, unseal_tokens, ConnectorConfigError
from paths import data_dir

MAX_MESSAGES = 100
PAIR_TTL = 900


class MessagesError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _owner(value):
    if not isinstance(value, str) or not value.strip():
        raise MessagesError('Owner is required')
    return _hash(value)


class MessagesStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / 'device_messages.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS devices (owner TEXT PRIMARY KEY, credential TEXT UNIQUE, snapshot TEXT, synced_at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS pairings (owner TEXT PRIMARY KEY, code TEXT UNIQUE, expires REAL)')
        self.path.chmod(0o600)

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def begin(self, owner):
        seal_tokens({'check': True})  # configuration must work before pairing
        code = secrets.token_urlsafe(24)
        expires = time.time() + PAIR_TTL
        with self._db() as db:
            db.execute('DELETE FROM pairings WHERE expires <= ?', (time.time(),))
            db.execute('INSERT OR REPLACE INTO pairings VALUES (?,?,?)', (_owner(owner), _hash(code), expires))
        return {'pairing_code': code, 'expires_at': expires}

    def redeem(self, code):
        if not isinstance(code, str) or not 20 <= len(code) <= 100:
            raise MessagesError('Invalid or expired pairing code')
        credential = secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT owner, expires FROM pairings WHERE code=?', (_hash(code),)).fetchone()
            if not row or row[1] <= time.time():
                raise MessagesError('Invalid or expired pairing code')
            db.execute('DELETE FROM pairings WHERE owner=?', (row[0],))
            # A new phone replaces the previous credential and clears its data.
            db.execute('INSERT OR REPLACE INTO devices VALUES (?,?,NULL,NULL)', (row[0], _hash(credential)))
        return credential

    def sync(self, credential, messages):
        if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
            raise MessagesError('Upload at most 100 SMS messages')
        clean = []
        for msg in messages:
            if not isinstance(msg, dict):
                raise MessagesError('Invalid SMS message')
            item = {}
            for key, limit in [('sender', 200), ('number', 100), ('received', 100), ('body', 10000)]:
                value = msg.get(key, '')
                if not isinstance(value, str) or len(value) > limit:
                    raise MessagesError('Invalid SMS field')
                item[key] = value
            clean.append(item)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT owner FROM devices WHERE credential=?', (_hash(credential),)).fetchone()
            if not row:
                raise MessagesError('Phone is not paired; pair again in Connections')
            blob = seal_tokens({'messages': clean})
            synced = time.time()
            db.execute('UPDATE devices SET snapshot=?, synced_at=? WHERE owner=?', (blob, synced, row[0]))
        return {'count': len(clean), 'synced_at': synced}

    def view(self, owner):
        configured = True
        try:
            seal_tokens({'check': True})
        except ConnectorConfigError:
            configured = False
        with self._db() as db:
            row = db.execute('SELECT snapshot, synced_at FROM devices WHERE owner=?', (_owner(owner),)).fetchone()
        connected = bool(configured and row and row[0] and unseal_tokens(row[0]))
        return {'provider': 'device_messages', 'status': 'connected' if connected else 'disconnected',
                'connected': connected, 'usable': connected, 'configured': configured,
                'paired': bool(row), 'synced_at': row[1] if row else None, 'read_only': True,
                'capabilities': {'bots': {'messages_reader': {'capabilities': ['messages.read'] if configured else []}}}}

    def search(self, owner, query='', limit=10):
        if not isinstance(query, str) or len(query) > 200 or not 1 <= limit <= 20:
            raise MessagesError('Use a query of at most 200 characters and a limit from 1 to 20')
        with self._db() as db:
            row = db.execute('SELECT snapshot, synced_at FROM devices WHERE owner=?', (_owner(owner),)).fetchone()
        if not row or not row[0]:
            raise MessagesError('Connect Messages and sync your phone in Connections first')
        payload = unseal_tokens(row[0])
        if not payload:
            raise MessagesError('Messages are unavailable; pair and sync your phone again')
        msgs = payload['messages']
        terms = query.casefold().split()
        matches = [m for m in msgs if all(t in ' '.join(m.values()).casefold() for t in terms)]
        return {'messages': matches[:limit], 'synced_at': row[1], 'snapshot_count': len(msgs)}

    def disconnect(self, owner):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            key = _owner(owner)
            db.execute('DELETE FROM devices WHERE owner=?', (key,))
            db.execute('DELETE FROM pairings WHERE owner=?', (key,))


def read_command(text):
    """Only explicit SMS read/search requests; unrelated and write turns bypass."""
    match = re.fullmatch(r'(?is)messages:\s*(latest|search\s+(.{1,200}))\s*', text.strip())
    if match:
        return match.group(2) or ''
    match = re.fullmatch(r'(?is)(?:please\s+)?(?:show|read|check|list|find|search|get)\s+(?:(?:my|the|latest|recent)\s+)*(?:texts|text messages|sms)(?:\s+(?:about|for|containing|from)\s+(.{1,200}?))?[.!?]*', text.strip())
    return (match.group(1) or '') if match else None


def answer(owner, query):
    try:
        result = MessagesStore().search(owner, query)
    except (MessagesError, ConnectorConfigError) as exc:
        return str(exc)
    stamp = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(result['synced_at']))
    header = f"Android SMS snapshot synced {stamp} (up to 100 recent received texts)."
    if not result['messages']:
        return header + '\nNo matching SMS messages in this snapshot. RCS is not included.'
    # Return data directly; message text is never interpreted as tool instructions.
    return header + '\n\n' + '\n\n'.join(
        f"From: {m['sender'] or m['number']}\nReceived: {m['received']}\n{m['body']}"
        for m in result['messages'])
