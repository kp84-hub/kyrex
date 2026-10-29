"""Exact-sender Gmail Trash reviews on the existing durable task lifecycle."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from email.utils import getaddresses
import hashlib
import json
import re
import sqlite3
import time
import uuid
import urllib.request
import urllib.error

import connectors
import email_accounts
from paths import data_dir

MODIFY = 'https://www.googleapis.com/auth/gmail.modify'
API = connectors.PROVIDERS['google']['gmail_api'] + '/users/me'
SEARCH_PAGE = 500
WRITE_BATCH = 1000
PREVIEW_PAGE = 50
READ_CONCURRENCY = 8


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
    """Fixed endpoints, no redirects/permanent deletes; never retry writes."""
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
    for attempt in range(8):
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
                raw = response.read(1_048_577)
                if len(raw) > 1_048_576: raise ValueError()
                return json.loads(raw or b'{}')
        except urllib.error.HTTPError as exc:
            # Only safe reads can back off. Never retry a submitted write.
            limited = exc.code == 429
            if exc.code == 403:
                limited = any(reason in exc.read(8192).decode('utf-8','replace')
                              for reason in ('rateLimitExceeded','userRateLimitExceeded'))
            if method == 'GET' and limited and attempt < 7:
                time.sleep(min(2 ** attempt,32))
                continue
        except Exception:
            pass
        break
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
                CREATE TABLE IF NOT EXISTS gmail_cleanup_items(review TEXT, position INTEGER, identity TEXT, sealed TEXT,
                    PRIMARY KEY(review,position), UNIQUE(review,identity));
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
                           'manifest_version':2,'count':0,'moved_count':0,'in_flight_count':0,
                           'batches_attempted':0,'verified_batches':0,'scan_complete':False,
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

    def update(self, owner, identity, state, detail, *, expected=None, attempted=False, messages=None, more=False, **progress):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM gmail_cleanup_reviews WHERE owner=? AND id=?',(owner,identity)).fetchone()
            if not row or (expected and row['state'] != expected): raise ValueError('This cleanup review changed. Refresh its status.')
            payload = connectors.unseal_tokens(row['sealed']); payload['detail'] = detail
            if attempted:
                if payload.get('in_flight_count') or payload.get('batches_attempted',0)!=payload.get('verified_batches',0):
                    raise ValueError('An earlier cleanup batch is unresolved.')
                connection = db.execute('SELECT revision FROM gmail_cleanup_grants WHERE owner=?',(owner,)).fetchone()
                if not connection or connection['revision'] != payload['connection']['revision'] or not grant().scope_granted(owner,MODIFY):
                    raise ValueError('Cleanup authorization changed before submission.')
            if messages is not None: payload.update(messages=messages,more=more)
            payload.update(progress)
            db.execute('UPDATE gmail_cleanup_reviews SET state=?,sealed=?,attempted=max(attempted,?) WHERE owner=? AND id=?',
                       (state,connectors.seal_tokens(payload),int(attempted),owner,identity))

    def append_items(self, owner, identity, messages):
        # A separate encrypted row per message keeps large reviews out of one
        # ever-growing JSON blob and makes phone/API pages bounded.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM gmail_cleanup_reviews WHERE owner=? AND id=?',(owner,identity)).fetchone()
            if not row or row['state']!='preparing': raise ValueError('Cleanup manifest is frozen.')
            payload = connectors.unseal_tokens(row['sealed']); count = payload.get('count',0)
            for item in messages:
                added = db.execute('INSERT OR IGNORE INTO gmail_cleanup_items VALUES (?,?,?,?)',
                    (identity,count,hashlib.sha256(item['id'].encode()).hexdigest(),connectors.seal_tokens(item))).rowcount
                count += added
            payload.update(count=count,detail=f'Found {count:,} matching emails. Still scanning; nothing has been moved.')
            db.execute('UPDATE gmail_cleanup_reviews SET sealed=? WHERE owner=? AND id=?',
                       (connectors.seal_tokens(payload),owner,identity))

    def items(self, owner, identity, offset=0, limit=WRITE_BATCH):
        row = self.row(owner,identity)
        if row.get('manifest_version')!=2: return row['messages'][offset:offset+limit]
        with self.db() as db:
            rows = db.execute('SELECT sealed FROM gmail_cleanup_items WHERE review=? AND position>=? ORDER BY position LIMIT ?',
                              (identity,offset,limit)).fetchall()
        return [connectors.unseal_tokens(item['sealed']) for item in rows]

    def batches(self, owner, identity):
        offset = 0
        while True:
            items = self.items(owner,identity,offset)
            if not items: return
            yield items
            offset += len(items)

    def manifest_digest(self, owner, identity):
        digest = hashlib.sha256()
        for batch in self.batches(owner,identity):
            for item in batch: digest.update(json.dumps(item,sort_keys=True).encode()+b'\n')
        return digest.hexdigest()

    def claim_batch(self, owner, identity, size):
        row = self.row(owner,identity)
        if row.get('in_flight_count') or row.get('batches_attempted',0)!=row.get('verified_batches',0):
            raise ValueError('An earlier batch is unresolved.')
        self.update(owner,identity,'executing',f'Moving approved emails: {row.get("moved_count",0):,} of {row["count"]:,} confirmed.',
                    expected=row['state'],attempted=True,in_flight_count=size,
                    batches_attempted=row.get('batches_attempted',0)+1)

    def view(self, owner, identity, offset=0):
        row = self.row(owner,identity); task = self.tasks.get(row['task_id']) or {}
        approval = self.tasks.get_pending_approval(row['task_id']) if task.get('status') == 'awaiting_approval' else None
        state,detail = row['state'],row['detail']
        if state in {'queued','preparing','ready','checking'} and task.get('status') in {'done','failed','cancelled'}:
            state,detail = 'ended','This review ended without a Trash operation. Prepare a new preview.'
        if state == 'executing' and task.get('status') in {'done','failed','cancelled'}:
            state,detail = ('unknown' if row.get('in_flight_count') else 'partial'), 'Cleanup stopped. Check the confirmed and unresolved counts before preparing another review.'
        if state == 'ready' and (time.time()-row.get('ready_at',row['created'])>1800 or self.connection(owner) != row['connection']):
            state,detail = 'expired','This review expired or the cleanup account changed. Prepare a fresh preview.'
        count = row.get('count',len(row['messages']))
        page = self.items(owner,identity,offset,PREVIEW_PAGE)
        return {'id':identity,'revision':row['revision'],'state':state,'detail':detail,
                'mailbox':row['connection']['address'],'sender':row['sender'],
                'messages':[{k:m[k] for k in ('id','subject','date')} for m in page],
                'more':row['more'],'count':count,'offset':offset,'page_size':PREVIEW_PAGE,
                'has_next':offset+len(page)<count,'scan_complete':row.get('scan_complete',True),
                'moved_count':row.get('moved_count',count if state=='trashed' else 0),
                'in_flight_count':row.get('in_flight_count',0),
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
        if not matches and name!='From': matches = ['']
        if len(matches)!=1 or not isinstance(matches[0],str): raise ValueError('Gmail message metadata is ambiguous.')
        values[name] = matches[0][:1000]
    actual = getaddresses([values['From']])
    if len(actual)!=1 or actual[0][1].lower()!=sender: return None
    labels = result.get('labelIds')
    if not isinstance(labels,list): raise ValueError('Gmail labels could not be verified.')
    fingerprint = hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()
    return {'id':identity,'subject':values['Subject'],'date':values['Date'],'fingerprint':fingerprint,
            'labels':labels}


def read_metadata(token, identities, sender, check):
    # Bound concurrency and check cancellation/worker ownership between small
    # read groups, including during a large initial scan or preflight.
    with ThreadPoolExecutor(max_workers=READ_CONCURRENCY) as pool:
        for start in range(0,len(identities),PREVIEW_PAGE):
            check()
            values = list(pool.map(lambda identity: metadata(token,identity,sender),
                                   identities[start:start+PREVIEW_PAGE]))
            check()
            yield values


def collect(token_for_page, sender, check):
    # Page through ALL results, deduplicate and validate actual From headers.
    # Never approve an estimate or a partially completed provider scan.
    cursor = None; cursors = set(); seen = set()
    while True:
        check(); token = token_for_page()
        params = {'q':'from:'+sender+' -in:trash -in:spam -in:sent -in:drafts','maxResults':SEARCH_PAGE}
        if cursor: params['pageToken'] = cursor
        page = transport('GET',API+'/messages',token,params)
        hits = page.get('messages',[])
        if not isinstance(hits,list) or len(hits)>SEARCH_PAGE: raise ValueError('Gmail search was invalid.')
        identities = []
        for hit in hits:
            identity = hit.get('id') if isinstance(hit,dict) else None
            if not isinstance(identity,str) or not re.fullmatch('[A-Za-z0-9_-]{1,512}',identity):
                raise ValueError('Invalid Gmail message identity.')
            if identity not in seen: seen.add(identity); identities.append(identity)
        for values in read_metadata(token,identities,sender,check):
            yield [item for item in values if item and not set(item['labels']) & {'TRASH','SPAM','SENT','DRAFT'}]
        cursor = page.get('nextPageToken')
        if not cursor: return
        if not isinstance(cursor,str) or len(cursor)>4096 or cursor in cursors:
            raise ValueError('Gmail pagination did not finish. No review can be approved.')
        cursors.add(cursor)


def validate_batch(token, messages, sender, check):
    checked = 0
    for fresh in read_metadata(token,[m['id'] for m in messages],sender,check):
        for current in fresh:
            old = messages[checked]; checked += 1
            if (not current or current['fingerprint']!=old['fingerprint']
                    or set(current['labels']) & {'TRASH','SPAM','SENT','DRAFT'}):
                raise ValueError('Reviewed messages changed. Prepare a fresh preview.')


def run(ctx, owner, text, task_id, send, *, on_result=None, on_approval=None, on_approval_resolved=None, on_progress=None):
    import serve
    store = CleanupStore(); identity = json.loads(text)['review']; row = store.row(owner,identity)
    task = store.tasks.get(task_id) or {}
    def active():
        current = store.row(owner,identity)
        live = store.tasks.get(task_id) or {}
        if current['state']=='ready' and time.time()-current.get('ready_at',current['created'])>1800:
            raise ValueError('Cleanup review expired.')
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
        current = store.row(owner,identity)
        # A presentation/transport failure cannot change a verified receipt.
        try:
            if on_result: on_result({'status':'ok' if state=='trashed' else state,
                'count':current.get('moved_count',0),'total':current.get('count',0),
                'unresolved_count':current.get('in_flight_count',0),'final_response':detail})
        except Exception: pass
        try: send(owner,detail)
        except Exception: pass
    try:
        active()
        if row['state'] != 'queued':
            # Preserve verified batch receipts on recovery. No batch is replayed.
            state = 'unknown' if row.get('in_flight_count') else 'partial' if row.get('moved_count') else 'blocked'
            if row['state'] not in {'trashed','cancelled','empty','blocked','partial','unknown'}:
                finish(state,f'Cleanup was interrupted: {row.get("moved_count",0):,} confirmed moved, {row.get("in_flight_count",0):,} unresolved. Check Gmail; no batch will be replayed.')
            return
        store.update(owner,identity,'preparing','Scanning all exact-sender matches. No emails have been moved.',
                     expected='queued',manifest_version=2,count=0,moved_count=0,in_flight_count=0,
                     batches_attempted=0,verified_batches=0,scan_complete=False)
        token_for_page = lambda: store.auth(owner,row['connection'])[1]
        for found in collect(token_for_page,row['sender'],active):
            store.append_items(owner,identity,found)
        active()
        count = store.row(owner,identity)['count']
        if not count: finish('empty','No eligible messages from this exact sender were found.'); return
        digest = store.manifest_digest(owner,identity)
        store.update(owner,identity,'ready',f'Review all {count:,} matching emails. One approval covers this entire frozen set.',
                     expected='preparing',scan_complete=True,ready_at=time.time(),manifest_sha256=digest)
        summary = f"Move all {count:,} reviewed messages from {row['sender']} to Trash in {row['connection']['address']}?"
        # Subjects live only in the sealed manifest and authenticated card;
        # the shared task approval record binds its exact digest, not bodies.
        detail = json.dumps({'review':identity,'revision':row['revision'],
            'manifest_sha256':digest,'mailbox':row['connection']['address'],'sender':row['sender'],'count':count})
        approved = serve._request_in_process_approval(ctx,owner,send,tier=1,summary=summary,detail=detail,
            on_approval=on_approval,on_approval_resolved=on_approval_resolved)
        if not approved: finish('cancelled','Cleanup declined or expired. No email was moved.'); return
        active()
        store.update(owner,identity,'checking','Approval received. Rechecking the complete frozen set before moving any emails.',expected='ready')
        if store.manifest_digest(owner,identity)!=digest: raise ValueError('Cleanup manifest changed.')
        checked = 0
        for messages in store.batches(owner,identity):
            validate_batch(token_for_page(),messages,row['sender'],active)
            checked += len(messages)
            store.update(owner,identity,'checking',f'Rechecked {checked:,} of {count:,} approved emails. Nothing has been moved yet.',expected='checking')
        for messages in store.batches(owner,identity):
            active(); token = token_for_page()
            validate_batch(token,messages,row['sender'],active)
            active(); token = token_for_page()
            # Durable claim BEFORE each write. Unknown batch stops the entire
            # cleanup; neither it nor earlier successful batches are replayed.
            store.claim_batch(owner,identity,len(messages))
            transport('POST',API+'/messages/batchModify',token,body={'ids':[m['id'] for m in messages],
                      'addLabelIds':['TRASH'],'removeLabelIds':['INBOX']})
            for fresh in read_metadata(token,[m['id'] for m in messages],row['sender'],lambda:None):
                if any(not m or 'TRASH' not in m['labels'] for m in fresh):
                    raise ValueError('Trash result could not be confirmed.')
            current = store.row(owner,identity)
            moved = current['moved_count']+len(messages)
            store.update(owner,identity,'executing',f'Moved {moved:,} of {count:,} approved emails to Trash.',
                         expected='executing',moved_count=moved,in_flight_count=0,
                         verified_batches=current['verified_batches']+1)
            if on_progress:
                try: on_progress({'message':f'Moved {moved:,} of {count:,} approved emails to Trash.'})
                except Exception: pass
        finish('trashed',f'Moved all {count:,} reviewed messages to Gmail Trash.')
    except Exception:
        current = store.row(owner,identity)
        if current['state'] in {'trashed','cancelled','empty','blocked','partial','unknown'}: return
        moved,unresolved = current.get('moved_count',0),current.get('in_flight_count',0)
        finish('unknown' if unresolved else 'partial' if moved else 'blocked',
               f'Cleanup stopped: {moved:,} confirmed moved to Trash; {unresolved:,} messages in an unresolved batch. Remaining batches were not submitted. Check Gmail before another cleanup; no batch will be replayed.'
               if current['attempted'] else 'Cleanup could not finish. Check the cleanup connection and prepare a fresh preview. No Trash operation was submitted.')
