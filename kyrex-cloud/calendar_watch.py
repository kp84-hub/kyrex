"""Calendar suggestions on the existing durable task lifecycle.

Tables below hold preferences, observations and drafts, never an execution queue.
CloudTaskStore/TaskWorker own every scan and every reviewed calendar write.
"""
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import cal_writer
import connectors
import calendar_watch_events as events
import calendar_watch_sources as sources

ACTIVE = {'queued', 'running', 'awaiting_approval'}


def conversation_id(owner):
    return hashlib.sha256(('calendar-suggestions:' + owner).encode()).hexdigest()[:32]


def context_bot(owner, bot_id, cfg=None, write=False):
    import account_store
    if account_store.isolated(owner):
        account = account_store.get(owner)
        if not account_store.enabled() or not account or not account.get('active') or account.get('kind') == 'managed':
            raise ValueError('Event review requires an active account with personal connections.')
    import bots
    import policy
    bot = bots.load_bots().get(bot_id) or {}
    if bot.get('owner') != owner or not bots.is_running(bot):
        raise ValueError('Choose a running Bot owned by this account.')
    ops = ['cal:list'] + (['cal:create'] if write else [])
    if cfg:
        ops += (['gmail:read', 'mail:read'] if cfg['emails'] else [])
        ops += ['messages:read'] if cfg['messages'] else []
    rules = bot.get('policy')
    if not isinstance(rules, dict): raise ValueError('The selected Bot has an invalid policy.')
    for op in ops:
        verdict = policy.evaluate(rules, op, 0)
        # Connected owner grants remain the shared-tool authority. Preserve
        # explicit Bot restrictions, including read rules requiring review.
        if verdict['matched_rule'] and verdict['effective_tier'] != 0:
            raise ValueError('The selected Bot restricts ' + op + '. Choose an eligible Bot.')
    return bot


def destination(owner):
    conn = connectors.default_store()
    if not conn.calendar_read_available(owner) or not conn.calendar_write_available(owner):
        raise ValueError('Connect Google Calendar read and write access before enabling event review.')
    status = conn.status(owner)
    if not status.get('connected_at'):
        raise ValueError('Reconnect Google Calendar to verify its account.')
    return {'connected_at': status['connected_at'], 'calendar': conn.preferred_calendar(owner),
            'email': conn.account_view(owner).get('email') or 'Your connected Google account'}


