"""Android SMS/RCS text snapshots with separately enabled confirmed sends. Pairing and revocation are transactional.

The phone credential can only replace its owner's bounded snapshot; it cannot
read Cloud snapshots. With separate phone consent it handles scoped send commands. Message bodies are encrypted with the existing connector key.
"""
import hashlib
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from connectors import seal_tokens, unseal_tokens, ConnectorConfigError
from paths import data_dir

MAX_MESSAGES = 100
PAIR_TTL = 900
PHONE_TTL = 45
PHONE_STATES = {'ready', 'reconnecting', 'needs_attention'}


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
            db.execute('CREATE TABLE IF NOT EXISTS message_controls (owner TEXT PRIMARY KEY, credential TEXT, enabled INTEGER, seen REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS message_presence (owner TEXT PRIMARY KEY, credential TEXT, state TEXT, seen REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS message_sends (id TEXT PRIMARY KEY, owner TEXT, credential TEXT, state TEXT, data TEXT, expires REAL, confirmed REAL, request_key TEXT, UNIQUE(owner, request_key))')
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
            db.execute('DELETE FROM message_controls WHERE owner=?', (row[0],))
            db.execute('DELETE FROM message_presence WHERE owner=?', (row[0],))
            db.execute('DELETE FROM message_sends WHERE owner=?', (row[0],))
        return credential

    def sync(self, credential, messages):
        if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
            raise MessagesError('Upload at most 100 messages')
        clean = []
        for msg in messages:
            if not isinstance(msg, dict):
                raise MessagesError('Invalid message')
            item = {}
            for key, limit in [('sender', 200), ('number', 100), ('received', 100), ('body', 10000), ('id', 200), ('conversation_id', 200), ('conversation', 200), ('kind', 20), ('direction', 20)]:
                value = msg.get(key, '')
                if not isinstance(value, str) or len(value) > limit:
                    raise MessagesError('Invalid message field')
                item[key] = value
            if item['kind'] not in {'', 'SMS', 'MMS', 'RCS', 'UNKNOWN'} or item['direction'] not in {'', 'incoming', 'outgoing', 'unknown'}:
                raise MessagesError('Invalid message metadata')
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
            control = db.execute('SELECT enabled FROM message_controls WHERE owner=?', (_owner(owner),)).fetchone()
        send_enabled = bool(configured and control and control[0])
        connected = bool(configured and row and row[0] and unseal_tokens(row[0]))
        return {'provider': 'device_messages', 'status': 'connected' if connected else 'disconnected',
                'connected': connected, 'usable': connected, 'configured': configured,
                'paired': bool(row), 'send_enabled': send_enabled, 'mode': 'android_companion', 'synced_at': row[1] if row else None, 'read_only': not send_enabled,
                'phone': self.phone_view(owner),
                'capabilities': {'bots': {'messages_reader': {'capabilities': ['messages.read'] if configured else []}}}}

    def heartbeat(self, credential, state):
        if not isinstance(state, str) or state not in PHONE_STATES:
            raise MessagesError('Invalid phone connection state')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            digest = _hash(credential)
            row = db.execute('SELECT owner FROM devices WHERE credential=?', (digest,)).fetchone()
            if not row:
                raise MessagesError('Phone account link was revoked; pair again')
            # Server receipt time, never a phone-supplied timestamp. No command
            # is read or claimed, and heartbeat does not enable sending.
            db.execute('INSERT OR REPLACE INTO message_presence VALUES (?,?,?,?)',
                       (row[0], digest, state, time.time()))
        return {'recorded': True}

    def phone_view(self, owner):
        with self._db() as db:
            row = db.execute('SELECT p.state,p.seen,c.enabled FROM devices d '
                             'LEFT JOIN message_presence p ON p.owner=d.owner AND p.credential=d.credential '
                             'LEFT JOIN message_controls c ON c.owner=d.owner AND c.credential=d.credential '
                             'WHERE d.owner=?', (_owner(owner),)).fetchone()
        if not row:
            return {'status': 'not_linked', 'last_seen': None, 'expires_in': 0, 'send_ready': False}
        state, seen, enabled = row
        remaining = max(0, PHONE_TTL - (time.time() - seen)) if seen is not None else 0
        status = state if remaining > 0 else 'offline' if seen is not None else 'unknown'
        return {'status': status, 'last_seen': seen, 'expires_in': remaining,
                'send_ready': status == 'ready' and bool(enabled)}

    def search(self, owner, query='', limit=10):
        if not isinstance(query, str) or len(query) > 200 or not 1 <= limit <= 20:
            raise MessagesError('Use a query of at most 200 characters and a limit from 1 to 20')
        payload, synced_at = self._snapshot(owner)
        msgs = payload['messages']
        terms = query.casefold().split()
        matches = [m for m in msgs if all(t in ' '.join(m.values()).casefold() for t in terms)]
        return {'messages': matches[:limit], 'synced_at': synced_at, 'snapshot_count': len(msgs)}

    def _snapshot(self, owner):
        with self._db() as db:
            row = db.execute('SELECT snapshot, synced_at FROM devices WHERE owner=?', (_owner(owner),)).fetchone()
        if not row or not row[0]:
            raise MessagesError('Connect Messages and sync your phone in Connections first')
        payload = unseal_tokens(row[0])
        if not payload:
            raise MessagesError('Messages are unavailable; pair and sync your phone again')
        return payload, row[1]

    def conversations(self, owner, limit=5):
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise MessagesError('Request between 1 and 20 conversations')
        payload, synced_at = self._snapshot(owner)
        groups = {}
        for index, message in enumerate(payload['messages']):
            key = (message.get('conversation_id') or message.get('number')
                   or message.get('conversation') or message.get('sender')
                   or f'unknown:{index}')
            rank = _message_recency(message.get('received'), index)
            if key not in groups or rank > groups[key][0]:
                groups[key] = (rank, message)
        ordered = sorted(groups.values(), key=lambda pair: pair[0], reverse=True)
        return {'conversations': [m for _, m in ordered[:limit]],
                'synced_at': synced_at, 'conversation_count': len(groups)}

    def disconnect(self, owner):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            key = _owner(owner)
            db.execute('DELETE FROM devices WHERE owner=?', (key,))
            db.execute('DELETE FROM pairings WHERE owner=?', (key,))
            db.execute('DELETE FROM message_controls WHERE owner=?', (key,))
            db.execute('DELETE FROM message_presence WHERE owner=?', (key,))
            db.execute('DELETE FROM message_sends WHERE owner=?', (key,))


