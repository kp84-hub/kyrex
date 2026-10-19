"""Host-only session-cookie checkpoints for one exclusively held browser profile.

Chrome persists expiring cookies itself. Session cookies need a separate
checkpoint across a viewer/automation restart. No cookie or encryption key is
returned to Cloud. A logout saves an empty checkpoint; a profile reset removes
both files. This cannot renew an expired or revoked server session.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time

KEY_FILE = '.kyrex-session-key'
COOKIE_FILE = '.kyrex-session-cookies'
MAX_BYTES = 4 * 1024 * 1024
MAX_AGE = 7 * 24 * 3600
SAVE_SECONDS = 30


class SessionCookieError(Exception):
    pass


def private_read(directory, name, limit=MAX_BYTES):
    """No symlink following; files must have only owner read/write access."""
    root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
    finally: os.close(root_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > limit:
            raise SessionCookieError('Browser session storage is not private.')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise SessionCookieError('Browser session storage is too large.')
        return data
    finally:
        os.close(fd)


def private_write(directory, name, value, *, replace=True):
    """Atomic 0600 write; retain the profile owner's access across containers."""
    root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    owner = os.fstat(root_fd)
    temp = '.' + secrets.token_hex(16) + '.tmp'
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    try:
        if os.geteuid() == 0:
            os.fchown(fd, owner.st_uid, owner.st_gid)
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(fd)
        if replace:
            os.replace(temp, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        else:
            os.link(temp, name, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
        os.fsync(root_fd)
    finally:
        os.close(fd)
        try: os.unlink(temp, dir_fd=root_fd)
        except FileNotFoundError: pass
        os.close(root_fd)


def cookie_key(cookie):
    # CHIPS cookies with the same name/domain/path can belong to different
    # top-level sites. They must never replace one another.
    return tuple(cookie.get(k, '') for k in ('name', 'domain', 'path', 'partitionKey'))


def session_cookies(cookies):
    result = []
    if not isinstance(cookies, list) or len(cookies) > 10000:
        raise SessionCookieError('Invalid browser session checkpoint.')
    for c in cookies:
        if not isinstance(c, dict) or c.get('expires') != -1:
            continue
        if (not all(isinstance(c.get(k), str) for k in ('name', 'value', 'domain', 'path'))
                or any(len(c[k]) > 65536 for k in ('name', 'value', 'domain', 'path'))
                or not c['domain'] or not c['path'].startswith('/')
                or c.get('sameSite') not in {'Strict', 'Lax', 'None'}
                or not isinstance(c.get('secure'), bool) or not isinstance(c.get('httpOnly'), bool)
                or 'partitionKey' in c and not isinstance(c['partitionKey'], str)):
            raise SessionCookieError('Invalid browser session checkpoint.')
        fields = ('name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite', 'partitionKey')
        result.append({k: c[k] for k in fields if k in c})
    return sorted(result, key=cookie_key)


class SessionCookies:
    def __init__(self, profile, context, *, clock=time.time):
        self.profile = Path(profile)
        if self.profile.is_symlink() or not self.profile.is_dir():
            raise SessionCookieError('Invalid browser session profile.')
        self.context = context
        self.clock = clock
        self.last_saved = 0
        self.last_body = None
        self.last_write = 0
        self.binding = hashlib.sha256(str(self.profile.resolve()).encode()).hexdigest()

    def _cipher(self):
        from cryptography.fernet import Fernet
        try:
            key = private_read(self.profile, KEY_FILE, 128)
        except FileNotFoundError:
            # Losing the key must never silently replace an existing vault.
            if (self.profile / COOKIE_FILE).exists():
                raise SessionCookieError('Browser session key is unavailable.') from None
            key = Fernet.generate_key()
            try: private_write(self.profile, KEY_FILE, key, replace=False)
            except FileExistsError: key = private_read(self.profile, KEY_FILE, 128)
        return Fernet(key)

    def restore(self):
        try: sealed = private_read(self.profile, COOKIE_FILE)
        except FileNotFoundError: return 0
        try:
            saved = json.loads(self._cipher().decrypt(sealed))
            if saved.get('version') != 1 or saved.get('profile') != self.binding:
                raise ValueError()
            if (not isinstance(saved.get('saved_at'), (int, float))
                    or not 0 <= self.clock() - saved['saved_at'] <= MAX_AGE):
                return 0
            held = {cookie_key(c) for c in self.context.cookies()}
            restoring = [c for c in session_cookies(saved['cookies']) if cookie_key(c) not in held]
            if restoring: self.context.add_cookies(restoring)
            return len(restoring)
        except Exception:
            # Never leak a cookie, site, path, key or provider exception.
            raise SessionCookieError('Saved browser sign-in could not be restored.') from None

    def recover_for_manual_sign_in(self):
        """Replace unusable recovery data in the human viewer; keep native cookies."""
        root_fd = os.open(self.profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in (COOKIE_FILE, KEY_FILE):
                try: os.unlink(name, dir_fd=root_fd)
                except FileNotFoundError: pass
            os.fsync(root_fd)
        finally: os.close(root_fd)
        self.last_body = None

    def save(self, *, force=False):
        if not force and self.clock() - self.last_saved < SAVE_SECONDS:
            return
        try:
            cookies = session_cookies(self.context.cookies())
            body = json.dumps(cookies, sort_keys=True, separators=(',', ':'))
            if body != self.last_body or self.clock() - self.last_write >= 3600:
                payload = json.dumps({'version': 1, 'profile': self.binding,
                                      'saved_at': self.clock(), 'cookies': cookies}).encode()
                sealed = self._cipher().encrypt(payload)
                if len(sealed) > MAX_BYTES: raise ValueError()
                private_write(self.profile, COOKIE_FILE, sealed)
                self.last_body = body
                self.last_write = self.clock()
            self.last_saved = self.clock()
        except Exception:
            raise SessionCookieError('Browser sign-in could not be checkpointed.') from None
