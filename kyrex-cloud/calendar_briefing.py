"""Owner-enabled, read-only Google Calendar briefings and change detection."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import re
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo

import connectors

MODE = 'calendar_briefing'
TZ = ZoneInfo('America/New_York')
INTERVAL = 15 * 60
DEFAULTS = {'enabled': False, 'calendars': [], 'briefing_at': '07:00',
            'quiet_from': '21:00', 'quiet_until': '07:00'}
CLOCK = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d')
IMPORTANT = re.compile(r'\b(appointment|school|doctor|dentist|therapy|travel|flight|deadline|interview|performance|conference|exam)\b', re.I)


def coordinator(owner):
    import bots
    import serve
    eligible = [bot['id'] for bot in bots.load_bots().values()
                if bot.get('owner') == owner and bots.is_running(bot)
                and serve.is_coordinator_policy(bot.get('policy'))]
    if len(eligible) != 1:
        raise ValueError('Start one coordinator Bot to receive Calendar briefings.')
    return eligible[0]


def connection_revision(owner):
    conn = connectors.default_store()
    connected = conn.status(owner).get('connected_at')
    if not connected or not conn.scope_granted(owner, connectors.GOOGLE_CALENDAR_READ_SCOPE):
        raise ValueError('Connect Google Calendar with read access first.')
    return connected


def revision_from(text):
    data = json.loads(text)
    if (not isinstance(data, dict) or set(data) != {'revision'}
            or not isinstance(data['revision'], str) or not re.fullmatch(r'[a-f0-9]{32}', data['revision'])):
        raise ValueError('Calendar briefing needs saved settings.')
    return data['revision']


class BriefingStore:
    def __init__(self, tasks=None):
        if tasks is None:
            from task_store import CloudTaskStore
            tasks = CloudTaskStore()
        self.tasks = tasks
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS calendar_briefing_settings (
                    owner TEXT PRIMARY KEY, revision TEXT NOT NULL, enabled INTEGER NOT NULL,
                    config TEXT NOT NULL, next_scan REAL NOT NULL, last_scan REAL,
                    detail TEXT NOT NULL DEFAULT '', state TEXT);
                CREATE TABLE IF NOT EXISTS calendar_briefing_notices (
                    owner TEXT NOT NULL, revision TEXT NOT NULL, identity TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(owner, revision, identity));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.tasks.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self, owner):
        with self.db() as db:
            row = db.execute('SELECT * FROM calendar_briefing_settings WHERE owner=?', (owner,)).fetchone()
        if row is None:
            return dict(DEFAULTS)
        return {**connectors.unseal_tokens(row['config']), 'revision': row['revision'],
                'enabled': bool(row['enabled']), 'last_scan': row['last_scan'], 'detail': row['detail']}

    def configure(self, owner, settings):
        config = {key: settings[key] for key in DEFAULTS if key != 'enabled'}
        if any(not CLOCK.fullmatch(config[key]) for key in ('briefing_at', 'quiet_from', 'quiet_until')):
            raise ValueError('Choose valid times for the briefing and quiet hours.')
        if settings['enabled']:
            config['bot'] = coordinator(owner)
            config['connection'] = connection_revision(owner)
            choices = {row['id'] for row in connectors.default_store().list_calendars(owner)}
            selection = set(config['calendars'])
            if not (1 <= len(selection) <= 5 and len(selection) == len(config['calendars'])
                    and selection <= choices):
                raise ValueError('Choose up to five calendars from your connected Google account.')
            if connection_revision(owner) != config['connection']:
                raise ValueError('Google Calendar changed while saving. Try again.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT enabled,config FROM calendar_briefing_settings WHERE owner=?', (owner,)).fetchone()
            if old and bool(old['enabled']) == settings['enabled'] and connectors.unseal_tokens(old['config']) == config:
                return
            db.execute('''INSERT INTO calendar_briefing_settings(owner,revision,enabled,config,next_scan)
                VALUES(?,?,?,?,?) ON CONFLICT(owner) DO UPDATE SET revision=excluded.revision,enabled=excluded.enabled,
                config=excluded.config,next_scan=excluded.next_scan,last_scan=NULL,detail='',state=NULL''',
                (owner, uuid.uuid4().hex, settings['enabled'], connectors.seal_tokens(config), time.time()))
            db.execute('DELETE FROM calendar_briefing_notices WHERE owner=?', (owner,))

    def current(self, owner, revision):
        cfg = self.settings(owner)
        if not cfg['enabled'] or cfg['revision'] != revision:
            raise ValueError('Calendar briefing settings changed.')
        if coordinator(owner) != cfg['bot']:
            raise ValueError('The coordinator changed. Save Calendar Briefing settings again.')
        if connection_revision(owner) != cfg['connection']:
            raise ValueError('The Google connection changed. Save Calendar Briefing settings again.')
        return cfg

    def failure(self, owner, revision, detail):
        with self.db() as db:
            db.execute('UPDATE calendar_briefing_settings SET detail=? WHERE owner=? AND revision=?',
                       (detail, owner, revision))

    def save_scan(self, owner, cfg, rows, window_end, now, cancelled):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = db.execute('SELECT state,last_scan FROM calendar_briefing_settings WHERE owner=? AND revision=? AND enabled=1',
                               (owner, cfg['revision'])).fetchone()
            if saved is None or (saved['last_scan'] is not None and saved['last_scan'] >= now.timestamp()) or cancelled():
                return
            state = connectors.unseal_tokens(saved['state']) if saved['state'] else {}
            state, notice = reconcile(cfg, state, rows, window_end, now)
            db.execute('UPDATE calendar_briefing_settings SET state=?,last_scan=?,detail=? WHERE owner=? AND revision=?',
                       (connectors.seal_tokens(state), now.timestamp(), 'Calendar checked.', owner, cfg['revision']))
            if notice:
                key, title, body = notice
                db.execute('INSERT OR IGNORE INTO calendar_briefing_notices VALUES (?,?,?,?,?,?)',
                           (owner, cfg['revision'], key, title, connectors.seal_tokens({'body': body}),
                            (now + timedelta(hours=24)).timestamp()))

    def pending(self, now):
        with self.db() as db:
            db.execute('DELETE FROM calendar_briefing_notices WHERE expires<=?', (now,))
            return db.execute('SELECT * FROM calendar_briefing_notices ORDER BY expires LIMIT 100').fetchall()

    def complete(self, row):
        with self.db() as db:
            db.execute('DELETE FROM calendar_briefing_notices WHERE owner=? AND revision=? AND identity=?',
                       (row['owner'], row['revision'], row['identity']))


def submit_due(tasks, now=None):
    """Enqueue scheduled reads on the existing durable Kyrex worker."""
    from task_store import DuplicateTaskId
    now = time.time() if now is None else now
    store = BriefingStore(tasks)
    with store.db() as db:
        due = db.execute('''SELECT owner,revision,config,next_scan FROM calendar_briefing_settings AS settings
            WHERE enabled=1 AND next_scan<=? AND NOT EXISTS (
                SELECT 1 FROM tasks WHERE chat_id=settings.owner AND executor_prefix=?
                AND status IN ('queued','running','awaiting_approval'))''', (now, MODE)).fetchall()
    for row in due:
        cfg = connectors.unseal_tokens(row['config'])
        task_id = 'calendar-briefing-' + hashlib.sha256(
            f"{row['owner']}:{row['revision']}:{row['next_scan']}".encode()).hexdigest()[:32]
        try:
            tasks.submit(session_key=cfg['bot'], bot_id=cfg['bot'], chat_id=row['owner'],
                         executor_prefix=MODE, task_text=json.dumps({'revision': row['revision']}),
                         conversation_id=None, task_id=task_id, resolve_bot=True)
        except DuplicateTaskId:
            pass
        with store.db() as db:
            db.execute('UPDATE calendar_briefing_settings SET next_scan=? WHERE owner=? AND revision=? AND next_scan=?',
                       (now - now % INTERVAL + INTERVAL, row['owner'], row['revision'], row['next_scan']))


def event_time(value):
    if isinstance(value, dict) and value.get('dateTime'):
        stamp = datetime.fromisoformat(value['dateTime'])
        if stamp.tzinfo is not None and stamp.utcoffset() is not None:
            return stamp.astimezone(TZ), False
    elif isinstance(value, dict) and value.get('date'):
        stamp = datetime.fromisoformat(value['date'])
        if stamp.isoformat()[:10] == value['date']:
            return stamp.replace(tzinfo=TZ), True
    raise ValueError('Calendar event time could not be verified.')


def read_events(owner, cfg, now):
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=8)
    conn = connectors.default_store().calendar(owner)
    events = {}
    for calendar_id in cfg['calendars']:
        for row in conn.events(calendar_id=calendar_id, time_min=start.isoformat(),
                               time_max=end.isoformat(), max_results=100, briefing=True):
            if not row.get('id') or row.get('owner') != owner:
                raise ValueError('Calendar event identity could not be verified.')
            key = json.dumps([calendar_id, row['id']], ensure_ascii=True)
            if key in events:
                raise ValueError('Calendar returned a duplicate event identity.')
            if row['status'] == 'cancelled':
                events[key] = None
                continue
            begins, all_day = event_time(row['start'])
            finishes, _ = event_time(row['end'])
            if finishes.timestamp() <= begins.timestamp():
                raise ValueError('Calendar event time range could not be verified.')
            events[key] = {'start': begins.timestamp(), 'end': finishes.timestamp(), 'all_day': all_day,
                           'title': ' '.join(row['summary'].split())[:160] or 'Untitled event',
                           'location': ' '.join(row['location'].split()),
                           'recurring': row['recurring'], 'link': row['link']}
    return events, end.timestamp()


def local(stamp):
    return datetime.fromtimestamp(stamp, TZ)


def text(value):
    # Keep calendar fields as plain text inside the Chat Markdown renderer.
    return re.sub(r'([\\`*_{}\[\]()<>#+!|&])', r'\\\1', value)


def time_label(event):
    stamp = local(event['start'])
    return f"{stamp:%a %b} {stamp.day} · " + ('All day' if event['all_day'] else stamp.strftime('%I:%M %p').lstrip('0'))


def label(event):
    title = text(event['title'])
    link = event['link']
    # Old events (e.g. canceled) may have no usable link.
    return f'[{title}]({link})' if link else title


def overlap(event, other):
    return (not event['all_day'] and not other['all_day']
            and event['start'] < other['end'] and other['start'] < event['end'])


def agenda(events, now):
    day_end = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
    upcoming = sorted((e for e in events.values() if e and e['end'] > now.timestamp()), key=lambda e: e['start'])
    today = [e for e in upcoming if e['start'] < day_end]
    ahead = [e for e in upcoming if e['start'] >= day_end and
             (IMPORTANT.search(e['title']) or not e['recurring'])]
    ahead.sort(key=lambda e: (not bool(IMPORTANT.search(e['title'])), e['start']))
    conflicts = [(e, other) for index, e in enumerate(today) for other in today[index+1:]
                 if other['start'] < e['end'] and overlap(e, other)][:5]
    sections = []
    for title, items, limit in (('Today', today, 12), ('Coming up', ahead, 5)):
        if items:
            lines = [f"- {time_label(e)} · {label(e)}" for e in sorted(items[:limit], key=lambda e: e['start'])]
            if len(items) > limit:
                lines.append(f'- {len(items) - limit} more on your calendar')
            sections.append(f'**{title}**\n' + '\n'.join(lines))
    if conflicts:
        sections.append('**Overlaps today**\n' + '\n'.join(
            f"- {label(e)} and {label(other)} at {time_label(other)}" for e, other in conflicts))
    return sections


def change_line(old, new, status, rows):
    e = new or old
    if status == 'canceled':
        return f"- Canceled: {label(e)} · was {time_label(e)}"
    if new is None:
        return f"- No longer listed at {time_label(e)} · {label(e)}"
    if old is None:
        description = f'Added for {time_label(new)}'
    else:
        parts = []
        if (old['start'], old['end'], old['all_day']) != (new['start'], new['end'], new['all_day']):
            if (old['start'], old['all_day']) == (new['start'], new['all_day']):
                end = local(new['end'])
                end_label = f'{end:%a %b} {end.day}' if new['all_day'] else end.strftime('%I:%M %p').lstrip('0')
                parts.append(f'End date changed to {end_label}' if new['all_day'] else f'End time changed to {end_label}')
            else:
                parts.append(f'Moved from {time_label(old)} to {time_label(new)}')
        if old['location'] != new['location']:
            parts.append(f"Location changed to {text(new['location'])}" if new['location'] else 'Location removed')
        description = ' · '.join(parts)
    if not description:
        return ''
    conflict = next((other for other in rows.values() if other is not None and other is not new and overlap(new, other)), None)
    suffix = f" · overlaps {label(conflict)}" if conflict else ''
    return f'- {description} · {label(new)}{suffix}'


def quiet_at(cfg, now):
    clock = now.strftime('%H:%M')
    low, high = cfg['quiet_from'], cfg['quiet_until']
    return low <= clock < high if low <= high else clock >= low or clock < high


def reconcile(cfg, state, rows, window_end, now):
    before = state.get('rows')
    buffered = state.get('pending', {})
    stamp = now.timestamp()
    upper = stamp + 48 * 3600
    if before is not None:
        for key in before.keys() | rows.keys():
            old, new = before.get(key), rows.get(key)
            if old == new or not any(e and e['end'] > stamp and e['start'] <= upper for e in (old, new)):
                continue
            if old is None and new and new['start'] >= state['window_end']:
                continue
            base = buffered[key][0] if key in buffered else old
            status = 'canceled' if key in rows and rows[key] is None else ''
            if change_line(base, new, status, rows):
                buffered[key] = [base, new, status]
            else:
                buffered.pop(key, None)

    clock, date = now.strftime('%H:%M'), now.date().isoformat()
    digest = state.get('digest')
    notice = None
    if not quiet_at(cfg, now):
        changes = []
        for key, (old, new, status) in sorted(buffered.items(), key=lambda entry: (entry[1][1] or entry[1][0])['start']):
            if (new or old)['end'] > stamp:
                line = change_line(old, rows[key] if key in rows else new, status, rows)
                if line:
                    changes.append(line)
        changes = changes[:12] + ([f'- {len(changes) - 12} more changes on your calendar'] if len(changes) > 12 else [])
        buffered = {}
        daily = clock >= cfg['briefing_at'] and digest != date
        sections = agenda(rows, now) if daily else []
        if daily:
            digest = date
        if changes:
            sections.insert(0, '**Calendar changes**\n' + '\n'.join(changes))
        if sections:
            identity = date if daily else str(int(stamp * 1000))
            title = 'Calendar briefing' if daily else 'Calendar changes'
            notice = (identity, title, f'**{title}**\n\n' + '\n\n'.join(sections))
    return {'rows': {key: event for key, event in rows.items() if event},
            'window_end': window_end, 'digest': digest, 'pending': buffered}, notice


def run_scan(ctx, owner, task_text, task_id):
    store = BriefingStore()
    revision = revision_from(task_text)
    task = store.tasks.get(task_id) or {}
    if (task.get('status') != 'running' or task.get('chat_id') != owner or task.get('task_text') != task_text
            or task.get('executor_prefix') != MODE or not task.get('bot_id')
            or ctx.bot_owner != owner or ctx.bot_id != task.get('bot_id')):
        raise ValueError('Calendar briefing task binding is unavailable.')
    try:
        cfg = store.current(owner, revision)
        if cfg['bot'] != ctx.bot_id:
            raise ValueError('Calendar briefing settings changed.')
        now = datetime.now(TZ)
        cancelled = lambda: store.tasks.is_cancel_requested(task_id)
        if not cancelled():
            rows, end = read_events(owner, cfg, now)
            store.current(owner, revision)
            store.save_scan(owner, cfg, rows, end, now, cancelled)
    except ValueError as exc:
        if str(exc) in {'Calendar briefing settings changed.',
                        'The coordinator changed. Save Calendar Briefing settings again.',
                        'The Google connection changed. Save Calendar Briefing settings again.',
                        'Connect Google Calendar with read access first.',
                        'Start one coordinator Bot to receive Calendar briefings.'}:
            detail = str(exc)
        else:
            detail = 'Calendar could not be verified. The next check will retry.'
        store.failure(owner, revision, detail)
        raise ValueError(detail) from None
    except (connectors.ConnectorError, OSError):
        detail = 'Calendar could not be verified. The next check will retry.'
        store.failure(owner, revision, detail)
        raise ValueError(detail) from None
    return {'mode': MODE, 'status': 'no_changes', 'count': 0, 'final_response': 'Calendar briefing check completed.'}
