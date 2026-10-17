"""Short-lived native handoffs and session bindings for existing device grants.

This is an authentication adapter, not an executor or task queue. Voice and
Messages retain their existing credentials, leases, send reviews and receipts.
"""
import base64
import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import connectors
from paths import data_dir


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class NativeGrants:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir()/'android_native_grants.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS handoffs (code TEXT PRIMARY KEY, sealed TEXT, expires REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS bindings (token TEXT PRIMARY KEY, capability TEXT, sealed TEXT)')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db: yield db
        finally: db.close()

    def begin(self, owner, capability, challenge, session, origin):
        code = secrets.token_urlsafe(32)
        expiry = time.time()+120
        payload = dict(owner=owner, capability=capability, challenge=challenge, session=session, origin=origin)
        with self.db() as db:
            db.execute('DELETE FROM handoffs WHERE expires <= ?', (time.time(),))
            db.execute('INSERT INTO handoffs VALUES (?,?,?)', (digest(code), connectors.seal_tokens(payload), expiry))
        return {'code': code, 'expires_at': expiry}

    def redeem(self, code, verifier, capability, origin, sessions, issue):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT sealed,expires FROM handoffs WHERE code=?', (digest(code),)).fetchone()
            if not row or row[1] <= time.time(): raise ValueError('Native connection expired. Start again.')
            grant = connectors.unseal_tokens(row[0])
            proof = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            if (grant['capability'] != capability or grant['origin'] != origin or
                    not secrets.compare_digest(grant['challenge'], proof) or
                    sessions.owner_for_reference(grant['session']) != grant['owner']):
                raise ValueError('Native connection changed. Start again.')
            # Existing adapters mint narrow native authority. No token is
            # returned before its session binding is durable. A failed reply
            # is never replayed; the user starts a fresh connection.
            result = issue(grant['owner'], capability)
            token = result['device_token'] if capability == 'voice' else result['upload_token']
            db.execute('INSERT INTO bindings VALUES (?,?,?)', (digest(token), capability,
                connectors.seal_tokens({'owner': grant['owner'], 'session': grant['session']})))
            db.execute('DELETE FROM handoffs WHERE code=?', (digest(code),))
        return {**result, 'owner': grant['owner']}

    def check(self, token, capability, sessions=None):
        with self.db() as db:
            row = db.execute('SELECT capability,sealed FROM bindings WHERE token=?', (digest(token),)).fetchone()
        # The actual provider store still validates legacy credentials. This
        # compatibility path cannot make an unknown token a valid credential.
        if not row: return
        if sessions is None:
            import main
            sessions = main.sessions
        grant = connectors.unseal_tokens(row[1])
        if row[0] != capability or sessions.owner_for_reference(grant['session']) != grant['owner']:
            raise ValueError('Kyrex account link was revoked. Connect again in Kyrex Chat.')


def check_native(token, capability):
    NativeGrants().check(token, capability)
