"""Owner-confirmed phone sends. Claimed sends are never requeued or retried."""
import re
import time
import secrets
from datetime import datetime
from device_messages import MessagesStore, MessagesError, _owner, _hash
from connectors import seal_tokens, unseal_tokens

ACTIVE = ('queued', 'preparing', 'ready', 'send_queued', 'sending')


def resolve_thread(store, owner, recipient=None, context=None):
    result = store.search(owner, '', 20)
    # The resolver needs the complete bounded snapshot, not only 20 search hits.
    with store._db() as db:
        blob = db.execute('SELECT snapshot FROM devices WHERE owner=?', (_owner(owner),)).fetchone()
    messages = unseal_tokens(blob[0]).get('messages', [])
    threads = {}
    for msg in messages:
        cid = msg.get('conversation_id')
        if cid:
            threads.setdefault(cid, []).append(msg)
    if not recipient or recipient.casefold() in {'him', 'her', 'them', 'he', 'she', 'they', 'that conversation'}:
        cid = (context or {}).get('conversation_id')
        if cid not in threads:
            raise MessagesError('Name the conversation, for example: Text Ethan: Are you home?')
        return cid, threads[cid], result['synced_at']
    query = ' '.join(recipient.casefold().split()).strip('"“”')
    def values(rows):
        return {str(m.get(k, '')).casefold().strip() for m in rows for k in ('conversation', 'sender', 'number')} - {'', 'you'}
    exact = [cid for cid, rows in threads.items() if query in values(rows)]
    candidates = exact or [cid for cid, rows in threads.items() if any(re.search(r'(?<!\w)' + re.escape(query) + r'(?!\w)', v) for v in values(rows))]
    if len(candidates) != 1:
        if candidates:
            labels = [threads[cid][0].get('conversation') or cid for cid in candidates]
            raise MessagesError('More than one conversation matches. Use its full conversation name: ' + '; '.join(labels[:10]))
        raise MessagesError('That conversation is not in the current phone snapshot. Open it in Google Messages, sync the companion, then use its exact name or number.')
    return candidates[0], threads[candidates[0]], result['synced_at']


def send_command(text):
    # Preserve message body exactly; only delimiters around the recipient are stripped.
    patterns = [r'(?is)(?:please\s+)?(?:send\s+(?:a\s+)?(?:text|message|sms)\s+to|text)\s+([^:\n]{1,200})\s*:\s?(.{1,1600})',
                r'(?is)(?:please\s+)?send\s+(?:a\s+)?(?:text|message|sms)\s+to\s+(.{1,200}?)\s+(?:saying|that says)\s+(.{1,1600})',
                r'(?is)reply(?:\s+to\s+([^:\n]{1,200}))?\s*:\s?(.{1,1600})']
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return (match.group(1) or '').strip(), match.group(2)
    return None


def reply_command(text):
    text = text.strip().rstrip('?.!')
    match = re.fullmatch(r'(?is)(?:show|read|check|get)\s+(?:me\s+)?(?:the\s+)?(?:latest|last|new)?\s*(?:reply|text|message)(?:\s+from\s+(.{1,200}))?', text)
    if match:
        return {'recipient': (match.group(1) or '').strip(), 'after_send': 'reply' in text.casefold()}
    match = re.fullmatch(r'(?is)what did\s+(.{1,200}?)\s+(?:reply|say)', text)
    if match:
        return {'recipient': match.group(1).strip(), 'after_send': True}
    match = re.fullmatch(r'(?is)did\s+(.{1,200}?)\s+reply', text)
    if match:
        return {'recipient': match.group(1).strip(), 'after_send': True}
    match = re.fullmatch(r"(?is)(?:show|read|check|get)\s+(?:me\s+)?(.{1,200}?)(?:'s|’s)\s+(?:(?:latest|last|new)\s+)?(reply|text|message)", text)
    if match:
        return {'recipient': match.group(1).strip(), 'after_send': match.group(2).casefold() == 'reply'}
    return None


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def latest_reply(owner, recipient='', context=None, after_send=True):
    store = MessagesStore()
    cid, rows, synced = resolve_thread(store, owner, recipient, context)
    threshold = 0
    if after_send:
        threshold = max((_timestamp(m.get('received')) for m in rows if m.get('direction') == 'outgoing'), default=0)
        if (context or {}).get('send_id') and cid == (context or {}).get('conversation_id'):
            with store._db() as db:
                send = db.execute('SELECT confirmed FROM message_sends WHERE id=? AND owner=?', (context['send_id'], _owner(owner))).fetchone()
            if send and send[0]:
                threshold = send[0]
    incoming = [m for m in rows if m.get('direction') == 'incoming' and _timestamp(m.get('received')) > threshold]
    stamp = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(synced))
    label = rows[0].get('conversation') or recipient or cid
    if not incoming:
        return {'content': f'No newer incoming reply found for {label} in the snapshot synced {stamp}. Keep the companion open or tap Sync now, then check again.', 'conversation_id': cid}
    message = max(incoming, key=lambda m: _timestamp(m.get('received')))
    return {'content': f"{message['sender'] or message['number'] or label} — {message['received']}\n\n{message['body']}\n\nSnapshot synced {stamp}.", 'conversation_id': cid}


