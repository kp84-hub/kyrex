"""Bounded inbox pages and one tools-disabled extraction per durable scan task."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
import time

import budget_evidence
from budget_watch import BudgetStore, eligible
import calendar_watch_sources as sources
import email_accounts

RECEIPT = re.compile(r'\b(?:receipt|invoice|purchase|purchased|order|ordered|payment|paid|charged|'
                     r'billing|bill|refund|renewal|renews|renew|subscription|membership|trial|transaction)\b', re.I)


def collect(owner, cfg, store, now):
    reader = email_accounts.default_store().reader(owner)
    accounts = list(reader.rows)
    cursors = dict(cfg.get('cursors') or {})
    # One mailbox page per task; round-robin prevents a busy inbox starving others.
    index = int(cursors.get('_turn', 0)) % len(accounts)
    account = accounts[index]
    # Finish a live provider continuation promptly (some expire in minutes).
    # Each task remains one bounded page; other inboxes resume after this window.
    account = next((key for key in accounts if (cursors.get(key) or {}).get('next')), account)
    state = dict(cursors.get(account) or {'since': cfg['since']})
    until = state.get('until') or now
    lower = datetime.fromtimestamp(state['since'], timezone.utc).date().isoformat()
    upper = (datetime.fromtimestamp(until, timezone.utc) + timedelta(days=1)).date().isoformat()
    query = f'in:inbox after:{lower} before:{upper}'
    native = reader._reader(account)
    # Provider continuation tokens stay sealed; a failure never advances this page.
    try:
        page = native.search(query=query, max_results=10, page_token=state.get('next') or None)
    except Exception:
        if state.get('next'):
            # Some providers expire cursors. Restart the same window; seen IDs
            # prevent duplicate extraction or ledger writes.
            page = native.search(query=query, max_results=10)
        else: raise
    batch, scanned, undated = [], [], 0
    for hit in page.get('messages', [])[:10]:
        key = sources.digest({'kind': 'email', 'account': account, 'id': hit['id']})
        if store.seen(owner, key): continue
        store.valid(owner, cfg['revision'])
        meta = native.message(hit['id'])
        reader._check()
        if meta.get('owner') != owner or meta.get('id') != hit['id']:
            raise ValueError('Email identity did not match the selected inbox.')
        headers = meta.get('headers') or {}
        received = sources.stamp(headers.get('Date'))
        if not received:
            undated += 1
            continue
        timestamp = datetime.fromisoformat(received).timestamp()
        # Exact opt-in cutoff, independent of date-only provider searches.
        if timestamp < cfg['since'] or timestamp > until: continue
        source = {'key': key, 'received': received,
                  'label': str(headers.get('From') or '')[:254] + ' · ' + reader.rows[account]['address']}
        if RECEIPT.search(str(headers.get('Subject') or '') + ' ' + str(meta.get('snippet') or '')):
            value = native.read_message(hit['id'])
            reader._check()
            if value.get('owner') != owner or value.get('id') != hit['id']:
                raise ValueError('Email identity did not match the selected inbox.')
            source['text'] = ('Subject: ' + str(headers.get('Subject') or '') + '\n' + str(value.get('body') or ''))[:6000]
            batch.append(source)
        scanned.append(source)
    reader._check()
    if page.get('next_page_token'):
        cursors[account] = {**state, 'until': until, 'next': page['next_page_token']}
        cursors['_more_accounts'] = True
    else:
        cursors[account] = {'since': max(cfg['since'], until - 2 * 86400)}
        cursors['_turn'] = (accounts.index(account) + 1) % len(accounts)
        cursors['_more_accounts'] = cursors['_turn'] != 0
    detail = ('One inbox page checked; selected inboxes rotate between checks. '
              'English receipt-related subjects/snippets are screened; email text is limited to 6,000 characters per candidate. '
              'Attachments, spam/trash, missing dates and receipts without matching subjects may be missed.')
    if undated: detail += f' {undated} email(s) had no usable date.'
    if page.get('next_page_token'): detail += ' More messages remain for later checks.'
    return batch, scanned, cursors, detail


def run_scan(ctx, owner, text, task_id, send, *, on_result=None, on_progress=None):
    import sys
    from pathlib import Path
    backend = str(Path(__file__).parent / 'web' / 'backend')
    if backend not in sys.path: sys.path.insert(0, backend)
    store = BudgetStore()
    revision = json.loads(text)['revision']
    def active():
        if store.tasks.is_cancel_requested(task_id): raise ValueError('Budget scan was cancelled.')
        return store.valid(owner, revision)
    try:
        cfg = active()
        task = store.tasks.get(task_id) or {}
        if (task.get('status') != 'running' or task.get('chat_id') != owner
                or task.get('bot_id') != cfg['bot_id'] or task.get('task_text') != text
                or task.get('executor_prefix') != 'budget_watch'
                or ctx.bot_owner != owner or ctx.bot_id != cfg['bot_id']):
            raise ValueError('Budget scan task binding is unavailable.')
        now = time.time()
        if on_progress: on_progress({'message': 'Checking a selected inbox for new receipts and subscriptions.'})
        batch, scanned, cursors, detail = collect(owner, cfg, store, now)
        active()
        records = asyncio.run(budget_evidence.extract(owner, eligible(owner, cfg['bot_id']), batch, cfg['dollar_currency'], cfg['time_zone'])) if batch else []
        active()
        inserted = store.observe(owner, revision, scanned, records, cursors, now, detail)
        result = {'mode': 'budget_watch', 'status': 'no_changes', 'count': len(inserted),
                  'final_response': f'Budget scan completed. {len(inserted)} new record(s). ' + detail}
    except Exception:
        with store.db() as db:
            db.execute('UPDATE budget_settings SET detail=? WHERE owner=? AND revision=?',
                ('Budget scan could not finish. Check selected inboxes and the Budget Bot model. Unprocessed emails will be retried.', owner, revision))
        raise ValueError('Budget scan could not finish. Check Budget Bot settings.') from None
    if on_result: on_result(result)
    send(owner, result['final_response'])
