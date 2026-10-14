"""Owner-reviewed Zoho email drafts. Preparing never sends; HTTPS runs once."""
import hashlib
import json
import re
import smtplib
import sqlite3
import ssl
import time
import uuid
from contextlib import contextmanager
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
from pathlib import Path

import agent_inbox
import connectors
import zoho_inbox
from paths import data_dir

SMTP_HOSTS = {'imappro.zoho.com': 'smtppro.zoho.com', 'imap.zoho.com': 'smtp.zoho.com'}
ADDRESS = re.compile(r'[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
MESSAGE_ID = re.compile(r'<[A-Za-z0-9.!#$%&\'*+\-/=?^_`{|}~]+@[A-Za-z0-9.-]+>')


class EmailSendError(Exception):
    pass


def address(value):
    if not isinstance(value, str) or len(value) > 254 or not ADDRESS.fullmatch(value):
        raise EmailSendError('Use one complete recipient email address. Names, multiple recipients, CC and BCC are not supported yet.')
    return value


def content(to, subject, body):
    to = address(to)
    if (not isinstance(subject, str) or not subject.strip() or len(subject) > 200
            or any(ord(c) < 32 or ord(c) == 127 for c in subject)):
        raise EmailSendError('Use a subject of 1–200 characters without line breaks.')
    if (not isinstance(body, str) or not body.strip() or len(body.encode('utf-8')) > 24000
            or any(ord(c) < 32 and c not in '\r\n\t' for c in body)):
        raise EmailSendError('Use a plain-text message of at most 24 KB. Attachments are not supported yet.')
    return {'to': to, 'subject': subject, 'body': body.replace('\r\n', '\n').replace('\r', '\n')}


def mailbox(store, owner):
    row = store.record(owner)
    if (row.get('source') != 'zoho' or row.get('provider') != 'zoho'
            or not row.get('sealed') or row.get('host') not in SMTP_HOSTS):
        raise EmailSendError('Connect your dedicated Zoho mailbox in Connections → Email inbox. Other email sources remain read-only.')
    zoho_inbox.validate(row.get('address'), row.get('host'))
    return row


def same_mailbox(row, payload):
    return (row.get('source') == row.get('provider') == 'zoho'
            and row.get('sealed') and row.get('revision') == payload['source_revision']
            and row.get('address') == payload['from'] and row.get('host') == payload['host'])


def smtp_send(store, owner, payload, factory=None):
    """TLS, exact sender/recipient and a source lease through SMTP DATA.

    No retries. A dropped connection during DATA cannot prove rejection.
    The source lock serializes disconnect/reconnect against the actual send.
    """
    client = None
    submitted = False
    try:
        row = mailbox(store, owner)
        if not same_mailbox(row, payload):
            return 'failed', 'The mailbox changed. Nothing was sent; prepare a new draft.'
        password = connectors.unseal_tokens(row['sealed']).get('password')
        zoho_inbox.validate_password(password)
        msg = EmailMessage(policy=SMTP)
        msg['From'], msg['To'], msg['Subject'] = payload['from'], payload['to'], payload['subject']
        msg['Date'] = formatdate(localtime=False, usegmt=True)
        msg['Message-ID'] = payload['message_id']
        if payload.get('in_reply_to'):
            msg['In-Reply-To'] = payload['in_reply_to']
            msg['References'] = payload['references']
        msg.set_content(payload['body'], cte='quoted-printable')
        client = (factory or smtplib.SMTP_SSL)(SMTP_HOSTS[payload['host']], port=465,
            context=ssl.create_default_context(), timeout=15)
        client.login(payload['from'], password)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = store.record(owner)
            if not same_mailbox(current, payload):
                return 'failed', 'The mailbox changed. Nothing was sent; prepare a new draft.'
            if client.mail(payload['from'])[0] != 250 or client.rcpt(payload['to'])[0] not in (250, 251):
                return 'failed', 'Zoho rejected the sender or recipient. Nothing was sent.'
            submitted = True
            code, _ = client.data(msg.as_bytes())
            if code == 250:
                return 'accepted', 'Zoho accepted this email. Delivery and appointment confirmation are not yet verified.'
            return 'failed', 'Zoho rejected the email. It was not accepted for delivery.'
    except Exception:
        if submitted:
            return 'unknown', 'The send outcome is unknown. Check Zoho Sent and the recipient before creating another send. No automatic retry.'
        return 'failed', 'Zoho could not send this email. Check the dedicated mailbox app password and SMTP access. Nothing was submitted.'
    finally:
        if client:
            try:
                client.quit()
            except Exception:
                pass


class DraftStore:
    def __init__(self, path=None, inbox=None, sender=None):
        self.path = Path(path) if path else data_dir() / 'email_drafts.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.inbox = inbox or agent_inbox.default_store()
        import zoho_send
        self.sender = sender or zoho_send.send
        self.transport = 'custom' if sender else 'zoho_https'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation TEXT NOT NULL, request_key TEXT, revision TEXT NOT NULL, state TEXT NOT NULL, sealed TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, detail TEXT NOT NULL)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS draft_request ON drafts(owner,request_key) WHERE request_key IS NOT NULL')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _row(self, owner, draft_id, db):
        if not isinstance(draft_id, str) or not re.fullmatch('[a-f0-9]{32}', draft_id):
            raise EmailSendError('Email draft not found.')
        row = db.execute('SELECT * FROM drafts WHERE owner=? AND id=?', (owner, draft_id)).fetchone()
        if row is None:
            raise EmailSendError('Email draft not found.')
        return dict(row)

    def _view(self, row):
        payload = connectors.unseal_tokens(row['sealed'])
        state, detail = row['state'], row['detail']
        if state == 'draft' and time.time() - row['created'] > 86400:
            state, detail = 'expired', 'This review expired. Prepare a new draft; nothing was sent.'
        if state == 'sending' and time.time() - row['updated'] > 300:
            state, detail = 'unknown', 'Send outcome unresolved. Check Zoho Sent before creating another send. No automatic retry.'
        if state == 'draft' and not same_mailbox(self.inbox.record(row['owner']), payload):
            state, detail = 'expired', 'The mailbox changed or disconnected. Prepare a new draft; nothing was sent.'
        return {'id': row['id'], 'conversation_id': row['conversation'], 'revision': row['revision'],
            'state': state, 'detail': detail, 'from': payload['from'], 'to': payload['to'],
            'subject': payload['subject'], 'body': payload['body'], 'reply': bool(payload.get('parent_id')),
            'original_sender': payload.get('original_sender', ''), 'created_at': row['created']}

    def get(self, owner, draft_id):
        with self.db() as db:
            return self._view(self._row(owner, draft_id, db))

    def prepare(self, owner, conversation, to, subject, body, parent_id='', request_key=None):
        if request_key:
            with self.db() as db:
                old = db.execute('SELECT * FROM drafts WHERE owner=? AND request_key=?', (owner, request_key)).fetchone()
                if old:
                    return self._view(dict(old))
        row = mailbox(self.inbox, owner)
        values = content(to, subject, body) if not parent_id else content(row['address'], subject, body)
        parent = {}
        if parent_id:
            parent = self.inbox.reader(owner).reply_context(parent_id)
            values['to'] = address(parent['to'])
            values['subject'] = parent['subject']
        if self.inbox.epoch(owner)[1] != row['revision']:
            raise EmailSendError('The mailbox changed while preparing. Try again with the active mailbox.')
        draft_id = uuid.uuid4().hex
        values.update({'from': row['address'], 'host': row['host'], 'source_revision': row['revision'],
            'message_id': f'<kyrex-{draft_id}@{row["address"].split("@")[1]}>', 'parent_id': parent_id, **parent})
        # Revalidate host-derived reply headers and recipient before storage.
        content(values['to'], values['subject'], values['body'])
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_key:
                old = db.execute('SELECT * FROM drafts WHERE owner=? AND request_key=?', (owner, request_key)).fetchone()
                if old:
                    return self._view(dict(old))
            db.execute('INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,?)', (draft_id, owner, conversation,
                request_key, uuid.uuid4().hex, 'draft', connectors.seal_tokens(values), now, now,
                'Review the exact address and message, then press Send email. Nothing has been sent.'))
        return self.get(owner, draft_id)

    def edit(self, owner, draft_id, revision, to, subject, body):
        values = content(to, subject, body)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(owner, draft_id, db)
            if row['revision'] != revision or self._view(row)['state'] != 'draft':
                raise EmailSendError('The draft changed or is no longer editable. Reload it before making a decision.')
            payload = connectors.unseal_tokens(row['sealed'])
            if payload.get('parent_id') and to != payload['to']:
                raise EmailSendError('A threaded reply must keep its verified recipient. Create a new email to use another address.')
            payload.update(values)
            db.execute('UPDATE drafts SET sealed=?,revision=?,updated=? WHERE id=?',
                (connectors.seal_tokens(payload), uuid.uuid4().hex, time.time(), draft_id))
        return self.get(owner, draft_id)

    def decide(self, owner, draft_id, revision, decision):
        if decision not in {'send', 'cancel'}:
            raise EmailSendError('Choose Send email or Cancel.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(owner, draft_id, db)
            if row['revision'] != revision:
                raise EmailSendError('The draft changed. Review its current text before confirming.')
            view = self._view(row)
            if row['state'] != 'draft':
                return view  # A repeat POST can never repeat submission.
            if decision == 'send' and view['state'] != 'draft':
                raise EmailSendError(view['detail'])
            state = 'cancelled' if decision == 'cancel' else 'sending'
            payload = connectors.unseal_tokens(row['sealed'])
            if decision == 'send' and self.transport == 'zoho_https':
                payload['transport'] = 'zoho_https'
            db.execute('UPDATE drafts SET state=?,updated=?,detail=?,sealed=? WHERE id=?',
                (state, time.time(), 'Cancelled. Nothing sent.' if state == 'cancelled' else 'Sending once. Do not retry.',
                 connectors.seal_tokens(payload), draft_id))
        if decision == 'send':
            try:
                state, detail = self.sender(self.inbox, owner, payload)
            except Exception:
                state, detail = 'unknown', 'Send outcome unknown. Check Zoho Sent. No automatic retry.'
            if state not in {'accepted', 'failed', 'unknown'}:
                state, detail = 'unknown', 'Send outcome unknown. Check Zoho Sent. No automatic retry.'
            with self.db() as db:
                db.execute('UPDATE drafts SET state=?,detail=?,updated=?,sealed=? WHERE owner=? AND id=? AND state=?',
                    (state, detail, time.time(), connectors.seal_tokens(payload), owner, draft_id, 'sending'))
        return self.get(owner, draft_id)

    def replies(self, owner, draft_id):
        with self.db() as db:
            row = self._row(owner, draft_id, db)
        if row['state'] not in {'accepted', 'unknown', 'sending'}:
            raise EmailSendError('This draft has no submitted email to check for replies.')
        payload = connectors.unseal_tokens(row['sealed'])
        if not same_mailbox(self.inbox.record(owner), payload):
            raise EmailSendError('This send belongs to a disconnected or changed mailbox. Check it in Zoho.')
        mid = payload['message_id']
        if payload.get('transport') == 'zoho_https':
            import zoho_send
            mid = zoho_send.SendStore(self.inbox).thread_id(owner, payload)
        result = self.inbox.reader(owner).reply_to(mid, payload['to'])
        return result
