"""Bounded read adapters for selected inboxes and consented phone snapshots."""
import hashlib
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import connectors
import device_messages
import email_accounts


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def stamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        try: dt = parsedate_to_datetime(str(value))
        except (ValueError, TypeError, OverflowError): return None
    if dt.tzinfo is None: return None
    return dt.astimezone(timezone.utc).isoformat()


def phone_binding(owner):
    store = device_messages.MessagesStore()
    with store._db() as db:
        row = db.execute('SELECT credential FROM devices WHERE owner=?', (device_messages._owner(owner),)).fetchone()
    if not row:
        raise ValueError('Connect Messages and sync your phone before enabling message scans.')
    return row[0]


def binding(owner, emails, messages):
    result = {}
    if emails:
        registry = email_accounts.default_store()
        if not registry.available(owner, connectors.default_store()):
            raise ValueError('Select a connected inbox in Connections → Email accounts.')
        result['email'] = list(registry.epoch(owner))
        # The legacy email selector predates mailbox grant revisions. Pin its
        # Google grant as well so reconnecting another Gmail account cannot
        # reuse an earlier source review.
        import agent_inbox
        if registry.selection(owner) in {'legacy','all'} and agent_inbox.default_store().record(owner)['source'] == 'gmail':
            live = connectors.default_store().status(owner)
            result['legacy_google'] = [live.get('connected_at'), live.get('connected'), sorted(live.get('scopes') or [])]
    if messages:
        result['messages'] = phone_binding(owner)
    return result


def check(owner, cfg):
    if binding(owner, cfg['emails'], cfg['messages']) != cfg['binding']:
        raise ValueError('Source connections or selected inboxes changed. Save event review settings again.')


class SourceUnavailable(ValueError):
    """A selected source cannot be read under its saved binding."""


def check_source(owner, cfg, kind):
    email = kind == 'email'
    keys = ('email', 'legacy_google') if email else ('messages',)
    name = 'email' if email else 'phone message'
    if not cfg['emails' if email else 'messages']:
        raise SourceUnavailable(f'The {name} source is no longer enabled.')
    try:
        current = binding(owner, email, not email)
    except (connectors.ConnectorError, ValueError):
        fix = ('Check Connections → Email accounts.' if email
               else 'Open Kyrex on your phone and sync Messages.')
        raise SourceUnavailable(f'The selected {name} connection is unavailable. {fix}') from None
    if any(current.get(key) != cfg['binding'].get(key) for key in keys):
        raise SourceUnavailable(f'The selected {name} connection changed. Save Event Review settings again.')


def source_ready(owner, cfg, kind, notes):
    try:
        check_source(owner, cfg, kind)
        return True
    except SourceUnavailable as exc:
        notes.append(str(exc))
        return False


def _email_source(owner, account, native_id, reader):
    value = reader._reader(account).read_message(native_id)
    reader._check()
    if value.get('owner') != owner or value.get('id') != native_id:
        raise ValueError('Email source identity did not match the selected inbox.')
    headers = value.get('headers') or {}
    text = ('Subject: ' + str(headers.get('Subject') or '') + '\n' + str(value.get('body') or ''))[:20200]
    ref = {'kind': 'email', 'account': account, 'id': native_id}
    return {'key': digest(ref), 'ref': ref, 'text': text, 'digest': digest(text),
            'label': str(headers.get('From') or '')[:254] + ' · ' + reader.rows[account]['address'],
            'received': stamp(headers.get('Date')), 'coverage': 'Email text; attachments are excluded.'}


def _phone_source(item, synced):
    # Include body digest in the observation key so a changed message is read
    # again. Provider IDs alone are not globally unique across threads.
    ref = {'kind': 'messages', 'id': item.get('id', ''),
           'thread': item.get('conversation_id') or item.get('conversation') or item.get('number') or '',
           'received': item.get('received', ''), 'digest': digest(item)}
    text = str(item.get('body') or '')[:10000]
    return {'key': digest(ref), 'ref': ref, 'text': text, 'digest': digest(text),
            'label': str(item.get('conversation') or item.get('sender') or 'Phone message')[:200],
            'received': stamp(item.get('received')), 'synced_at': synced,
            'coverage': 'Synced phone text only; up to 100 messages from 10 recent conversations. Attachments and older history are excluded.'}


