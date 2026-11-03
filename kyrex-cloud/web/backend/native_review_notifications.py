"""Observe saved native reviews that do not have a CloudTask approval event.

Only fixed SQLite metadata and owner-scoped Chat references are read. Message
bodies, recipients, sealed payloads and credentials never enter the inbox.
This observer cannot prepare, decide, submit, expire or replay source work.
"""
import json
import logging
import re
import sqlite3
import time

from paths import data_dir
from coordinator_notifications import owned_conversation

SOURCES = (
    ('email', 'email_drafts.sqlite3', 'drafts', 'draft', 'created', 86400),
    ('booking', 'bookings.sqlite3', 'bookings', 'proposed', 'updated', 600),
    ('message', 'device_messages.sqlite3', 'message_sends', 'ready', 'expires', 0),
)
LABELS = {'email': 'Email draft needs review', 'booking': 'Booking needs review',
          'message': 'Message preview needs review'}
ID = re.compile(r'[a-f0-9]{32}\Z')


def message_conversation(chat, owner, identity):
    folder = chat._conv_path(owner, '0' * 32).parent
    for path in sorted(folder.glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)[:500]:
        if not ID.fullmatch(path.stem) or path.stat().st_size > 2 * 1024 * 1024:
            continue
        conv = owned_conversation(chat, owner, path.stem)
        if conv and any(isinstance(message, dict) and isinstance(message.get('message_send'), dict)
                        and message['message_send'].get('id') == identity
                        for message in conv.get('messages', [])):
            return path.stem
    return None


def observe(chat, store, *, now=None):
    now = time.time() if now is None else now
    registry = chat.bots.load_bots()
    for kind, filename, table, state, clock, ttl in SOURCES:
        path = data_dir() / filename
        if not path.exists():
            continue
        try:
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5) as db:
                db.row_factory = sqlite3.Row
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    continue
                columns = 'id,owner,state,expires' if kind == 'message' else 'id,owner,conversation,revision,state,' + clock
                deadline = now if kind == 'message' else now - ttl
                # SQL identifiers and state values are host constants above.
                rows = db.execute(f'SELECT {columns} FROM {table} WHERE state=? AND {clock}>? ORDER BY {clock} DESC LIMIT 500',
                                  (state, deadline)).fetchall()
                for row in rows:
                    owner, identity = row['owner'], row['id']
                    if (not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', owner)
                            or not isinstance(identity, str) or not ID.fullmatch(identity)):
                        continue
                    cid = message_conversation(chat, owner, identity) if kind == 'message' else row['conversation']
                    conv = owned_conversation(chat, owner, cid)
                    bot = registry.get((conv or {}).get('bot_id')) or {}
                    if not conv or (conv.get('bot_id') and bot.get('owner') != owner):
                        continue
                    revision = row['revision'] if kind != 'message' else 'ready'
                    if not isinstance(revision, str) or (kind != 'message' and not ID.fullmatch(revision)):
                        continue
                    source = f'native-review:{kind}:{identity}:{revision}'
                    title = str(bot.get('name') or 'Kyrex')[:60] + ' · ' + LABELS[kind]
                    store.record(owner, source, 'awaiting_review', title, cid)
                # Retire each saved review against its current source row;
                # bounded enumeration cannot dismiss an older valid review.
                for alert in store.coordinator_candidates():
                    prefix = 'native-review:' + kind + ':'
                    if not alert['source'].startswith(prefix):
                        continue
                    parts = alert['source'].split(':')
                    if len(parts) != 4 or not ID.fullmatch(parts[2]):
                        continue
                    row = db.execute(f'SELECT {columns} FROM {table} WHERE owner=? AND id=?',
                                     (alert['owner'], parts[2])).fetchone()
                    if (not row or row['state'] != state or row[clock] <= deadline
                            or (kind != 'message' and (row['revision'] != parts[3]
                                or row['conversation'] != alert['conversation']))):
                        store.resolve(alert['owner'], source=alert['source'])
        except (OSError, sqlite3.Error, ValueError, TypeError):
            logging.getLogger(__name__).warning('Saved %s review alerts are unavailable; observation will retry.', kind)
