"""Owner-scoped voice clients and one explicit, renewable listening lease."""
import hashlib
import secrets
import sqlite3
import time
from pathlib import Path
from paths import data_dir


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class VoiceLeaseError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class VoiceDevices:
    LEASE_SECONDS = 45
    # A delayed heartbeat may renew only the exact, unchanged lease. A new
    # claim, explicit Stop or revoked pairing still prevents recovery.
    RECOVERY_SECONDS = 90

    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / 'voice_devices.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS codes (owner TEXT PRIMARY KEY, code TEXT UNIQUE, conversation TEXT, expires REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS voice_clients (id TEXT PRIMARY KEY, owner TEXT, token TEXT UNIQUE, conversation TEXT, expires REAL, name TEXT, platform TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS voice_threads (owner TEXT PRIMARY KEY, conversation TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS voice_leases (owner TEXT PRIMARY KEY, client TEXT, secret TEXT, name TEXT, expires REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS voice_turns (owner TEXT PRIMARY KEY, request TEXT, until REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS voice_code_labels (owner TEXT PRIMARY KEY, name TEXT, platform TEXT)')
            # Preserve v0.1–v0.3 credentials, moving plaintext-free hashes once.
            if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='devices'").fetchone():
                for owner, token, cid, expiry in db.execute('SELECT * FROM devices').fetchall():
                    db.execute('INSERT OR IGNORE INTO voice_clients VALUES (?,?,?,?,?,?,?)',
                               (secrets.token_hex(16), owner, token, cid, expiry, 'Android phone', 'android'))
                    db.execute('INSERT OR IGNORE INTO voice_threads VALUES (?,?)', (owner, cid))
                db.execute('DROP TABLE devices')
        self.path.chmod(0o600)

    def db(self):
        return sqlite3.connect(self.path, timeout=10)

    def remember(self, owner, conversation):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO voice_threads VALUES (?,?)', (owner, conversation))
            db.execute('UPDATE voice_clients SET conversation=? WHERE owner=?', (conversation, owner))

    def begin(self, owner, conversation, name='Android phone', platform='android'):
        if platform not in ('android', 'windows') or not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError('Choose Android or Windows and a device name of up to 60 characters.')
        code = secrets.token_urlsafe(24)
        expiry = time.time() + 600
        with self.db() as db:
            db.execute('DELETE FROM codes WHERE expires <= ?', (time.time(),))
            db.execute('INSERT OR REPLACE INTO codes VALUES (?,?,?,?)', (owner, digest(code), conversation, expiry))
            db.execute('INSERT OR REPLACE INTO voice_code_labels VALUES (?,?,?)', (owner, name.strip(), platform))
            db.execute('INSERT OR REPLACE INTO voice_threads VALUES (?,?)', (owner, conversation))
        return {'pairing_code': code, 'expires_at': expiry}

    def redeem(self, code):
        if not isinstance(code, str) or not 20 <= len(code) <= 100:
            raise ValueError('Invalid or expired voice pairing code.')
        token = secrets.token_urlsafe(32)
        device_id = secrets.token_hex(16)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT owner, conversation, expires FROM codes WHERE code=?', (digest(code),)).fetchone()
            if not row or row[2] <= time.time():
                raise ValueError('Invalid or expired voice pairing code.')
            label = db.execute('SELECT name, platform FROM voice_code_labels WHERE owner=?', (row[0],)).fetchone() or ('Android phone', 'android')
            db.execute('DELETE FROM codes WHERE owner=?', (row[0],))
            if db.execute('SELECT COUNT(*) FROM voice_clients WHERE owner=? AND expires>?', (row[0], time.time())).fetchone()[0] >= 12:
                raise ValueError('Disconnect an old voice device before pairing another.')
            db.execute('INSERT INTO voice_clients VALUES (?,?,?,?,?,?,?)',
                       (device_id, row[0], digest(token), row[1], time.time() + 365 * 86400, *label))
        return {'device_token': token, 'device_id': device_id, 'conversation_id': row[1]}

    def resolve_device(self, token):
        if not isinstance(token, str) or not 30 <= len(token) <= 100:
            raise ValueError('Voice device is not paired.')
        with self.db() as db:
            row = db.execute('SELECT owner, conversation, id, expires FROM voice_clients WHERE token=?', (digest(token),)).fetchone()
        if not row or row[3] <= time.time():
            raise ValueError('Voice pairing expired or was revoked. Pair again.')
        return row[:3]

    def resolve(self, token):
        return self.resolve_device(token)[:2]

    def view(self, owner):
        with self.db() as db:
            clients = db.execute('SELECT id, name, platform, expires FROM voice_clients WHERE owner=? AND expires>?', (owner, time.time())).fetchall()
            row = db.execute('SELECT conversation FROM voice_threads WHERE owner=?', (owner,)).fetchone()
            pending = db.execute('SELECT conversation FROM codes WHERE owner=? AND expires>?', (owner, time.time())).fetchone()
            active = db.execute('SELECT name, expires FROM voice_leases WHERE owner=? AND expires>?', (owner, time.time())).fetchone()
        return {'paired': bool(clients), 'conversation_id': row[0] if row else pending[0] if pending else None,
                'devices': [dict(zip(('id', 'name', 'platform', 'expires_at'), client)) for client in clients],
                'active_device': {'name': active[0], 'expires_at': active[1]} if active else None}

    def revoke(self, owner, device_id=None):
        with self.db() as db:
            if device_id:
                db.execute('DELETE FROM voice_clients WHERE owner=? AND id=?', (owner, device_id))
                db.execute('DELETE FROM voice_leases WHERE owner=? AND client=?', (owner, 'device:' + device_id))
            else:
                db.execute('DELETE FROM voice_clients WHERE owner=?', (owner,))
                db.execute('DELETE FROM codes WHERE owner=?', (owner,))
                db.execute('DELETE FROM voice_leases WHERE owner=?', (owner,))

    def claim(self, owner, client, name, takeover=False):
        secret = secrets.token_urlsafe(32)
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT client, name, expires FROM voice_leases WHERE owner=?', (owner,)).fetchone()
            if row and row[2] > now and row[0] != client and not takeover:
                raise VoiceLeaseError('active', f'Voice is listening on {row[1]}. Select Use this browser to switch.')
            busy = db.execute('SELECT until FROM voice_turns WHERE owner=?', (owner,)).fetchone()
            if busy and busy[0] > now:
                raise ValueError('A voice request is still running. Check the voice chat before switching devices.')
            db.execute('INSERT OR REPLACE INTO voice_leases VALUES (?,?,?,?,?)',
                       (owner, client, digest(secret), name[:60], now + self.LEASE_SECONDS))
        return {'lease': secret, 'expires_at': now + self.LEASE_SECONDS}

    def check(self, owner, client, secret, *, renew=False):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT client, secret, expires FROM voice_leases WHERE owner=?', (owner,)).fetchone()
            if not row:
                raise VoiceLeaseError('stopped', 'Voice listening was stopped. Start listening again.')
            if row[0] != client or not secret or row[1] != digest(secret):
                raise VoiceLeaseError('moved', 'Voice listening moved to another session or device. Start listening again.')
            deadline = row[2] + self.RECOVERY_SECONDS if renew else row[2]
            if deadline <= now:
                raise VoiceLeaseError('expired', 'The voice listening session expired. Start listening again.')
            if renew:
                db.execute('UPDATE voice_leases SET expires=? WHERE owner=?', (now + self.LEASE_SECONDS, owner))
        return {'expires_at': now + self.LEASE_SECONDS if renew else row[2]}

    def release(self, owner, client, secret):
        with self.db() as db:
            db.execute('DELETE FROM voice_leases WHERE owner=? AND client=? AND secret=?', (owner, client, digest(secret)))

    def reserve(self, owner, client, secret, request_id):
        # Authorization and the single in-flight turn are checked atomically.
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT client, secret, expires FROM voice_leases WHERE owner=?', (owner,)).fetchone()
            if not row or row[0] != client or not secret or row[1] != digest(secret) or row[2] <= now:
                raise ValueError('Voice session expired or moved to another device.')
            busy = db.execute('SELECT request, until FROM voice_turns WHERE owner=?', (owner,)).fetchone()
            if busy and busy[1] > now:
                raise ValueError('A voice request is already running. Check your voice chat.')
            db.execute('INSERT OR REPLACE INTO voice_turns VALUES (?,?,?)', (owner, request_id, now + 330))

    def finish(self, owner, request_id):
        # A short cooldown also excludes simultaneous wakes from a second client.
        with self.db() as db:
            db.execute('UPDATE voice_turns SET until=? WHERE owner=? AND request=?', (time.time() + 2, owner, request_id))
