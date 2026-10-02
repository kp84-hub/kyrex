"""Owner-scoped consent for a read-only Google Messages browser connector.

Browser cookies stay on the enrolled host. No SMS snapshot is uploaded/stored.
Pairing screens/input are transient and never included in tasks or audit logs.
"""
import hashlib
import sqlite3
import time
from pathlib import Path

import browser_hosts
import browser_host_channel
from paths import data_dir


class WebMessagesError(ValueError):
    pass


class WebMessages:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / 'web_messages.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS connections (owner TEXT PRIMARY KEY, host TEXT, state TEXT, updated REAL)')
        self.path.chmod(0o600)

    def db(self): return sqlite3.connect(self.path, timeout=10)
    def key(self, owner):
        if not isinstance(owner, str) or not owner.strip(): raise WebMessagesError('Owner required')
        return hashlib.sha256(owner.encode()).hexdigest()

    def record(self, owner):
        with self.db() as db:
            return db.execute('SELECT host,state,updated FROM connections WHERE owner=?', (self.key(owner),)).fetchone()

    def save(self, owner, host, state):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO connections VALUES (?,?,?,?)', (self.key(owner), host, state, time.time()))

    def channel(self, owner, host_id=None):
        manager = browser_host_channel.default_manager()
        if not host_id:
            # Old/offline registrations must not make a live connector
            # ambiguous. Only an authenticated, owner-matched socket with the
            # new capability is eligible. Never borrow another owner's host.
            candidates = []
            legacy_live = False
            for candidate_id in manager.owned_host_ids():
                host = browser_hosts.get_host(candidate_id)
                live = manager.channel_for(candidate_id)
                if (not host or host.owner != owner or not host.is_available()
                        or not live or live.closed or not live.authenticated
                        or live.owner != owner):
                    continue
                if getattr(live, 'messages_connector', False):
                    candidates.append(candidate_id)
                else:
                    legacy_live = True
            if len(candidates) > 1:
                raise WebMessagesError('More than one Messages browser is online; an explicit host selection is required.')
            if not candidates:
                if legacy_live:
                    raise WebMessagesError('Your Browser Host needs the Messages connector update.')
                raise WebMessagesError('Your Messages browser is offline. Check your Browser Host connection.')
            host_id = candidates[0]
        host = browser_hosts.get_host(host_id)
        if not host or host.owner != owner or not host.is_available():
            raise WebMessagesError('Your Messages browser is unavailable. Check your Browser Host connection.')
        channel = manager.channel_for(host.host_id)
        if not channel or channel.closed or not channel.authenticated or channel.owner != owner:
            raise WebMessagesError('Your Messages browser is offline. Reconnect your Browser Host.')
        if not getattr(channel, 'messages_connector', False):
            raise WebMessagesError('Your Browser Host needs the Messages connector update.')
        return host.host_id, channel

    def rpc(self, owner, action, data=None):
        row = self.record(owner)
        if action == 'connect':
            host_id, channel = self.channel(owner, row[0] if row else None)
            self.save(owner, host_id, 'pairing')
            row = self.record(owner)
        else:
            if not row or row[1] not in {'pairing', 'connected'}:
                raise WebMessagesError('Connect Messages in Connections first')
            if action in {'read', 'status'} and row[1] != 'connected':
                raise WebMessagesError('Finish connecting Messages first')
            if action in {'screen', 'input', 'finish'} and row[1] != 'pairing':
                raise WebMessagesError('No active Messages pairing')
            host_id, channel = self.channel(owner, row[0])
        try:
            result = channel.messages_request(owner, action, data)
        except browser_host_channel.ChannelError as exc:
            raise WebMessagesError(str(exc))
        if self.record(owner) != row:
            raise WebMessagesError('Messages connection changed. Try again.')
        if action == 'finish':
            if result.get('connected') is not True:
                raise WebMessagesError('Google Messages pairing was not verified')
            with self.db() as db:
                changed = db.execute("UPDATE connections SET state='connected',updated=? WHERE owner=? AND host=? AND state='pairing' AND updated=?", (time.time(), self.key(owner), host_id, row[2])).rowcount
                if not changed: raise WebMessagesError('Messages pairing was cancelled')
        return result

    def view(self, owner):
        row = self.record(owner)
        ready = False
        try:
            self.channel(owner, row[0] if row else None)
            ready = True
        except WebMessagesError:
            pass
        connected = False
        if row and row[1] == 'connected' and ready:
            try:
                connected = self.rpc(owner, 'status').get('connected') is True
            except WebMessagesError:
                pass
        return {'provider': 'device_messages', 'mode': 'google_messages_web',
                'configured': ready, 'connected': connected, 'usable': connected,
                'status': 'connected' if connected else 'disconnected',
                'paired': bool(row and row[1] in {'pairing', 'connected'}), 'read_only': True,
                'capabilities': {'bots': {'messages_reader': {'capabilities': ['messages.read']}}}}

    def disconnect(self, owner):
        row = self.record(owner)
        # Revoke Cloud reads even if the host is offline. A retained tombstone
        # permits a later retry to erase the host's dedicated profile.
        if row:
            self.save(owner, row[0], 'revoked')
            try:
                _, channel = self.channel(owner, row[0])
                channel.messages_request(owner, 'disconnect')
            except (WebMessagesError, browser_host_channel.ChannelError):
                return {'disconnected': True, 'cleanup_pending': True}
        return {'disconnected': True, 'cleanup_pending': False}


def answer(owner, query):
    try:
        result = WebMessages().rpc(owner, 'read', {'query': query})
    except WebMessagesError as exc:
        return str(exc)
    heading = 'Google Messages — visible text from up to 10 recent conversations; this is a limited search.'
    if not result.get('messages'):
        return heading + '\nNo matching text found in those conversations.'
    # Message content is data, never prompt/tool instructions.
    return heading + '\n\n' + '\n\n'.join(f"Conversation: {m['sender']}\n{m['body']}" for m in result['messages'])
