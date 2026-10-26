"""Durable, server-side browser sessions.

Only an HMAC digest of each bearer cookie is stored.  The raw cookie remains
in the browser and is never written to disk.
"""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
import time
from pathlib import Path

from paths import data_dir


class SessionStore:
    """Small dict-compatible session store backed by SQLite."""

    def __init__(self, *, secret: str, path=None, ttl_seconds=7 * 86400,
                 clock=time.time):
        if not secret:
            raise ValueError("a web session secret is required")
        self._secret = str(secret).encode()
        self.path = Path(path) if path is not None else data_dir() / "web_sessions.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = int(ttl_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self):
        with self._lock, self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS web_sessions ("
                "token_digest TEXT PRIMARY KEY, username TEXT NOT NULL, "
                "expires_at REAL NOT NULL)"
            )
            columns = {row[1] for row in conn.execute('PRAGMA table_info(web_sessions)')}
            if 'account_epoch' not in columns:
                conn.execute('ALTER TABLE web_sessions ADD COLUMN account_epoch INTEGER')

    def _digest(self, token) -> str:
        return hmac.new(
            self._secret, str(token).encode(), hashlib.sha256
        ).hexdigest()

    def __setitem__(self, token, username):
        self.create(token, username)

    def create(self, token, username, account_epoch=None):
        import account_store
        if account_epoch is None:
            account_epoch = account_store.session_epoch(username)
        expires_at = float(self._clock()) + self.ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO web_sessions "
                "(token_digest, username, expires_at, account_epoch) VALUES (?, ?, ?, ?)",
                (self._digest(token), str(username), expires_at, account_epoch),
            )

    def get(self, token, default=None):
        if token is None:
            return default
        return self.owner_for_reference(self.reference(token), default)

    def reference(self, token) -> str:
        """Non-bearer reference for a device grant bound to a browser session."""
        return self._digest(token)

    def owner_for_reference(self, digest, default=None):
        """Resolve a live reference without exporting/copying the browser cookie."""
        now = float(self._clock())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT username, expires_at, account_epoch FROM web_sessions "
                "WHERE token_digest = ?", (digest,)
            ).fetchone()
            if not row:
                return default
            import account_store
            if account_store.isolated(row[0]) and row[2] != account_store.session_epoch(row[0]):
                return default
            if float(row[1]) <= now:
                conn.execute(
                    "DELETE FROM web_sessions WHERE token_digest = ?", (digest,)
                )
                return default
            return row[0]

    def __getitem__(self, token):
        marker = object()
        value = self.get(token, marker)
        if value is marker:
            raise KeyError(token)
        return value

    def __contains__(self, token):
        return self.get(token, None) is not None

    def __delitem__(self, token):
        digest = self._digest(token)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM web_sessions WHERE token_digest = ?", (digest,)
            )
            if cursor.rowcount == 0:
                raise KeyError(token)

    def pop(self, token, default=None):
        value = self.get(token, default)
        try:
            del self[token]
        except KeyError:
            pass
        return value

    def clear(self):
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM web_sessions")

    def revoke_owner(self, username):
        """Revoke all paired browser devices without exporting their cookies."""
        with self._lock, self._connect() as conn:
            conn.execute('DELETE FROM web_sessions WHERE username=?', (username,))