def read_command(text):
    """Only explicit SMS read/search requests; unrelated and write turns bypass."""
    match = re.fullmatch(r'(?is)messages:\s*(latest|search\s+(.{1,200}))\s*', text.strip())
    if match:
        return match.group(2) or ''
    match = re.fullmatch(r'(?is)(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:show|read|check|list|find|search|get|pull up|look at)(?:\s+me)?\s+(?:(?:my|the|latest|recent|most recent)\s+)*(?:texts|text messages|sms(?: messages)?|rcs(?: messages)?)(?:\s+(?:about|for|containing|from|with)\s+(.{1,200}?))?[.!?]*', text.strip())
    return (match.group(1) or '') if match else None


def conversation_command(text):
    """Explicit recent-text-conversation reads; never sends or generic chats."""
    match = re.fullmatch(
        r'(?is)(?:please\s+)?(?:(?:can|could)\s+you\s+)?'
        r'(?:show|list|read|get|check)(?:\s+me)?\s+(?:(?:my|the)\s+)?'
        r'(?:(?:most\s+recent|latest|recent|last)\s+)?'
        r'(?:(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?'
        r'(?:(?:most\s+recent|latest|recent|last)\s+)?'
        r'(?:text|sms|rcs|text\s+message)\s+conversations?[.!?]*', text.strip())
    if not match:
        return None
    words = dict(zip('one two three four five six seven eight nine ten'.split(), range(1, 11)))
    count = match.group(1)
    return words.get(count.lower(), 5) if count and not count.isdigit() else int(count or 5)


def _message_recency(value, index):
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return (1, date.timestamp(), -index)
    except (ValueError, TypeError, OverflowError):
        # Unknown timestamps preserve the phone snapshot's existing order.
        return (0, 0, -index)


def conversations_answer(owner, limit=5):
    try:
        result = MessagesStore().conversations(owner, limit)
    except (MessagesError, ConnectorConfigError) as exc:
        return str(exc)
    stamp = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(result['synced_at']))
    header = f"Recent text conversations — phone snapshot synced {stamp}. Only synced history is included."
    if not result['conversations']:
        return header + '\nNo conversations in this snapshot. Sync now in the phone companion to update it.'
    lines = [header]
    for index, message in enumerate(result['conversations'], 1):
        name = message.get('conversation') or message.get('sender') or message.get('number') or 'Unknown conversation'
        preview = ' '.join(message.get('body', '').split())
        if len(preview) > 160:
            preview = preview[:157] + '…'
        lines.append(f"{index}. {name} — {message.get('received') or 'time unavailable'}\n{preview}")
    return '\n\n'.join(lines)


def answer(owner, query):
    try:
        result = MessagesStore().search(owner, query)
    except (MessagesError, ConnectorConfigError) as exc:
        return str(exc)
    stamp = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(result['synced_at']))
    header = f"Android message snapshot synced {stamp} (up to 100 text messages from 10 recent conversations; attachments and older history are not included)."
    if not result['messages']:
        return header + '\nNo matching messages in this snapshot. A missing match does not mean the message does not exist on your phone.'
    # Return data directly; message text is never interpreted as tool instructions.
    return header + '\n\n' + '\n\n'.join(
        f"Conversation: {m.get('conversation', '')}\nSender: {m['sender'] or m['number']} ({m.get('direction') or 'unknown'}; {m.get('kind') or 'SMS'})\nTime: {m['received']}\n{m['body']}"
        for m in result['messages'])


def connection_view(owner):
    phone = MessagesStore().view(owner)
    if phone['paired']:
        return phone
    # Existing browser pairings remain readable; new connections use the phone.
    import web_messages
    legacy = web_messages.WebMessages().view(owner)
    return legacy if legacy['paired'] else phone


def connected_answer(owner, query):
    if MessagesStore().view(owner)['paired']:
        return answer(owner, query)
    import web_messages
    return web_messages.answer(owner, query)
