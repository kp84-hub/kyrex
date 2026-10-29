"""Exact-sender Gmail Trash reviews on the existing durable task lifecycle."""
from contextlib import contextmanager
from email.utils import getaddresses
import hashlib
import json
import re
import sqlite3
import time
import uuid
import urllib.request

import connectors
import email_accounts
from paths import data_dir

MODIFY = 'https://www.googleapis.com/auth/gmail.modify'
API = connectors.PROVIDERS['google']['gmail_api'] + '/users/me'
MAX_MESSAGES = 100


class CleanupGrant(connectors.ConnectorStore):
    # An isolated credential file: ordinary Gmail OAuth stays read-only.
    allowed_scopes = frozenset({connectors.GOOGLE_GMAIL_READ_SCOPE,MODIFY})


def grant():
    return CleanupGrant(data_dir() / 'gmail_cleanup_grant.json')


def address(value):
    if not isinstance(value, str) or len(value) > 254 or not email_accounts.ADDRESS.fullmatch(value):
        raise ValueError('Use one exact sender email address, such as offers@example.com.')
    return value.lower()


def transport(method, url, token, params=None, body=None):
    """No redirects, arbitrary endpoints, permanent deletes or automatic retries."""
    from urllib.parse import urlencode
    allowed = (method == 'GET' and (url == API + '/profile' or url == API + '/messages'
               or re.fullmatch(re.escape(API) + r'/messages/[A-Za-z0-9_-]{1,512}', url)))
    allowed = allowed or (method == 'POST' and url == API + '/messages/batchModify')
    if not allowed: raise ValueError('Unsupported Gmail cleanup operation.')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url + ('?' + urlencode(params, doseq=True) if params else ''),
        data=data, method=method, headers={'Authorization':'Bearer ' + token, 'Accept':'application/json',
                                         **({'Content-Type':'application/json'} if data else {})})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576: raise ValueError()
            return json.loads(raw or b'{}')
    except Exception:
        raise ValueError('Gmail cleanup could not verify the provider response. Check the saved review status.') from None


def bot_allowed(owner, bot_id):
    import account_store
    import bots
    import policy
    if account_store.isolated(owner):
        account = account_store.get(owner)
        if not account_store.enabled() or not account or not account.get('active') or account.get('kind') == 'managed':
            raise ValueError('Gmail cleanup requires an active personal account.')
    if not bot_id: return
    bot = bots.load_bots().get(bot_id) or {}
    if bot.get('owner') != owner or not bots.is_running(bot) or not isinstance(bot.get('policy'), dict):
        raise ValueError('Choose a running Bot owned by you for email cleanup.')
    for operation, tier in (('mail:read',0),('gmail:read',0),('mail:trash',1)):
        result = policy.evaluate(bot['policy'],operation,tier)
        if result['matched_rule'] and result['effective_tier'] not in (0,1):
            raise ValueError('This Bot restricts email cleanup. Choose an eligible assistant.')
        if tier == 0 and result['matched_rule'] and result['effective_tier'] != 0:
            raise ValueError('This Bot restricts email reads.')


