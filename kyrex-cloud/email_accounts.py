"""Owner-owned read mailboxes alongside the existing dedicated sending inbox.

Reuse provider readers and OAuth; this registry selects resources, binds IDs,
and persists bounded search continuations. It cannot send or mutate mail.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import agent_inbox
import connectors as core
from paths import data_dir

ID = re.compile(r'[a-f0-9]{32}\Z')
ADDRESS = re.compile(r'[A-Za-z0-9.!#$%&*+_=?^`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\Z')
MAX_ACCOUNTS = 10


class EmailAccounts:
    def __init__(self, path=None):
        from pathlib import Path
        self.path = Path(path) if path else data_dir() / 'email_accounts.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.credentials = self.path.parent / (self.path.stem + '_credentials')
        self.credentials.mkdir(mode=0o700, exist_ok=True)
        self.credentials.chmod(0o700)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS mailboxes (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, provider TEXT NOT NULL,
                    address TEXT NOT NULL DEFAULT '', revision TEXT NOT NULL,
                    credential TEXT, status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL DEFAULT 0);
                CREATE UNIQUE INDEX IF NOT EXISTS mailbox_address
                    ON mailboxes(owner,provider,address) WHERE address != '';
                CREATE TABLE IF NOT EXISTS preferences (owner TEXT PRIMARY KEY, selection TEXT NOT NULL, revision TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth (state_hash TEXT PRIMARY KEY, owner TEXT, account TEXT,
                    revision TEXT, credential TEXT, redirect TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS refs (id TEXT PRIMARY KEY, owner TEXT, kind TEXT,
                    epoch TEXT, sealed TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS legacy_gmail (owner TEXT PRIMARY KEY, grant TEXT, address TEXT);
            ''')
            db.execute('BEGIN IMMEDIATE')
            if 'created' not in {row[1] for row in db.execute('PRAGMA table_info(mailboxes)')}:
                db.execute('ALTER TABLE mailboxes ADD COLUMN created REAL NOT NULL DEFAULT 0')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _owner(self, owner):
        if not isinstance(owner, str) or not owner.strip():
            raise core.ConnectorError('A signed-in account is required.')
        return owner.strip()

    def _record(self, owner, account):
        self._owner(owner)
        if not isinstance(account, str) or not ID.fullmatch(account):
            raise core.ConnectorError('Choose a connected email account.')
        with self.db() as db:
            row = db.execute('SELECT * FROM mailboxes WHERE owner=? AND id=?', (owner, account)).fetchone()
        if not row:
            raise core.ConnectorUnavailable('This email account is not available.')
        return dict(row)

    def _rows(self, owner):
        self._owner(owner)
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM mailboxes WHERE owner=? ORDER BY rowid', (owner,))]

    def selection(self, owner):
        self._owner(owner)
        with self.db() as db:
            row = db.execute('SELECT selection FROM preferences WHERE owner=?', (owner,)).fetchone()
        return row[0] if row else 'legacy'

    def epoch(self, owner):
        selection = self.selection(owner)
        legacy = agent_inbox.default_store().epoch(owner)
        if selection == 'legacy':
            return legacy
        with self.db() as db:
            pref = db.execute('SELECT revision FROM preferences WHERE owner=?', (owner,)).fetchone()
        google = core.default_store().status(owner) if legacy[0] == 'gmail' else {}
        grant = [google.get('connected_at'), google.get('connected'), google.get('scopes')]
        value = [selection, pref[0] if pref else '', legacy, grant,
                 [(row['id'], row['revision'], row['status']) for row in self._rows(owner)]]
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        return ('none' if selection == 'none' else 'mailboxes', digest)

    def binding(self, owner):
        return self.epoch(owner)[1] if self.selection(owner) != 'legacy' else None

    def select(self, owner, selection):
        self._owner(owner)
        if selection not in ('legacy', 'all', 'none'):
            if self._record(owner, selection)['status'] != 'connected':
                raise core.ConnectorUnavailable('Connect this mailbox first.')
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO preferences VALUES (?,?,?)', (owner, selection, uuid.uuid4().hex))
            db.execute('DELETE FROM refs WHERE owner=?', (owner,))
        return self.view(owner)

    def _google(self, account, credential):
        if not ID.fullmatch(str(account)) or not ID.fullmatch(str(credential)):
            raise core.ConnectorUnavailable('Email credential reference is invalid.')
        return core.ConnectorStore(self.credentials / f'{account}_{credential}.json')

    def _dedicated(self, account, credential):
        if not ID.fullmatch(str(account)) or not ID.fullmatch(str(credential)):
            raise core.ConnectorUnavailable('Email credential reference is invalid.')
        return agent_inbox.AgentInboxStore(self.credentials / f'{account}_{credential}.sqlite3')

    def _remove_credential(self, account, credential):
        if ID.fullmatch(str(account)) and ID.fullmatch(str(credential)):
            for path in self.credentials.glob(account + '_' + credential + '.*'):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # An obsolete file cannot invalidate the new grant.

    def _new(self, owner, provider):
        self._owner(owner)
        identity, revision = uuid.uuid4().hex, uuid.uuid4().hex
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Expired, unfinished additions cannot exhaust an owner's slots.
            expired = list(db.execute('SELECT account,credential FROM oauth WHERE expires<=?', (time.time(),)))
            db.execute('DELETE FROM oauth WHERE expires<=?', (time.time(),))
            db.execute("DELETE FROM mailboxes WHERE owner=? AND status='pending' AND created<? AND id NOT IN (SELECT account FROM oauth)", (owner, time.time()-600))
            if db.execute('SELECT count(*) FROM mailboxes WHERE owner=?', (owner,)).fetchone()[0] >= MAX_ACCOUNTS:
                raise core.ConnectorError('Up to ten additional email accounts can be connected.')
            db.execute('INSERT INTO mailboxes(id,owner,provider,revision,created) VALUES (?,?,?,?,?)', (identity, owner, provider, revision, time.time()))
        for old in expired:
            self._remove_credential(old['account'], old['credential'])
        return self._record(owner, identity)

    def _activate(self, row, address, credential):
        if not isinstance(address, str) or len(address) > 254 or not ADDRESS.fullmatch(address):
            raise core.ConnectorUnavailable('The provider did not confirm the mailbox address.')
        address = address.lower()
        existing = agent_inbox.default_store().record(row['owner'])
        if row['provider'] in ('zoho', 'agentmail') and existing.get('source') == row['provider'] and str(existing.get('address') or '').lower() == address:
            raise core.ConnectorError('This address is already your existing dedicated reader; manage it in Email inbox.')
        if row['address'] and address != row['address']:
            raise core.ConnectorUnavailable('Reconnect using the same email address; add a different account separately.')
        try:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                changed = db.execute("UPDATE mailboxes SET address=?,credential=?,revision=?,status='connected' WHERE owner=? AND id=? AND revision=?",
                    (address, credential, uuid.uuid4().hex, row['owner'], row['id'], row['revision'])).rowcount
                if not changed:
                    raise core.ConnectorUnavailable('This email connection changed or was removed. Start again.')
                db.execute('DELETE FROM refs WHERE owner=?', (row['owner'],))
        except sqlite3.IntegrityError:
            raise core.ConnectorError('This email address is already connected; reconnect its existing entry.') from None
        if row.get('credential'):
            self._remove_credential(row['id'], row['credential'])

    def begin_gmail(self, owner, account=None, redirect_uri=None):
        row = self._record(owner, account) if account else self._new(owner, 'gmail')
        if row['provider'] != 'gmail':
            raise core.ConnectorError('Choose a Gmail account to reconnect.')
        credential = uuid.uuid4().hex
        try:
            store = self._google(row['id'], credential)
            started = store.begin_oauth(owner, redirect_uri=redirect_uri,
                scopes=[core.GOOGLE_GMAIL_READ_SCOPE], pkce=True)
            state = 'mail_' + started['state']
            redirect = redirect_uri or store.client_config()['redirect_uri']
            with self.db() as db:
                if db.execute('SELECT count(*) FROM oauth WHERE owner=? AND expires>?', (owner, time.time())).fetchone()[0] >= 20:
                    raise core.ConnectorError('Too many pending email connections; wait for them to expire.')
                db.execute('INSERT INTO oauth VALUES (?,?,?,?,?,?,?)',
                    (hashlib.sha256(state.encode()).hexdigest(), owner, row['id'], row['revision'], credential, redirect, started['expires_at']))
        except Exception:
            self._remove_credential(row['id'], credential)
            if not account:
                with self.db() as db:
                    db.execute("DELETE FROM mailboxes WHERE owner=? AND id=? AND status='pending'", (owner, row['id']))
            raise
        parts = urlsplit(started['authorization_url'])
        query = dict(parse_qsl(parts.query))
        query.update(state=state, prompt='consent select_account')
        return {'authorization_url': urlunsplit(parts._replace(query=urlencode(query))),
                'expires_at': started['expires_at'], 'account_id': row['id'], 'connection_revision': row['revision']}

    def complete_gmail(self, state, code, *, redirect_uri, exchange=None, transport=None):
        if not isinstance(state, str) or not re.fullmatch(r'mail_[a-f0-9]{64}', state):
            raise core.OAuthStateError('Invalid email authorization state.')
        key = hashlib.sha256(state.encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT * FROM oauth WHERE state_hash=?', (key,)).fetchone()
            if not saved or saved['expires'] <= time.time() or saved['redirect'] != redirect_uri:
                raise core.OAuthStateError('Email authorization expired, was used, or has a different redirect.')
            db.execute('DELETE FROM oauth WHERE state_hash=?', (key,))
        saved = dict(saved)
        try:
            row = self._record(saved['owner'], saved['account'])
            if row['revision'] != saved['revision']:
                raise core.OAuthStateError('The email account changed; start again.')
            store = self._google(row['id'], saved['credential'])
            store.complete_oauth(row['owner'], state[5:], code, redirect_uri=redirect_uri, exchange=exchange)
            if not store.scope_granted(row['owner'], core.GOOGLE_GMAIL_READ_SCOPE):
                raise core.ConnectorUnavailable('Gmail read permission was not granted.')
            token = store.access_token(row['owner'])
            request = transport or core.default_transport
            profile = request('GET', core.PROVIDERS['google']['gmail_api'] + '/users/me/profile', token)
            address = profile.get('emailAddress') if isinstance(profile, dict) else None
            self._activate(row, address, saved['credential'])
        except Exception:
            self._remove_credential(saved['account'], saved['credential'])
            raise
        # Cache only verified identity metadata for an already enabled legacy
        # Gmail reader; never inspect its mail or enable a blocked old grant.
        legacy = core.default_store()
        if (agent_inbox.default_store().record(row['owner'])['source'] == 'gmail'
                and legacy.scope_granted(row['owner'], core.GOOGLE_GMAIL_READ_SCOPE)):
            try:
                old_record = legacy._record(row['owner'], 'google') or {}
                old_token = core.unseal_tokens(old_record.get('sealed')).get('access_token')
                # Optional identity discovery must not refresh or rewrite the
                # Calendar/shared-Google record while adding another account.
                if not old_token:
                    return self.view(row['owner'])
                old_profile = request('GET', core.PROVIDERS['google']['gmail_api'] + '/users/me/profile', old_token)
                old_address = old_profile.get('emailAddress') if isinstance(old_profile, dict) else None
                grant = str(old_record.get('connected_at'))
                if (isinstance(old_address, str) and len(old_address) <= 254 and ADDRESS.fullmatch(old_address)
                        and str(legacy.status(row['owner']).get('connected_at')) == grant
                        and agent_inbox.default_store().record(row['owner'])['source'] == 'gmail'):
                    with self.db() as db:
                        db.execute('INSERT OR REPLACE INTO legacy_gmail VALUES (?,?,?)',
                            (row['owner'], grant, old_address.lower()))
            except Exception:
                pass  # A failed old grant must not block a separate new one.
        return self.view(row['owner'])

    def legacy_address(self, owner):
        if agent_inbox.default_store().record(owner)['source'] != 'gmail':
            return ''
        try:
            status = core.default_store().status(owner)
            if not status.get('connected') or core.GOOGLE_GMAIL_READ_SCOPE not in status.get('scopes', []):
                return ''
            with self.db() as db:
                row = db.execute('SELECT address FROM legacy_gmail WHERE owner=? AND grant=?',
                    (owner, str(status.get('connected_at')))).fetchone()
            return row[0] if row and isinstance(row[0], str) and len(row[0]) <= 254 and ADDRESS.fullmatch(row[0]) else ''
        except Exception:
            return ''

    def add_dedicated(self, owner, provider, fields):
        if provider not in ('zoho', 'agentmail'):
            raise core.ConnectorError('Choose Gmail, Zoho Mail or AgentMail.')
        row = self._new(owner, provider)
        credential = uuid.uuid4().hex
        try:
            store = self._dedicated(row['id'], credential)
            if provider == 'zoho':
                view = store.connect_zoho(owner, fields.get('email'), fields.get('password', ''), fields.get('host', 'imappro.zoho.com'))
            else:
                view = store.connect(owner, fields.get('inbox_id'), fields.get('api_key', ''))
            self._activate(row, view['address'], credential)
        except Exception:
            self.disconnect(owner, row['id'])
            raise
        return self.view(owner)

    def disconnect(self, owner, account):
        self._record(owner, account)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM mailboxes WHERE owner=? AND id=?', (owner, account))
            db.execute('DELETE FROM oauth WHERE owner=? AND account=?', (owner, account))
            db.execute('DELETE FROM refs WHERE owner=?', (owner,))
            db.execute("UPDATE preferences SET selection='none',revision=? WHERE owner=? AND selection=?", (uuid.uuid4().hex, owner, account))
        # Deleting local grants never revokes shared Google Calendar consent.
        for path in self.credentials.glob(account + '_*'):
            path.unlink(missing_ok=True)
        return self.view(owner)

    def view(self, owner):
        legacy = agent_inbox.default_store().view(owner)
        def safe_address(value):
            return value if isinstance(value, str) and len(value) <= 254 and ADDRESS.fullmatch(value) else ''
        address = safe_address(legacy.get('address')) if legacy.get('active') and legacy.get('source') in ('zoho', 'agentmail') else self.legacy_address(owner)
        accounts = [{'id': 'legacy', 'provider': legacy['source'],
            'address': address, 'status': 'connected' if address else 'legacy',
            'label': address or 'Existing email reader', 'read_only': True}]
        accounts += [{'id': row['id'], 'provider': row['provider'], 'address': safe_address(row['address']),
            'label': safe_address(row['address']) or 'Authorization pending', 'status': row['status'],
            'connection_revision': row['revision'], 'read_only': True}
            for row in self._rows(owner)]
        return {'accounts': accounts, 'selection': self.selection(owner),
                'gmail_configured': all(str(os.environ.get(name) or '').strip() for name in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REDIRECT_URI'))}

    def available(self, owner, legacy_store):
        scope = self.selection(owner)
        if scope == 'none':
            return False
        if scope == 'all':
            if any(row['status'] == 'connected' for row in self._rows(owner)):
                return True
            return self._legacy_available(owner, legacy_store)
        if scope == 'legacy':
            return self._legacy_available(owner, legacy_store)
        return self._record(owner, scope)['status'] == 'connected'

    def _legacy_available(self, owner, legacy_store):
        view = agent_inbox.default_store().view(owner)
        return bool(view['active'] or (view['source'] == 'gmail' and legacy_store.scope_granted(owner, core.GOOGLE_GMAIL_READ_SCOPE)))

    def reader(self, owner, *, legacy_store=None, transport=None):
        return MailboxRead(self, owner, legacy_store or core.default_store(), transport)

    def save_ref(self, owner, kind, epoch, value, ttl=7*86400):
        identity = 'mx1_' + uuid.uuid4().hex
        sealed = core.seal_tokens(value)
        with self.db() as db:
            db.execute('DELETE FROM refs WHERE expires<=?', (time.time(),))
            if db.execute('SELECT count(*) FROM refs WHERE owner=?', (owner,)).fetchone()[0] >= 10000:
                raise core.ConnectorUnavailable('Email reference limit reached; try again after older references expire.')
            db.execute('INSERT INTO refs VALUES (?,?,?,?,?,?)', (identity, owner, kind, json.dumps(epoch), sealed, time.time()+ttl))
        return identity

    def load_ref(self, owner, identity, kind, epoch):
        if not isinstance(identity, str) or not re.fullmatch(r'mx1_[a-f0-9]{32}', identity):
            raise core.ConnectorError('Search this mailbox again to select an email.')
        with self.db() as db:
            row = db.execute('SELECT sealed FROM refs WHERE id=? AND owner=? AND kind=? AND epoch=? AND expires>?',
                (identity, owner, kind, json.dumps(epoch), time.time())).fetchone()
        value = core.unseal_tokens(row[0]) if row else {}
        if not value:
            raise core.ConnectorUnavailable('This email reference expired or its account settings changed. Search again.')
        return value


class MailboxRead:
    def __init__(self, registry, owner, legacy_store, transport=None):
        self.registry, self.owner, self.legacy_store, self.transport = registry, owner, legacy_store, transport
        self.epoch = registry.epoch(owner)
        scope = registry.selection(owner)
        rows = [row for row in registry._rows(owner) if row['status'] == 'connected']
        self.rows = {row['id']: row for row in rows if scope in ('all', row['id'])}
        if any(not isinstance(row['address'], str) or len(row['address']) > 254 or not ADDRESS.fullmatch(row['address']) for row in self.rows.values()):
            raise core.ConnectorUnavailable('A selected mailbox has invalid identity metadata. Reconnect it in Email accounts.')
        if scope in ('all', 'legacy') and registry._legacy_available(owner, legacy_store):
            legacy = agent_inbox.default_store().view(owner)
            address = registry.legacy_address(owner) if legacy['source'] == 'gmail' else legacy.get('address')
            if not isinstance(address, str) or len(address) > 254 or not ADDRESS.fullmatch(address):
                address = ''
            duplicate_gmail = legacy['source'] == 'gmail' and address and any(
                row['provider'] == 'gmail' and row['address'].lower() == address.lower() for row in self.rows.values())
            if not duplicate_gmail:
                self.rows['legacy'] = {'id':'legacy', 'address':address or 'Existing email reader', 'provider':legacy['source']}
        if scope == 'none' or not self.rows:
            raise core.ConnectorUnavailable('No email account is selected. Choose one in Connections → Email accounts.')

    def _check(self):
        if self.registry.epoch(self.owner) != self.epoch:
            raise core.ConnectorUnavailable('Email account settings changed during this read. Search again.')

    def _reader(self, account):
        self._check()
        row = self.rows.get(account)
        if row is None:
            raise core.ConnectorUnavailable('This message belongs to a different email account selection.')
        if account == 'legacy':
            return self.legacy_store.gmail(self.owner, transport=self.transport, legacy=True)
        if row['provider'] == 'gmail':
            return core.GmailRead(self.registry._google(account, row['credential']), self.owner,
                transport=self.transport, source_check=self._check)
        return self.registry._dedicated(account, row['credential']).reader(self.owner)

    def _selected(self, query):
        query = str(query or '')
        match = re.match(r'^\s*mailbox:([^\s]+)\s*', query, re.I)
        accounts = list(self.rows)
        if match:
            accounts = [key for key, row in self.rows.items() if row['address'].lower() == match[1].lower()]
            if len(accounts) != 1:
                raise core.ConnectorError('Choose a connected mailbox in Email accounts, or search the selected inboxes without mailbox:.')
            query = query[match.end():]
        return accounts, query

    def search(self, *, query=None, max_results=10, page_token=None):
        self._check()
        accounts, native_query = self._selected(query)
        limit = max(1, min(int(max_results or 10), 50))
        fingerprint = hashlib.sha256(str(query or '').encode()).hexdigest()
        if page_token:
            state = self.registry.load_ref(self.owner, page_token, 'page', self.epoch)
            if state.get('query') != fingerprint or state.get('accounts') != accounts:
                raise core.ConnectorError('Continue the same email query and account selection.')
        else:
            state = {'query':fingerprint, 'accounts':accounts, 'cursor':0,
                     'pages':{key:{'started':False,'next':'','buffer':[]} for key in accounts}}
        # Fill one bounded provider page per mailbox. Keep provider tokens and
        # unused IDs sealed so a combined page never loses or mixes results.
        for key in accounts:
            page = state['pages'][key]
            if not page['buffer'] and (not page['started'] or page['next']):
                result = self._reader(key).search(query=native_query or None, max_results=limit, page_token=page['next'] or None)
                self._check()
                page.update(started=True, next=result.get('next_page_token') or '', buffer=[item['id'] for item in result.get('messages', [])][:50])
        hits = []
        while len(hits) < limit:
            progressed = False
            start = state.get('cursor', 0)
            for offset in range(len(accounts)):
                index = (start + offset) % len(accounts)
                key = accounts[index]
                page = state['pages'][key]
                if page['buffer'] and len(hits) < limit:
                    native_id = page['buffer'].pop(0)
                    identity = self.registry.save_ref(self.owner, 'message', self.epoch, {'account':key, 'id':native_id})
                    hits.append({'owner':self.owner, 'id':identity, 'thread_id':'', 'mailbox':self.rows[key]['address']})
                    state['cursor'] = (index + 1) % len(accounts)
                    progressed = True
            if not progressed:
                break
        more = any(page['buffer'] or page['next'] for page in state['pages'].values())
        token = self.registry.save_ref(self.owner, 'page', self.epoch, state, ttl=900) if more else ''
        self._check()
        return {'owner':self.owner, 'messages':hits, 'next_page_token':token,
                'mailboxes':[self.rows[key]['address'] for key in accounts]}

    def latest(self, *, query=None, max_results=10):
        accounts, native_query = self._selected(query)
        if len(accounts) != 1:
            raise core.ConnectorError('Choose one mailbox for a latest-email request; combined searches are not ordered by date.')
        account = accounts[0]
        reader = self._reader(account)
        result = (reader.latest if callable(getattr(reader, 'latest', None)) else reader.search)(query=native_query or None, max_results=max_results)
        self._check()
        hits = []
        for item in result.get('messages', []):
            identity = self.registry.save_ref(self.owner, 'message', self.epoch, {'account':account,'id':item['id']})
            hits.append({'owner':self.owner,'id':identity,'thread_id':'','mailbox':self.rows[account]['address']})
        # Latest only needs the first exact candidate. A new search is required
        # for continuation rather than relaying an unbound provider page token.
        return {'owner':self.owner,'messages':hits,'next_page_token':''}

    def _message(self, identity, full):
        self._check()
        saved = self.registry.load_ref(self.owner, identity, 'message', self.epoch)
        reader = self._reader(saved['account'])
        result = (reader.read_message if full else reader.message)(saved['id'])
        self._check()
        return dict(result, owner=self.owner, id=identity,
                    thread_id=saved['account'] + ':' + str(result.get('thread_id') or ''),
                    mailbox=self.rows[saved['account']]['address'], mailbox_id=saved['account'])

    def message(self, identity):
        return self._message(identity, False)

    def read_message(self, identity):
        return self._message(identity, True)


def default_store():
    return EmailAccounts()


def read_epoch(owner):
    return default_store().epoch(owner)


def automation_reader(owner):
    """Existing automations stay bound to their existing source, not a UI group."""
    store = core.default_store()
    return store.gmail(owner, legacy=True)


def automation_available(owner):
    store = core.default_store()
    return store.gmail_read_available(owner, legacy=True)