def collect(owner, cfg, seen, now):
    """Read each still-authorized source and keep coverage failures visible."""
    sources, notes = [], []
    covered = False
    cutoff = max(cfg['since'], now - 7 * 86400)
    if cfg['emails'] and source_ready(owner, cfg, 'email', notes):
        email_sources = []
        try:
            reader = email_accounts.default_store().reader(owner)
            for account in reader.rows:
                try:
                    native = reader._reader(account)
                    page = native.search(query='in:inbox newer_than:7d', max_results=50)
                    if page.get('next_page_token'):
                        notes.append('Email scan reached its 50-message limit for one inbox; older messages may be outside coverage.')
                    for hit in page.get('messages', [])[:50]:
                        try:
                            ref = {'kind': 'email', 'account': account, 'id': hit['id']}
                            if seen(digest(ref)):
                                continue
                            source = _email_source(owner, account, hit['id'], reader)
                            received = source['received']
                            if received and datetime.fromisoformat(received).timestamp() >= cutoff:
                                email_sources.append(source)
                            elif not received:
                                notes.append('An email without a reliable received date could not be scanned.')
                        except (connectors.ConnectorError, ValueError, KeyError, TypeError, OSError):
                            notes.append('An email could not be read and will be retried.')
                    covered = True
                except (connectors.ConnectorError, ValueError, OSError):
                    notes.append('One selected inbox could not be read. Its messages will be checked again.')
        except (connectors.ConnectorError, ValueError, OSError):
            notes.append('Selected email accounts could not be read. Check Connections → Email accounts.')
        if source_ready(owner, cfg, 'email', notes):
            sources.extend(email_sources)
        else:
            covered = False
    if cfg['messages'] and source_ready(owner, cfg, 'messages', notes):
        phone_sources = []
        try:
            payload, synced = device_messages.MessagesStore()._snapshot(owner)
            if not synced or synced < now - 86400:
                raise ValueError()
            for item in payload['messages']:
                source = _phone_source(item, synced)
                if seen(source['key']):
                    continue
                if source['received'] and datetime.fromisoformat(source['received']).timestamp() >= cutoff:
                    phone_sources.append(source)
            if source_ready(owner, cfg, 'messages', notes):
                sources.extend(phone_sources)
                covered = True
                notes.append('Messages coverage: synced text snapshot only, up to 100 messages from 10 recent conversations.')
        except (ValueError, LookupError, OSError):
            notes.append('Phone messages are unavailable or the snapshot is over 24 hours old. Open Kyrex on your phone and sync.')
    if not covered:
        raise SourceUnavailable(' '.join(dict.fromkeys(notes)) or
                                'No selected source could be scanned. Check Event Review settings.')
    # Round-robin inboxes plus messages so a busy mailbox cannot starve SMS.
    groups = {}
    for source in sources:
        groups.setdefault(source['ref'].get('account', 'messages'), []).append(source)
    ordered = []
    while any(groups.values()):
        for group in groups.values():
            if group: ordered.append(group.pop(0))
    return ordered, list(dict.fromkeys(notes))

def reread(owner, cfg, source):
    ref = source['ref']
    check_source(owner, cfg, ref['kind'])
    if ref['kind'] == 'email':
        current = _email_source(owner, ref['account'], ref['id'], email_accounts.default_store().reader(owner))
    else:
        payload, synced = device_messages.MessagesStore()._snapshot(owner)
        current = next((_phone_source(item, synced) for item in payload['messages']
                        if _phone_source(item, synced)['key'] == source['key']), None)
    if not current or current['digest'] != source['digest']:
        raise ValueError('The source message changed or is outside current synced history. Review it again before adding an event.')
    check_source(owner, cfg, ref['kind'])
    return current