class CleanupStore:
    def __init__(self, tasks=None):
        from task_store import CloudTaskStore
        self.tasks = tasks or CloudTaskStore()
        self.path = self.tasks.db_path
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS gmail_cleanup_grants(owner TEXT PRIMARY KEY, revision TEXT, sealed TEXT);
                CREATE TABLE IF NOT EXISTS gmail_cleanup_reviews(id TEXT PRIMARY KEY, owner TEXT, revision TEXT,
                    state TEXT, sealed TEXT, task_id TEXT, created REAL, attempted INTEGER DEFAULT 0,
                    request_key TEXT, UNIQUE(owner,request_key));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10); db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def connection(self, owner):
        with self.db() as db:
            row = db.execute('SELECT * FROM gmail_cleanup_grants WHERE owner=?',(owner,)).fetchone()
        if not row or not grant().scope_granted(owner,MODIFY):
            return {'connected':False,'address':'','revision':''}
        return {'connected':True,'address':connectors.unseal_tokens(row['sealed']).get('address',''), 'revision':row['revision']}

    def begin(self, owner, redirect_uri):
        bot_allowed(owner,'')
        started = grant().begin_oauth(owner,scopes=[connectors.GOOGLE_GMAIL_READ_SCOPE,MODIFY],pkce=True,redirect_uri=redirect_uri)
        from urllib.parse import urlsplit,parse_qsl,urlencode,urlunsplit
        parts = urlsplit(started['authorization_url']); query = dict(parse_qsl(parts.query))
        query.update(state='trash_' + started['state'],prompt='consent select_account')
        return {'authorization_url':urlunsplit(parts._replace(query=urlencode(query))), 'expires_at':started['expires_at']}

    def complete(self, state, code, redirect_uri, exchange=None):
        if not re.fullmatch(r'trash_[a-f0-9]{64}',state): raise connectors.OAuthStateError('Invalid cleanup authorization state.')
        status = grant().complete_oauth(None,state[6:],code,redirect_uri=redirect_uri,exchange=exchange)
        owner = status['owner']
        try:
            bot_allowed(owner,'')
            if not grant().scope_granted(owner,MODIFY): raise ValueError('Gmail modify permission was not granted.')
            profile = transport('GET',API+'/profile',grant().access_token(owner))
            mailbox = address(profile.get('emailAddress'))
        except Exception:
            self.disconnect(owner)
            raise ValueError('The cleanup account could not be verified. Connect it again.') from None
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO gmail_cleanup_grants VALUES (?,?,?)',
                       (owner,uuid.uuid4().hex,connectors.seal_tokens({'address':mailbox})))
        return self.connection(owner)

    def disconnect(self, owner):
        with self.db() as db: db.execute('DELETE FROM gmail_cleanup_grants WHERE owner=?',(owner,))
        grant().disconnect(owner)

    def auth(self, owner, expected=None):
        current = self.connection(owner)
        if not current['connected'] or (expected is not None and current != expected):
            raise ValueError('Connect Gmail cleanup in Settings, or prepare a fresh review after reconnecting.')
        token = grant().access_token(owner)
        if not grant().scope_granted(owner,MODIFY):
            raise ValueError('Gmail cleanup permission is no longer available. Reconnect it.')
        profile = transport('GET',API+'/profile',token)
        if address(profile.get('emailAddress')) != current['address']:
            raise ValueError('The Gmail cleanup account changed. Reconnect it and prepare a fresh review.')
        return current, token

    def create(self, owner, conversation, bot_id, sender, request_key=None):
        from task_store import DuplicateTaskId
        sender = address(sender); bot_allowed(owner,bot_id)
        connection,_ = self.auth(owner)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id FROM gmail_cleanup_reviews WHERE owner=? AND request_key=?',(owner,request_key)).fetchone() if request_key else None
            if old: identity = old['id']
            else:
                identity = uuid.uuid4().hex
                payload = {'conversation':conversation,'bot_id':bot_id,'sender':sender,'connection':connection,
                           'messages':[],'more':False,'detail':'Preparing an exact-sender preview. No email has been moved.'}
                db.execute('INSERT INTO gmail_cleanup_reviews VALUES (?,?,?,?,?,?,?,0,?)',
                    (identity,owner,uuid.uuid4().hex,'queued',connectors.seal_tokens(payload),'gmail-cleanup-'+identity,time.time(),request_key))
        row = self.row(owner,identity)
        if row['conversation']!=conversation or row['bot_id']!=bot_id or row['sender']!=sender:
            raise ValueError('This request already belongs to a different cleanup preview.')
        try:
            self.tasks.submit(session_key=bot_id or owner, bot_id=bot_id or None, chat_id=owner,
                executor_prefix='gmail_cleanup',task_text=json.dumps({'review':identity}),
                conversation_id=conversation,task_id=row['task_id'],resolve_bot=bool(bot_id))
        except DuplicateTaskId: pass
        return self.view(owner,identity)

    def row(self, owner, identity):
        if not isinstance(identity,str) or not re.fullmatch('[a-f0-9]{32}',identity): raise ValueError('Email cleanup review not found.')
        with self.db() as db:
            row = db.execute('SELECT * FROM gmail_cleanup_reviews WHERE owner=? AND id=?',(owner,identity)).fetchone()
        if not row: raise ValueError('Email cleanup review not found.')
        return {**dict(row), **connectors.unseal_tokens(row['sealed'])}

    def update(self, owner, identity, state, detail, *, expected=None, attempted=False, messages=None, more=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM gmail_cleanup_reviews WHERE owner=? AND id=?',(owner,identity)).fetchone()
            if not row or (expected and row['state'] != expected): raise ValueError('This cleanup review changed. Refresh its status.')
            payload = connectors.unseal_tokens(row['sealed']); payload['detail'] = detail
            if attempted:
                connection = db.execute('SELECT revision FROM gmail_cleanup_grants WHERE owner=?',(owner,)).fetchone()
                if not connection or connection['revision'] != payload['connection']['revision'] or not grant().scope_granted(owner,MODIFY):
                    raise ValueError('Cleanup authorization changed before submission.')
            if messages is not None: payload.update(messages=messages,more=more)
            db.execute('UPDATE gmail_cleanup_reviews SET state=?,sealed=?,attempted=max(attempted,?) WHERE owner=? AND id=?',
                       (state,connectors.seal_tokens(payload),int(attempted),owner,identity))

    def view(self, owner, identity):
        row = self.row(owner,identity); task = self.tasks.get(row['task_id']) or {}
        approval = self.tasks.get_pending_approval(row['task_id']) if task.get('status') == 'awaiting_approval' else None
        state,detail = row['state'],row['detail']
        if state in {'queued','preparing','ready'} and task.get('status') in {'done','failed','cancelled'}:
            state,detail = 'ended','This review ended without a Trash operation. Prepare a new preview.'
        if state == 'ready' and (time.time()-row['created']>1800 or self.connection(owner) != row['connection']):
            state,detail = 'expired','This review expired or the cleanup account changed. Prepare a fresh preview.'
        return {'id':identity,'revision':row['revision'],'state':state,'detail':detail,
                'mailbox':row['connection']['address'],'sender':row['sender'],'messages':row['messages'],
                'more':row['more'],'count':len(row['messages']),
                'approval_id':approval['approval_id'] if approval else None,
                'task_id':row['task_id'],'task_status':task.get('status'),'attempted':bool(row['attempted'])}

    def decision(self, owner, identity, revision, approval_id, decision):
        row = self.row(owner,identity); view = self.view(owner,identity)
        bot_allowed(owner,row['bot_id'])
        task = self.tasks.get(row['task_id']) or {}
        if (row['revision'] != revision or view['state'] != 'ready' or not approval_id
                or view['approval_id'] != approval_id or task.get('chat_id') != owner
                or task.get('conversation_id') != row['conversation'] or task.get('executor_prefix') != 'gmail_cleanup'
                or row['attempted'] or decision not in {'trash','cancel'}):
            raise ValueError('This exact cleanup approval is no longer pending.')
        if decision == 'trash': self.auth(owner,row['connection'])
        if not self.tasks.record_operator_reply(row['task_id'],'y' if decision=='trash' else 'n'):
            raise ValueError('This approval expired. Refresh the review.')
        return self.view(owner,identity)


def metadata(token, identity, sender):
    if not isinstance(identity,str) or not re.fullmatch('[A-Za-z0-9_-]{1,512}',identity): raise ValueError('Invalid Gmail message identity.')
    result = transport('GET',API+'/messages/'+identity,token,{'format':'metadata','metadataHeaders':['From','Subject','Date']})
    headers = result.get('payload',{}).get('headers',[])
    if not isinstance(headers,list) or result.get('id') != identity: raise ValueError('Gmail returned invalid message metadata.')
    values = {}
    for name in ('From','Subject','Date'):
        matches = [h.get('value') for h in headers if isinstance(h,dict) and str(h.get('name','')).lower()==name.lower()]
        if len(matches)!=1 or not isinstance(matches[0],str): raise ValueError('Gmail message metadata is ambiguous.')
        values[name] = matches[0][:1000]
    actual = getaddresses([values['From']])
    if len(actual)!=1 or actual[0][1].lower()!=sender: return None
    labels = result.get('labelIds')
    if not isinstance(labels,list): raise ValueError('Gmail labels could not be verified.')
    fingerprint = hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()
    return {'id':identity,'subject':values['Subject'],'date':values['Date'],'fingerprint':fingerprint,
            'labels':labels}


def collect(token, sender):
    # Exact From is rechecked in metadata; Gmail search aliases/display names
    # alone are not authority to trash a message. No body fetches.
    page = transport('GET',API+'/messages',token,{'q':'from:'+sender+' -in:trash -in:spam -in:sent -in:drafts','maxResults':MAX_MESSAGES})
    hits = page.get('messages',[])
    if not isinstance(hits,list) or len(hits)>MAX_MESSAGES: raise ValueError('Gmail search was invalid.')
    found = []
    for hit in hits:
        item = metadata(token,hit.get('id'),sender)
        if item and not set(item['labels']) & {'TRASH','SPAM','SENT','DRAFT'}:
            if item['id'] not in {old['id'] for old in found}: found.append(item)
    return found,bool(page.get('nextPageToken'))


def run(ctx, owner, text, task_id, send, *, on_result=None, on_approval=None, on_approval_resolved=None, on_progress=None):
    import serve
    store = CleanupStore(); identity = json.loads(text)['review']; row = store.row(owner,identity)
    task = store.tasks.get(task_id) or {}
    def active():
        current = store.row(owner,identity)
        live = store.tasks.get(task_id) or {}
        if time.time()-current['created']>1800: raise ValueError('Cleanup review expired.')
        if (current['task_id'] != task_id or live.get('chat_id') != owner or live.get('task_text') != text
                or live.get('executor_prefix') != 'gmail_cleanup' or live.get('status') != 'running'
                or live.get('run_id') != task.get('run_id') or live.get('claimed_by') != task.get('claimed_by')
                or task.get('conversation_id') != current['conversation']
                or (current['bot_id'] and (ctx.bot_id != current['bot_id'] or ctx.bot_owner != owner))
                or store.tasks.is_cancel_requested(task_id)):
            raise ValueError('Cleanup task binding is unavailable or the task was cancelled.')
        bot_allowed(owner,current['bot_id'])
        return current
    def finish(state, detail):
        store.update(owner,identity,state,detail)
        if on_result: on_result({'status':'ok' if state=='trashed' else 'no_changes','count':len(store.row(owner,identity)['messages']) if state=='trashed' else 0,'final_response':detail})
        send(owner,detail)
    try:
        active()
        if row['state'] != 'queued':
            finish('unknown' if row['attempted'] else 'blocked','This cleanup was interrupted. Check Gmail and prepare a new preview; no operation will be replayed.'); return
        store.update(owner,identity,'preparing','Checking exact-sender metadata. No emails have been moved.',expected='queued')
        _,token = store.auth(owner,row['connection'])
        messages,more = collect(token,row['sender'])
        active()
        if not messages: finish('empty','No eligible messages from this exact sender were found.'); return
        store.update(owner,identity,'ready','Review the exact messages below before moving them to Trash.',expected='preparing',messages=messages,more=more)
        summary = f"Move these {len(messages)} messages from {row['sender']} to Trash in {row['connection']['address']}?"
        # Subjects live only in the sealed manifest and authenticated card;
        # the shared task approval record binds its exact digest, not bodies.
        detail = json.dumps({'review':identity,'revision':row['revision'],
            'manifest_sha256':hashlib.sha256(json.dumps(messages,sort_keys=True).encode()).hexdigest(),
            'mailbox':row['connection']['address'],'sender':row['sender'],'count':len(messages),'more_matches':more})
        approved = serve._request_in_process_approval(ctx,owner,send,tier=1,summary=summary,detail=detail,
            on_approval=on_approval,on_approval_resolved=on_approval_resolved)
        if not approved: finish('cancelled','Cleanup declined or expired. No email was moved.'); return
        active()
        if time.time()-row['created']>1800: raise ValueError('Cleanup review expired.')
        _,token = store.auth(owner,row['connection'])
        for old in messages:
            fresh = metadata(token,old['id'],row['sender'])
            if not fresh or fresh['fingerprint']!=old['fingerprint'] or set(fresh['labels']) & {'TRASH','SPAM','SENT','DRAFT'}:
                raise ValueError('Reviewed messages changed. Prepare a fresh preview.')
        active(); _,token = store.auth(owner,row['connection'])
        # Claim BEFORE the provider write. A crash/lost response cannot replay.
        store.update(owner,identity,'unknown','Trash operation submitted or its result is unknown. Check Gmail; no automatic retry.',expected='ready',attempted=True)
        transport('POST',API+'/messages/batchModify',token,body={'ids':[m['id'] for m in messages],
                  'addLabelIds':['TRASH'],'removeLabelIds':['INBOX']})
        confirmed = all('TRASH' in (metadata(token,m['id'],row['sender']) or {}).get('labels',[]) for m in messages)
        if not confirmed: raise ValueError('Trash result could not be confirmed.')
        finish('trashed',f'Moved the {len(messages)} reviewed messages to Gmail Trash.' + (' More matches may remain; prepare another preview if needed.' if more else ''))
    except Exception:
        current = store.row(owner,identity)
        finish('unknown' if current['attempted'] else 'blocked',
               'The Trash result is unknown. Check Gmail before preparing another cleanup. This operation will not be retried.' if current['attempted']
               else 'Cleanup could not finish. Check the cleanup connection and prepare a fresh preview. No Trash operation was submitted.')