class WatchStore:
    def __init__(self, tasks=None):
        from task_store import CloudTaskStore
        self.tasks = tasks or CloudTaskStore()
        self.path = self.tasks.db_path
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS calendar_watch_settings (
                    owner TEXT PRIMARY KEY, revision TEXT NOT NULL, enabled INTEGER NOT NULL,
                    data TEXT NOT NULL, next_scan REAL NOT NULL, last_scan REAL, detail TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS calendar_watch_seen (
                    owner TEXT NOT NULL, source TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(owner,source));
                CREATE TABLE IF NOT EXISTS calendar_suggestions (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    revision TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL,
                    task_id TEXT, attempted INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                    UNIQUE(owner,fingerprint));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def settings(self, owner):
        with self.db() as db:
            row = db.execute('SELECT * FROM calendar_watch_settings WHERE owner=?', (owner,)).fetchone()
        if not row:
            return {'owner': owner, 'enabled': False, 'revision': '', 'emails': True,
                    'messages': False, 'bot_id': '', 'interval_seconds': 3600,
                    'next_scan': None, 'last_scan': None, 'detail': ''}
        # SQLite persists booleans as integers. Keep the same typed settings
        # contract as an unconfigured owner, including after save/restart.
        return {**dict(row), **connectors.unseal_tokens(row['data']), 'enabled': bool(row['enabled'])}

    def configure(self, owner, *, enabled, emails, messages, bot_id, interval_seconds):
        if interval_seconds not in (3600, 7200, 21600, 86400):
            raise ValueError('Choose hourly, every 2 hours, every 6 hours, or daily.')
        if enabled and not (emails or messages): raise ValueError('Choose email, messages, or both.')
        old = self.settings(owner)
        cfg = {'emails': emails, 'messages': messages, 'bot_id': bot_id,
               'interval_seconds': interval_seconds, 'since': old.get('since', time.time() - 86400)}
        if enabled:
            context_bot(owner, bot_id, cfg, write=True)
            import bot_provider
            bot_provider.resolve_bot_provider(owner, context_bot(owner, bot_id, cfg))
            cfg['binding'] = sources.binding(owner, emails, messages)
            cfg['destination'] = destination(owner)
        else:
            cfg['binding'] = old.get('binding', {})
            cfg['destination'] = old.get('destination', {})
        with self.db() as db:
            db.execute('INSERT INTO calendar_watch_settings(owner,revision,enabled,data,next_scan) VALUES (?,?,?,?,?) '
                'ON CONFLICT(owner) DO UPDATE SET revision=excluded.revision,enabled=excluded.enabled, '
                'data=excluded.data,next_scan=excluded.next_scan,detail=\'\'',
                (owner, uuid.uuid4().hex, int(enabled), connectors.seal_tokens(cfg), time.time()))
        return self.settings(owner)

    def valid(self, owner, revision):
        cfg = self.settings(owner)
        if not cfg['enabled'] or cfg['revision'] != revision:
            raise ValueError('Event review was paused or its settings changed. Open a fresh review.')
        context_bot(owner, cfg['bot_id'], cfg)
        sources.check(owner, cfg)
        if destination(owner) != cfg['destination']:
            raise ValueError('Calendar account or destination changed. Save event review settings again.')
        return cfg

    def seen(self, owner, key):
        with self.db() as db:
            return db.execute('SELECT 1 FROM calendar_watch_seen WHERE owner=? AND source=?', (owner, key)).fetchone() is not None

    def observe(self, owner, revision, scanned, drafts, now):
        cfg = self.valid(owner, revision)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT revision FROM calendar_watch_settings WHERE owner=? AND enabled=1', (owner,)).fetchone()
            if not current or current[0] != revision:
                raise ValueError('Event review settings changed.')
            for source, draft in drafts:
                # Suppress old events, but keep incomplete candidates for details.
                day = draft['fields']['date']
                if day and day < datetime.fromtimestamp(now, ZoneInfo(cal_writer.TIMEZONE)).date().isoformat(): continue
                signature = {'fields': {**draft['fields'], 'title': ' '.join(draft['fields']['title'].casefold().split())}, 'kind': draft['kind']}
                if draft['needs']: signature['source'] = source['key']
                fingerprint = sources.digest(signature)
                data = {**draft, 'source': source, 'settings_revision': revision,
                        'destination': cfg['destination'], 'detail': ''}
                db.execute('INSERT OR IGNORE INTO calendar_suggestions VALUES (?,?,?,?,?,?,NULL,0,?)',
                    (uuid.uuid4().hex, owner, fingerprint, uuid.uuid4().hex, 'pending', connectors.seal_tokens(data), now))
                if draft['kind'] in {'change', 'cancel'}:
                    # A later correction must not leave an earlier same-title
                    # create ready to approve. Blocking is conservative.
                    for row in db.execute("SELECT id,data FROM calendar_suggestions WHERE owner=? AND state IN ('pending','reviewing') AND attempted=0", (owner,)).fetchall():
                        old = connectors.unseal_tokens(row['data'])
                        if old.get('kind') == 'create' and old['fields']['title'].casefold() == draft['fields']['title'].casefold():
                            old['detail'] = 'A newer change or cancellation mentions this event. Check the source before adding it.'
                            db.execute("UPDATE calendar_suggestions SET state='blocked',data=? WHERE owner=? AND id=?", (connectors.seal_tokens(old), owner, row['id']))
            for source in scanned:
                db.execute('INSERT OR IGNORE INTO calendar_watch_seen VALUES (?,?,?)', (owner, source['key'], now))
            db.execute('DELETE FROM calendar_watch_seen WHERE created<?', (now - 30 * 86400,))

    def rows(self, owner):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM calendar_suggestions WHERE owner=? ORDER BY created DESC,id DESC LIMIT 100', (owner,))]

    def get(self, owner, identity):
        with self.db() as db:
            row = db.execute('SELECT * FROM calendar_suggestions WHERE owner=? AND id=?', (owner, identity)).fetchone()
        if not row: raise LookupError('Calendar suggestion not found.')
        return {**dict(row), **connectors.unseal_tokens(row['data'])}

    def view(self, owner, identity):
        row = self.get(owner, identity)
        task = self.tasks.get(row['task_id']) if row['task_id'] else None
        approval = self.tasks.get_pending_approval(row['task_id']) if task and task['status'] == 'awaiting_approval' else None
        state = row['state']
        detail = row.get('detail') or ''
        if task and state == 'reviewing' and task['status'] not in ACTIVE:
            state = 'ended'
            detail = 'This review ended without a confirmed calendar addition. Edit the suggestion to prepare a fresh review.'
        return {'id': row['id'], 'revision': row['revision'], 'state': state,
                'kind': row['kind'], 'fields': row['fields'], 'needs': row['needs'],
                'source': {k: row['source'].get(k) for k in ('label','received','coverage','synced_at')},
                'evidence': row['evidence'], 'destination': row['destination'],
                'timezone': cal_writer.TIMEZONE, 'detail': detail,
                'task': {'task_id': task['task_id'], 'status': task['status']} if task else None,
                'approval': {k: approval[k] for k in ('approval_id','summary','detail')} if approval else None,
                'can_edit': not row['attempted'] and state in {'pending','ended'}}

    def edit(self, owner, identity, revision, fields):
        intent = events.intent_from_fields(fields)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.get(owner, identity)
            if row['revision'] != revision or row['attempted'] or row['kind'] != 'create':
                raise ValueError('This suggestion changed or cannot be edited. Reload its status.')
            task = self.tasks.get(row['task_id']) if row['task_id'] else None
            if row['state'] not in {'pending','reviewing'} or task and task['status'] in ACTIVE:
                raise ValueError('Finish the current review before editing.')
            cfg = self.valid(owner, self.settings(owner)['revision'])
            data = connectors.unseal_tokens(row['data'])
            data.update(fields=fields, intent=intent, needs=[], settings_revision=cfg['revision'],
                        destination=cfg['destination'], detail='Details edited by you; review the saved event before approving.')
            db.execute("UPDATE calendar_suggestions SET revision=?,data=?,state='pending',task_id=NULL WHERE owner=? AND id=?",
                (uuid.uuid4().hex, connectors.seal_tokens(data), owner, identity))
        return self.view(owner, identity)

    def dismiss(self, owner, identity, revision):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.get(owner, identity)
            if row['revision'] != revision or row['attempted'] or row['state'] not in {'pending','blocked'}:
                raise ValueError('Finish the current review before dismissing this suggestion.')
            db.execute("UPDATE calendar_suggestions SET state='dismissed' WHERE owner=? AND id=?", (owner, identity))
        return self.view(owner, identity)

    def review(self, owner, identity, revision):
        from task_store import DuplicateTaskId
        row = self.get(owner, identity)
        if row['revision'] != revision: raise ValueError('The suggestion changed; reload it before reviewing.')
        if row['task_id'] and self.tasks.get(row['task_id']): return self.view(owner, identity)
        cfg = self.valid(owner, row['settings_revision'])
        if row['state'] not in {'pending','reviewing'} or row['kind'] != 'create' or row['needs'] or not row['intent']:
            raise ValueError('Complete the missing event details before opening a calendar review.')
        sources.reread(owner, cfg, row['source'])
        text = json.dumps({'suggestion': identity, 'revision': revision}, sort_keys=True)
        task_id = 'event-calendar-' + sources.digest([owner, identity, revision])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE calendar_suggestions SET task_id=?,state='reviewing' WHERE owner=? AND id=? AND revision=? AND state='pending' AND task_id IS NULL",
                (task_id, owner, identity, revision)).rowcount
            # A concurrent click may have linked the task after our initial
            # read but not yet submitted it. Repair only that exact active
            # link; CloudTaskStore deduplicates its deterministic task ID.
            linked = db.execute('SELECT task_id,state,attempted FROM calendar_suggestions WHERE owner=? AND id=? AND revision=?',
                (owner, identity, revision)).fetchone()
        if changed or (linked and linked['task_id'] == task_id and linked['state'] == 'reviewing' and not linked['attempted']):
            try:
                self.tasks.submit(session_key=cfg['bot_id'], bot_id=cfg['bot_id'], chat_id=owner,
                    conversation_id=conversation_id(owner), executor_prefix='event_calendar', task_text=text,
                    task_id=task_id, resolve_bot=True)
            except DuplicateTaskId: pass
            except Exception:
                # A later click can repair the durable link if submit failed
                # before acknowledgement; it never substitutes a new identity.
                raise ValueError('The review could not be queued. Reload its status.') from None
        return self.view(owner, identity)

    def outcome(self, owner, identity, revision, state, detail, *, attempt=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.get(owner, identity)
            if row['revision'] != revision: raise ValueError('Event review changed.')
            if attempt and (row['attempted'] or row['state'] != 'reviewing'):
                raise ValueError('This calendar attempt was already claimed.')
            data = connectors.unseal_tokens(row['data']); data['detail'] = detail
            changed = db.execute('UPDATE calendar_suggestions SET state=?,data=?,attempted=max(attempted,?) WHERE owner=? AND id=? AND revision=?',
                (state, connectors.seal_tokens(data), int(attempt), owner, identity, revision)).rowcount
            if not changed: raise ValueError('Event review changed.')


def submit_due(tasks, now=None):
    """Called by the existing worker poller. No reads, inference or writes here."""
    from task_store import DuplicateTaskId
    now = time.time() if now is None else now
    store = WatchStore(tasks)
    with store.db() as db:
        rows = db.execute('SELECT owner FROM calendar_watch_settings WHERE enabled=1 AND next_scan<=?', (now,)).fetchall()
    for row in rows:
        cfg = store.settings(row['owner'])
        # Do not fill the task queue while a previous scan is waiting/running.
        task_id = 'event-scan-' + sources.digest([cfg['owner'], cfg['revision'], cfg['next_scan']])
        with store.db() as db:
            active = db.execute("SELECT 1 FROM tasks WHERE chat_id=? AND executor_prefix='calendar_watch' AND status IN ('queued','running','awaiting_approval')", (cfg['owner'],)).fetchone()
        if active: continue
        try:
            tasks.submit(session_key=cfg['bot_id'], bot_id=cfg['bot_id'], chat_id=cfg['owner'],
                executor_prefix='calendar_watch', task_text=json.dumps({'revision':cfg['revision']}),
                conversation_id=None, task_id=task_id, resolve_bot=True)
        except DuplicateTaskId: pass
        with store.db() as db:
            db.execute('UPDATE calendar_watch_settings SET next_scan=? WHERE owner=? AND revision=? AND next_scan=?',
                (now + cfg['interval_seconds'], cfg['owner'], cfg['revision'], cfg['next_scan']))


def pending_event(owner, row):
    """Complete lookup in the exact selected calendar; errors block creation."""
    intent = cal_writer.load_intent(row['intent'])
    zone = ZoneInfo(cal_writer.TIMEZONE)
    start = datetime.fromisoformat(intent['start']).replace(tzinfo=zone)
    end = datetime.fromisoformat(intent['end']).replace(tzinfo=zone)
    low = start.replace(hour=0, minute=0, second=0)
    high = max(end, low + timedelta(days=1))
    existing = connectors.default_store().calendar(owner).events(time_min=low.isoformat(),
        time_max=high.isoformat(), max_results=100, calendar_id=row['destination']['calendar'], require_complete=True)
    event = cal_writer.to_google_event(intent)
    event['id'] = sources.digest([owner, row['destination'], row['fingerprint']])
    def when(value):
        if not isinstance(value, dict): return None
        if value.get('date'): return ('date', value['date'])
        try:
            parsed = datetime.fromisoformat(value['dateTime'].replace('Z', '+00:00'))
            if parsed.tzinfo is None: parsed = parsed.replace(tzinfo=ZoneInfo(value.get('timeZone', cal_writer.TIMEZONE)))
            return ('time', parsed.timestamp())
        except (ValueError, TypeError, KeyError): return None
    matches = [old for old in existing if old.get('status') != 'cancelled' and
        (old.get('id') == event['id'] or
         (' '.join(str(old.get('summary') or '').casefold().split()) == ' '.join(intent['title'].casefold().split())
          and when(old.get('start')) == when(event['start']) and when(old.get('end')) == when(event['end'])))]
    return event, matches


def run_scan(ctx, owner, text, task_id, send, *, on_result=None, on_progress=None):
    import asyncio
    import sys
    from pathlib import Path
    backend = str(Path(__file__).parent / 'web' / 'backend')
    if backend not in sys.path: sys.path.insert(0, backend)
    store = WatchStore()
    revision = json.loads(text)['revision']
    task = store.tasks.get(task_id) or {}
    def active():
        if store.tasks.is_cancel_requested(task_id): raise ValueError('Event scan was cancelled.')
        return store.valid(owner, revision)
    now = time.time()
    try:
        cfg = store.valid(owner, revision)
        if (task.get('status') != 'running' or task.get('chat_id') != owner
                or task.get('bot_id') != cfg['bot_id'] or task.get('task_text') != text
                or task.get('executor_prefix') != 'calendar_watch'
                or ctx.bot_owner != owner or ctx.bot_id != cfg['bot_id']):
            raise ValueError('Event scan task binding is unavailable.')
        if on_progress: on_progress({'message':'Checking selected inboxes and synced phone messages for events.'})
        found, notes = sources.collect(owner, cfg, lambda key: store.seen(owner, key), now)
        batch, scanned = [], []
        for source in found:
            active()
            if events.eventlike(source['text']):
                if len(batch) >= 20:
                    notes.append('More possible event messages remain; the next scan will continue checking them.')
                    break
                # Cap the one extraction call at 60k characters. Full original
                # source/digest remains sealed for revalidation, not inference.
                if sum(len(item['text']) for item in batch) + len(source['text']) > 60000:
                    notes.append('More possible event messages remain for the next scan.')
                    break
                batch.append(source)
            scanned.append(source)
        drafts = asyncio.run(events.extract(owner, context_bot(owner, cfg['bot_id'], cfg), batch)) if batch else []
        active()
        # An exact calendar match is already satisfied. Dismissed/event drafts
        # are deduped separately by durable observation and event identities.
        filtered = []
        for source, draft in drafts:
            active()
            if draft['intent']:
                _, matches = pending_event(owner, {**draft, 'destination':cfg['destination'], 'fingerprint':''})
                if matches: continue
            filtered.append((source, draft))
        store.observe(owner, revision, scanned, filtered, now)
        # Replayable delivery to the existing notification outbox. An empty
        # hourly scan produces no alert. Message excerpts never enter pushes.
        from chat_notifications import NotificationStore
        for row in store.rows(owner):
            if row['state'] == 'pending':
                NotificationStore().record(owner, 'calendar-suggestion:' + row['id'],
                    'automation', 'Calendar suggestion ready', conversation_id(owner))
        detail = ' '.join(dict.fromkeys(notes)) or 'Selected sources checked.'
        with store.db() as db:
            db.execute('UPDATE calendar_watch_settings SET last_scan=?,detail=? WHERE owner=? AND revision=?', (now, detail, owner, revision))
        result = {'mode':'calendar_watch', 'status':'no_changes', 'count':len(filtered),
                  'final_response': f'Event scan completed. {len(filtered)} candidate(s) checked. ' + detail}
    except Exception:
        with store.db() as db:
            db.execute('UPDATE calendar_watch_settings SET detail=? WHERE owner=? AND revision=?',
                ('Event scan could not finish. Check source connections, Calendar access and the selected Bot model. Unprocessed messages will be checked again.', owner, revision))
        raise ValueError('Event scan could not finish. Check Event review settings for source coverage.') from None
    if on_result: on_result(result)
    send(owner, result['final_response'])


def run_review(ctx, owner, text, task_id, cid, send, *, on_result=None,
               on_approval=None, on_approval_resolved=None, on_progress=None):
    import serve
    store = WatchStore()
    value = json.loads(text)
    identity, revision = value['suggestion'], value['revision']
    def active():
        row = store.get(owner, identity)
        cfg = store.valid(owner, row['settings_revision'])
        task = store.tasks.get(task_id) or {}
        if (ctx.bot_owner != owner or ctx.bot_id != cfg['bot_id'] or row['revision'] != revision
                or row['task_id'] != task_id or row['state'] != 'reviewing' or row['attempted']
                or task.get('status') != 'running' or task.get('cancel_requested')
                or task.get('chat_id') != owner or task.get('bot_id') != cfg['bot_id']
                or task.get('conversation_id') != conversation_id(owner) or cid != conversation_id(owner)
                or task.get('executor_prefix') != 'event_calendar' or task.get('task_text') != text):
            raise ValueError('Calendar suggestion or its task changed. Open a fresh review.')
        context_bot(owner, cfg['bot_id'], cfg, write=True)
        sources.reread(owner, cfg, row['source'])
        if destination(owner) != row['destination']:
            raise ValueError('The calendar account or destination changed. Save event review settings again.')
        return row
    def reply(message):
        if on_result: on_result({'status':'no_changes','count':0,'final_response':message})
        send(owner, message)
    try:
        row = active()
        event, matches = pending_event(owner, row)
        if matches:
            store.outcome(owner, identity, revision, 'duplicate', 'This event is already on your calendar.')
            reply('This event is already on your calendar. No duplicate was added.'); return
        detail = json.dumps({'account':row['destination']['email'], 'calendar':row['destination']['calendar'],
            'timezone':cal_writer.TIMEZONE, 'event':{k:v for k,v in event.items() if k != 'id'},
            'source':row['source']['label'], 'evidence':row['evidence']}, ensure_ascii=False, indent=2)
        approved = serve._request_in_process_approval(ctx, owner, send, tier=1,
            summary=cal_writer.summary_line(row['intent']), detail=detail,
            on_approval=on_approval, on_approval_resolved=on_approval_resolved)
        if not approved:
            reply('Calendar review declined or expired. No event was added.'); return
        row = active()
        fresh, matches = pending_event(owner, row)
        if matches or fresh != event:
            raise ValueError('The calendar changed during review. Check it before opening a new review.')
        active()
        # Persist uncertain BEFORE the provider request. A lost response or a
        # process restart can never re-enable this card's write attempt.
        store.outcome(owner, identity, revision, 'unknown',
            'Calendar creation is in progress or its result is unknown. Check Calendar before trying another event.', attempt=True)
        receipt = connectors.default_store().calendar_writer(owner).create_event(event, expected_binding=row['destination'])
        if receipt.get('id') != event['id']: raise ValueError('Calendar returned an unexpected event reference.')
        store.outcome(owner, identity, revision, 'added', 'Added to your calendar after your approval.')
        reply(cal_writer.format_receipt(row['intent'], receipt))
    except Exception as exc:
        # Never disclose provider response text, source bodies or credentials.
        row = store.get(owner, identity)
        if row['attempted']:
            reply('The calendar result is unknown. Check Calendar before trying again. This event will not be automatically retried.')
        else:
            detail = str(exc) if isinstance(exc, ValueError) else 'The connected calendar or source could not be verified.'
            store.outcome(owner, identity, revision, 'blocked', detail + ' No calendar write was attempted.')
            reply(detail + ' No calendar write was attempted.')
