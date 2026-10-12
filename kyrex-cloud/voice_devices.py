"""Revocable phone credentials scoped to the owner's single voice chat.

Wake audio stays on the phone. Only recognized text reaches the ordinary
Chat execution path; this store retains no audio, transcript or API keys.
"""
import hashlib
import secrets
import sqlite3
import time
from pathlib import Path
from paths import data_dir


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class VoiceDevices:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / 'voice_devices.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS codes (owner TEXT PRIMARY KEY, code TEXT UNIQUE, conversation TEXT, expires REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS devices (owner TEXT PRIMARY KEY, token TEXT UNIQUE, conversation TEXT, expires REAL)')
        self.path.chmod(0o600)

    def db(self):
        return sqlite3.connect(self.path, timeout=10)

    def begin(self, owner, conversation):
        code = secrets.token_urlsafe(24)
        expiry = time.time() + 600
        with self.db() as db:
            db.execute('DELETE FROM codes WHERE expires <= ?', (time.time(),))
            db.execute('INSERT OR REPLACE INTO codes VALUES (?,?,?,?)', (owner, digest(code), conversation, expiry))
        return {'pairing_code': code, 'expires_at': expiry}

    def redeem(self, code):
        if not isinstance(code, str) or not 20 <= len(code) <= 100:
            raise ValueError('Invalid or expired voice pairing code.')
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT owner, conversation, expires FROM codes WHERE code=?', (digest(code),)).fetchone()
            if not row or row[2] <= time.time():
                raise ValueError('Invalid or expired voice pairing code.')
            db.execute('DELETE FROM codes WHERE owner=?', (row[0],))
            db.execute('INSERT OR REPLACE INTO devices VALUES (?,?,?,?)', (row[0], digest(token), row[1], time.time() + 365 * 86400))
        return {'device_token': token, 'conversation_id': row[1]}

    def resolve(self, token):
        if not isinstance(token, str) or not 30 <= len(token) <= 100:
            raise ValueError('Voice phone is not paired.')
        with self.db() as db:
            row = db.execute('SELECT owner, conversation, expires FROM devices WHERE token=?', (digest(token),)).fetchone()
        if not row or row[2] <= time.time():
            raise ValueError('Voice phone pairing expired or was revoked. Pair again.')
        return row[0], row[1]

    def view(self, owner):
        with self.db() as db:
            row = db.execute('SELECT conversation, expires FROM devices WHERE owner=?', (owner,)).fetchone()
            pending = db.execute('SELECT conversation FROM codes WHERE owner=? AND expires>?', (owner, time.time())).fetchone()
        return {'paired': bool(row and row[1] > time.time()),
                'conversation_id': row[0] if row else pending[0] if pending else None}

    def revoke(self, owner):
        with self.db() as db:
            db.execute('DELETE FROM devices WHERE owner=?', (owner,))
            db.execute('DELETE FROM codes WHERE owner=?', (owner,))

