"""Owner-scoped trainer baselines, confirmation candidates and delivery ledger."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo('America/New_York')
CONFIRM_SECONDS = 90


def starts(day: str) -> float:
    return datetime.combine(date.fromisoformat(day), day_time(8, 30), EASTERN).timestamp()


def alert_text(day: str, name: str) -> str:
    return f'Your {date.fromisoformat(day):%A} class has a new trainer: {name}.'


def conversation_id(owner: str) -> str:
    return hashlib.sha256(f'level6-trainer-monitor:{owner}'.encode()).hexdigest()[:32]


class TrainerStore:
    def __init__(self, path=None):
        from paths import data_dir
        self.path = Path(path) if path else data_dir() / 'level6_trainers.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS monitor (
                    owner TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                    delivery TEXT NOT NULL DEFAULT 'chat', bot_id TEXT NOT NULL DEFAULT '',
                    interval_seconds INTEGER NOT NULL DEFAULT 3600, horizon_days INTEGER NOT NULL DEFAULT 14,
                    revision INTEGER NOT NULL DEFAULT 0, next_read REAL NOT NULL DEFAULT 0,
                    last_read REAL, last_error TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0,
                    occupied_dates TEXT NOT NULL DEFAULT '[]', lease TEXT, lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS baseline (
                    owner TEXT NOT NULL, day TEXT NOT NULL, trainer_id TEXT NOT NULL,
                    trainer_name TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                    last_verified REAL NOT NULL, seed_pending INTEGER NOT NULL DEFAULT 0,
                    candidate_id TEXT, candidate_name TEXT, candidate_at REAL,
                    PRIMARY KEY(owner, day)
                );
                CREATE TABLE IF NOT EXISTS changes (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, day TEXT NOT NULL, version INTEGER NOT NULL,
                    old_trainer_id TEXT NOT NULL, trainer_id TEXT NOT NULL, trainer_name TEXT NOT NULL,
                    message TEXT NOT NULL, observed_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    chat_visible INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT -1,
                    detail TEXT NOT NULL DEFAULT '', UNIQUE(owner, day, version)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    change_id TEXT NOT NULL, number INTEGER NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, finished_at REAL, detail TEXT NOT NULL DEFAULT '', request_key TEXT,
                    PRIMARY KEY(change_id, number), UNIQUE(change_id, request_key)
                );
            ''')
            for path in (self.path, Path(str(self.path) + '-wal'), Path(str(self.path) + '-shm')):
                if path.exists():
                    os.chmod(path, 0o600)
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self, owner):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO monitor(owner) VALUES (?)', (owner,))
            return dict(db.execute('SELECT * FROM monitor WHERE owner=?', (owner,)).fetchone())

    def configure(self, owner, *, enabled, delivery, bot_id, interval_seconds=3600, horizon_days=14):
        if (type(enabled) is not bool or delivery not in ('chat', 'group')
                or type(interval_seconds) is not int or not 900 <= interval_seconds <= 86400
                or type(horizon_days) is not int or horizon_days not in (7, 14)):
            raise ValueError('Invalid trainer monitor settings')
        self.settings(owner)
        with self.db() as db:
            db.execute('''UPDATE monitor SET enabled=?, delivery=?, bot_id=?, interval_seconds=?,
                horizon_days=?, revision=revision+1, next_read=0 WHERE owner=?''',
                (int(enabled), delivery, bot_id, interval_seconds, horizon_days, owner))
        return self.settings(owner)

    def owners(self):
        with self.db() as db:
            return [r[0] for r in db.execute('SELECT owner FROM monitor WHERE enabled=1')]

    def claim(self, owner, *, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cfg = db.execute('SELECT * FROM monitor WHERE owner=?', (owner,)).fetchone()
            if not cfg or not cfg['enabled'] or cfg['lease_until'] > now or cfg['next_read'] > now:
                return None
            # An abandoned send has an unknown outcome, never an automatic retry.
            db.execute("UPDATE attempts SET state='unknown', finished_at=?, detail='Worker interrupted during send' "
                       "WHERE change_id IN (SELECT id FROM changes WHERE owner=?) AND state='sending'", (now, owner))
            db.execute("UPDATE changes SET state='unknown', detail='Worker interrupted during send' "
                       "WHERE owner=? AND state='sending'", (owner,))
            lease = uuid.uuid4().hex
            db.execute('UPDATE monitor SET lease=?, lease_until=? WHERE owner=?', (lease, now + 300, owner))
            return {**dict(cfg), 'lease': lease}

    @staticmethod
    def _current(db, cfg):
        row = db.execute('SELECT * FROM monitor WHERE owner=?', (cfg['owner'],)).fetchone()
        return row and row['enabled'] and row['lease'] == cfg['lease'] and row['revision'] == cfg['revision']

    def apply_read(self, cfg, snapshot, *, now=None, jitter=0):
        now = time.time() if now is None else now
        owner = cfg['owner']
        observed = {r['date']: r for r in snapshot['rows'] if starts(r['date']) > now}
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self._current(db, cfg):
                return []
            for day, row in observed.items():
                base = db.execute('SELECT * FROM baseline WHERE owner=? AND day=?', (owner, day)).fetchone()
                if base is None:
                    db.execute('INSERT INTO baseline(owner,day,trainer_id,trainer_name,last_verified) VALUES (?,?,?,?,?)',
                               (owner, day, row['trainer_id'], row['trainer_name'], now))
                elif base['seed_pending'] or base['trainer_id'] == row['trainer_id']:
                    db.execute('''UPDATE baseline SET trainer_id=?, trainer_name=?, last_verified=?, seed_pending=0,
                        candidate_id=NULL, candidate_name=NULL, candidate_at=NULL WHERE owner=? AND day=?''',
                        (row['trainer_id'], row['trainer_name'], now, owner, day))
                elif base['candidate_id'] == row['trainer_id'] and now - base['candidate_at'] >= CONFIRM_SECONDS:
                    version = base['version'] + 1
                    key = hashlib.sha256(f'{owner}|{day}|08:30|{version}'.encode()).hexdigest()
                    db.execute('''INSERT INTO changes(id,owner,day,version,old_trainer_id,trainer_id,trainer_name,message,observed_at)
                        VALUES (?,?,?,?,?,?,?,?,?)''', (key, owner, day, version, base['trainer_id'], row['trainer_id'],
                        row['trainer_name'], alert_text(day, row['trainer_name']), now))
                    db.execute('''UPDATE baseline SET trainer_id=?,trainer_name=?,version=?,last_verified=?,
                        candidate_id=NULL,candidate_name=NULL,candidate_at=NULL WHERE owner=? AND day=?''',
                        (row['trainer_id'], row['trainer_name'], version, now, owner, day))
                else:
                    # A disagreeing confirmation starts a new two-read candidate.
                    db.execute('''UPDATE baseline SET candidate_id=?, candidate_name=?, candidate_at=?
                        WHERE owner=? AND day=?''', (row['trainer_id'], row['trainer_name'], now, owner, day))
            bases = {r['day']: dict(r) for r in db.execute('SELECT * FROM baseline WHERE owner=?', (owner,))}
            occupied = {day for day in set(snapshot.get('occupied_dates', [])) | set(bases) if starts(day) > now}
            nearest = {}
            for day in sorted(occupied):
                if starts(day) > now:
                    nearest.setdefault(date.fromisoformat(day).weekday(), day)
            for change in db.execute("SELECT * FROM changes WHERE owner=? AND state IN ('pending','eligible')", (owner,)).fetchall():
                day, base = change['day'], bases.get(change['day'])
                if starts(day) <= now:
                    state = 'expired'
                elif base and (base['version'] != change['version'] or base['trainer_id'] != change['trainer_id']):
                    state = 'superseded'
                elif (base and day in observed and observed[day]['trainer_id'] == base['trainer_id']
                        and not base['candidate_id'] and not base['seed_pending']
                        and nearest.get(date.fromisoformat(day).weekday()) == day):
                    state = 'eligible'
                else:
                    state = 'pending'
                if (base and day in observed and base['version'] == change['version']
                        and base['trainer_id'] == change['trainer_id']):
                    db.execute('UPDATE changes SET trainer_name=?,message=? WHERE id=?',
                               (base['trainer_name'], alert_text(day, base['trainer_name']), change['id']))
                db.execute('UPDATE changes SET state=?, chat_visible=max(chat_visible,?) WHERE id=?',
                           (state, int(state == 'eligible'), change['id']))
                if state in ('superseded', 'expired'):
                    db.execute("UPDATE attempts SET state='cancelled' WHERE change_id=? AND state='queued'", (change['id'],))
            candidates = min((b['candidate_at'] for day, b in bases.items()
                              if b['candidate_at'] is not None and starts(day) > now and day in observed), default=None)
            next_read = now + cfg['interval_seconds'] + jitter
            if candidates is not None:
                next_read = min(next_read, max(now + CONFIRM_SECONDS, candidates + CONFIRM_SECONDS))
            db.execute('''UPDATE monitor SET next_read=?,last_read=?,last_error='',failures=0,occupied_dates=?
                WHERE owner=?''', (next_read, now, json.dumps(sorted(occupied)), owner))
            cutoff = (datetime.fromtimestamp(now, EASTERN).date() - timedelta(days=30)).isoformat()
            db.execute('DELETE FROM baseline WHERE owner=? AND day<?', (owner, cutoff))
            return [dict(r) for r in db.execute("SELECT * FROM changes WHERE owner=? AND state='eligible' ORDER BY day,version", (owner,))]

    def read_failed(self, cfg, reason, *, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            if self._current(db, cfg):
                failures = db.execute('SELECT failures FROM monitor WHERE owner=?', (cfg['owner'],)).fetchone()[0] + 1
                db.execute('UPDATE monitor SET last_error=?, failures=?, next_read=? WHERE owner=?',
                           (reason[:240], failures, now + min(3600, 60 * 2 ** min(failures, 6)), cfg['owner']))

    def delivery_blocked(self, cfg, reason):
        with self.db() as db:
            if self._current(db, cfg):
                db.execute('UPDATE monitor SET last_error=? WHERE owner=?', (reason[:240], cfg['owner']))

    def release(self, cfg):
        with self.db() as db:
            db.execute('UPDATE monitor SET lease=NULL,lease_until=0 WHERE owner=? AND lease=?', (cfg['owner'], cfg['lease']))

    def begin_delivery(self, cfg, change_id, *, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self._current(db, cfg):
                return None
            row = db.execute("SELECT * FROM changes WHERE id=? AND owner=? AND state='eligible'", (change_id, cfg['owner'])).fetchone()
            if not row or starts(row['day']) <= now:
                return None
            if cfg['delivery'] == 'chat':
                db.execute("UPDATE changes SET state='sent', detail='Delivered in Kyrex Chat' WHERE id=?", (change_id,))
                return None
            number = max(0, row['attempt'])
            existing = db.execute('SELECT * FROM attempts WHERE change_id=? AND number=?', (change_id, number)).fetchone()
            if existing and existing['state'] != 'queued':
                return None
            if existing:
                db.execute("UPDATE attempts SET state='sending' WHERE change_id=? AND number=?", (change_id, number))
            else:
                db.execute("INSERT INTO attempts(change_id,number,state,created_at) VALUES (?,?,'sending',?)", (change_id, number, now))
            db.execute("UPDATE changes SET state='sending',attempt=? WHERE id=?", (number, change_id))
            db.execute('UPDATE monitor SET lease_until=? WHERE owner=?', (now + 2100, cfg['owner']))
            return {**dict(row), 'attempt': number}

    def finish_delivery(self, attempt, state, detail='', *, now=None):
        if state not in ('sent', 'failed', 'unknown'):
            raise ValueError('Invalid delivery outcome')
        now = time.time() if now is None else now
        with self.db() as db:
            changed = db.execute("UPDATE attempts SET state=?,finished_at=?,detail=? WHERE change_id=? AND number=? AND state='sending'",
                                (state, now, detail[:240], attempt['id'], attempt['attempt'])).rowcount
            if changed:
                db.execute("UPDATE changes SET state=?,detail=? WHERE id=? AND state='sending' AND attempt=?",
                           (state, detail[:240], attempt['id'], attempt['attempt']))

    def resend(self, owner, change_id, request_key, *, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cfg = db.execute('SELECT * FROM monitor WHERE owner=?', (owner,)).fetchone()
            row = db.execute('SELECT * FROM changes WHERE owner=? AND id=?', (owner, change_id)).fetchone()
            if not row:
                raise LookupError('Alert not found')
            if db.execute('SELECT 1 FROM attempts WHERE change_id=? AND request_key=?', (change_id, request_key)).fetchone():
                return
            if not cfg or not cfg['enabled'] or cfg['delivery'] != 'group':
                raise ValueError('Enable group alerts before resending')
            base = db.execute('SELECT * FROM baseline WHERE owner=? AND day=?', (owner, row['day'])).fetchone()
            if (row['state'] not in ('sent', 'failed', 'unknown') or starts(row['day']) <= now
                    or not base or base['version'] != row['version'] or base['trainer_id'] != row['trainer_id']):
                raise ValueError('Only the current upcoming trainer change can be resent')
            number = row['attempt'] + 1
            db.execute("INSERT INTO attempts(change_id,number,state,created_at,request_key) VALUES (?,?,'queued',?,?)", (change_id, number, now, request_key))
            db.execute("UPDATE changes SET state='pending',attempt=?,detail='Manual resend requested' WHERE id=?", (number, change_id))
            db.execute('UPDATE monitor SET next_read=0 WHERE owner=?', (owner,))

    def reset(self, owner):
        with self.db() as db:
            if db.execute("SELECT 1 FROM changes WHERE owner=? AND state='sending'", (owner,)).fetchone():
                raise ValueError('Wait for the in-flight delivery before resetting the baseline')
            db.execute('UPDATE baseline SET seed_pending=1,candidate_id=NULL,candidate_name=NULL,candidate_at=NULL WHERE owner=?', (owner,))
            db.execute("UPDATE changes SET state='superseded' WHERE owner=? AND state IN ('pending','eligible')", (owner,))
            db.execute("UPDATE attempts SET state='cancelled' WHERE state='queued' AND change_id IN (SELECT id FROM changes WHERE owner=?)", (owner,))
            db.execute('UPDATE monitor SET revision=revision+1,next_read=0 WHERE owner=?', (owner,))

    def history(self, owner, limit=100, *, visible_only=False):
        with self.db() as db:
            rows = db.execute('SELECT * FROM changes WHERE owner=?' + (' AND chat_visible=1' if visible_only else '') +
                              ' ORDER BY observed_at DESC,version DESC LIMIT ?', (owner, min(200, max(1, limit)))).fetchall()
            return [dict(r) for r in rows]