class SendQueue:
    def __init__(self, store=None):
        self.store = store or MessagesStore()

    def _expire(self, db):
        db.execute("UPDATE message_sends SET state=CASE WHEN state='sending' THEN 'unknown' ELSE 'expired' END WHERE expires<=? AND state IN ('queued','preparing','ready','send_queued','sending')", (time.time(),))
        db.execute('DELETE FROM message_sends WHERE expires<?', (time.time()-86400,))

    def _phone(self, db, credential):
        digest = _hash(credential)
        row = db.execute('SELECT owner FROM devices WHERE credential=?', (digest,)).fetchone()
        if not row:
            raise MessagesError('Phone account link was revoked; pair again')
        return row[0], digest

    def start(self, owner, recipient, text, context=None, request_key=None):
        if not isinstance(text, str) or not text.strip() or len(text)>1600:
            raise MessagesError('Use 1–1600 characters of message text')
        if not isinstance(recipient, str) or len(recipient)>200:
            raise MessagesError('Use a conversation name or phone number')
        cid, rows, _ = resolve_thread(self.store, owner, recipient, context)
        key = _owner(owner)
        request_key = _hash(request_key) if request_key else secrets.token_hex(16)
        with self.store._db() as db:
            db.execute('BEGIN IMMEDIATE'); self._expire(db)
            old = db.execute('SELECT id FROM message_sends WHERE owner=? AND request_key=?', (key, request_key)).fetchone()
            if old:
                job_id = old[0]
            else:
                phone = db.execute('SELECT d.credential,c.enabled FROM devices d LEFT JOIN message_controls c ON c.owner=d.owner AND c.credential=d.credential WHERE d.owner=?', (key,)).fetchone()
                if not phone or not phone[1]:
                    raise MessagesError('In the updated phone companion, enable Allow sends confirmed in Kyrex Chat, then keep it open.')
                active = db.execute("SELECT id FROM message_sends WHERE owner=? AND state IN ('queued','preparing','ready','send_queued','sending')", (key,)).fetchone()
                if active:
                    raise MessagesError('Finish or cancel the current message send before preparing another.')
                job_id = secrets.token_hex(16)
                data = {'conversation_id': cid, 'label': rows[0].get('conversation') or recipient, 'text': text}
                db.execute('INSERT INTO message_sends VALUES (?,?,?,?,?,?,NULL,?)', (job_id, key, phone[0], 'queued', seal_tokens(data), time.time()+300, request_key))
        return self.get(owner, job_id)

    def _public(self, row):
        job_id, state, blob, expires = row
        data = unseal_tokens(blob)
        if not data:
            raise MessagesError('Send preview is unavailable. Nothing will be retried.')
        return {'id': job_id, 'state': state, 'expires_at': expires, 'conversation_id': data['conversation_id'], 'name': data.get('name') or data.get('label'), 'text': data['text'], 'recipients': data.get('recipients', []), 'kind': data.get('kind', 'UNKNOWN')}

    def get(self, owner, job_id):
        with self.store._db() as db:
            db.execute('BEGIN IMMEDIATE'); self._expire(db)
            row = db.execute('SELECT id,state,data,expires FROM message_sends WHERE id=? AND owner=?', (job_id, _owner(owner))).fetchone()
        if not row:
            raise MessagesError('Message send not found for your account')
        return self._public(row)

    def decide(self, owner, job_id, decision):
        if decision not in {'send', 'cancel'}:
            raise MessagesError('Choose Send or Cancel')
        with self.store._db() as db:
            db.execute('BEGIN IMMEDIATE'); self._expire(db)
            row = db.execute('SELECT state FROM message_sends WHERE id=? AND owner=?', (job_id, _owner(owner))).fetchone()
            if not row:
                raise MessagesError('Message send not found for your account')
            if decision == 'cancel':
                if row[0] in {'queued','preparing','ready','send_queued'}:
                    db.execute("UPDATE message_sends SET state='cancelled' WHERE id=?", (job_id,))
                elif row[0] == 'sending':
                    raise MessagesError('Send already claimed by phone. Check Google Messages; it cannot be cancelled now.')
            elif row[0] == 'ready':
                db.execute("UPDATE message_sends SET state='send_queued',confirmed=? WHERE id=?", (time.time(), job_id))
            elif row[0] not in {'send_queued','sending','accepted','unknown'}:
                raise MessagesError('Preview is not ready or has expired. No send was queued.')
        return self.get(owner, job_id)

    def poll(self, credential, enabled):
        if not isinstance(enabled, bool):
            raise MessagesError('Phone sending preference must be true or false')
        with self.store._db() as db:
            db.execute('BEGIN IMMEDIATE'); self._expire(db)
            owner, digest = self._phone(db, credential)
            db.execute('INSERT OR REPLACE INTO message_controls VALUES (?,?,?,?)', (owner, digest, int(enabled), time.time()))
            if not enabled:
                db.execute("UPDATE message_sends SET state='cancelled' WHERE owner=? AND state IN ('queued','preparing','ready','send_queued')", (owner,))
                return {'command': None}
            row = db.execute("SELECT id,state,data,expires FROM message_sends WHERE owner=? AND credential=? AND state IN ('queued','send_queued') ORDER BY expires LIMIT 1", (owner, digest)).fetchone()
            if not row:
                return {'command': None}
            data = unseal_tokens(row[2])
            if not data:
                raise MessagesError('Phone command could not be read')
            action = 'prepare' if row[1] == 'queued' else 'send'
            db.execute('UPDATE message_sends SET state=?, expires=? WHERE id=?', ('preparing' if action=='prepare' else 'sending', row[3] if action=='prepare' else time.time()+120, row[0]))
            command = {'id': row[0], 'action': action, 'conversation_id': data['conversation_id'], 'expires_at': row[3]}
            if action == 'prepare':
                command['text'] = data['text']
            else:
                command['token'] = data['token']
            return {'command': command}

    def acknowledge(self, credential, job_id, action, result):
        if not isinstance(result, dict):
            raise MessagesError('Invalid phone result')
        with self.store._db() as db:
            db.execute('BEGIN IMMEDIATE'); self._expire(db)
            owner, digest = self._phone(db, credential)
            row = db.execute('SELECT state,data FROM message_sends WHERE id=? AND owner=? AND credential=?', (job_id, owner, digest)).fetchone()
            if not row:
                raise MessagesError('Message send not found for this phone')
            expected = 'preparing' if action == 'prepare' else 'sending'
            if action not in {'prepare','send'} or row[0] != expected:
                return {'recorded': False}  # Late or repeated ACK cannot resurrect a cancelled job.
            data = unseal_tokens(row[1])
            if action == 'send':
                state = 'accepted' if result.get('accepted') is True else 'unknown'
                db.execute('UPDATE message_sends SET state=? WHERE id=?', (state, job_id))
            elif result.get('failed') is True:
                db.execute("UPDATE message_sends SET state='failed' WHERE id=?", (job_id,))
            else:
                recipients = result.get('recipients')
                if result.get('text') != data['text'] or result.get('conversation_id') != data['conversation_id'] or not re.fullmatch('[a-f0-9]{32}', str(result.get('token', ''))):
                    raise MessagesError('Phone preview did not match the requested message')
                if not isinstance(recipients, list) or not 1<=len(recipients)<=50 or any(not isinstance(x,str) or not x or len(x)>400 for x in recipients):
                    raise MessagesError('Invalid recipient preview')
                if not isinstance(result.get('name'),str) or len(result['name'])>200 or result.get('kind') not in {'SMS','RCS','UNKNOWN'}:
                    raise MessagesError('Invalid conversation preview')
                data.update({k: result[k] for k in ('token','recipients','name','kind')})
                db.execute("UPDATE message_sends SET state='ready',data=?,expires=? WHERE id=?", (seal_tokens(data), time.time()+90, job_id))
        return {'recorded': True}
