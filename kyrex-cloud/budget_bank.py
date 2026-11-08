"""Owner-scoped bank sync in the existing task DB; code-only aggregate boundary."""
from datetime import date, datetime, timedelta
import json
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import connectors
import budget_plaid as provider
from budget_watch import BudgetStore, cid

POLICY = {'budget:read': 0, '*': 'deny'}
PROMPT = """You are Budget Bot. Use the current host-provided budget snapshot.
The host has already read and calculated it; private Chat has no tools to call.
Use its exact dates and formatted currency amounts. Never rescale an amount,
recalculate a balance, or use earlier replies as a source of financial figures.
Never invent missing figures, merchant names or account details.
The host supplies only category/day totals and an optional owner-entered starting
balance estimate. Never request bank credentials, merchants, transaction locations,
account numbers or raw statements. Keep currencies separate. Explain trends using
the compared periods and data freshness. Missing or incomplete bank data is not
zero spending. The balance is an estimate for selected cash accounts, not a live
bank balance or a safe-to-spend promise. Pending items are excluded. Transfers and
debt payments are excluded from spending but affect cash changes. Settings →
Budget Bot controls the bank connection, account selection and starting balance.
You cannot pay, transfer, trade or edit the bank. Give concise, useful answers.
"""


def eligible(owner, bot_id, require_running=True):
    import budget_watch
    return budget_watch.eligible(owner, bot_id, require_running=require_running, email=False)


def day(value):
    try:
        if date.fromisoformat(value).isoformat() == value: return value
    except (ValueError, TypeError): pass
    raise provider.BankError('Choose a date as YYYY-MM-DD.')


class BankStore(BudgetStore):
    def __init__(self, tasks=None):
        super().__init__(tasks)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS budget_bank (
                    owner TEXT PRIMARY KEY, revision TEXT NOT NULL, state TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, bot_id TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL, next_scan REAL NOT NULL DEFAULT 0,
                    last_scan REAL, detail TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS budget_bank_transactions (
                    owner TEXT NOT NULL, identity TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(owner,identity));
            ''')

    def get(self, owner):
        with self.db() as db:
            row = db.execute('SELECT * FROM budget_bank WHERE owner=?', (owner,)).fetchone()
        return self._opened(row)

    @staticmethod
    def _opened(row):
        if not row: return None
        value = connectors.unseal_tokens(row['data'])
        if not value: raise provider.BankError('Bank storage is unavailable. Check the deployment encryption key.')
        return {**dict(row), 'private': value, 'enabled': bool(row['enabled'])}

    def _write(self, owner, revision, *, state=None, enabled=None, bot_id=None, data=None, detail=None, clear_transactions=False, **fields):
        values = dict(fields)
        for key, value in [('state', state), ('enabled', enabled), ('bot_id', bot_id), ('detail', detail)]:
            if value is not None: values[key] = value
        if data is not None: values['data'] = connectors.seal_tokens(data)
        values['revision'] = uuid.uuid4().hex
        with self.db() as db:
            changed = db.execute('UPDATE budget_bank SET ' + ','.join(k + '=?' for k in values)
                + ' WHERE owner=? AND revision=?', (*values.values(), owner, revision)).rowcount
            if not changed: raise provider.BankError('Bank settings changed. Refresh before continuing.')
            if clear_transactions:
                db.execute('DELETE FROM budget_bank_transactions WHERE owner=?', (owner,))
        return self.get(owner)

    def view(self, owner):
        row = self.get(owner)
        try:
            client = provider.Plaid()
            ready, environment = True, client.environment
        except provider.BankError:
            ready, environment = False, ''
        result = {'available': ready, 'environment': environment, 'state': 'disconnected',
            'revision': '', 'enabled': False, 'bot_id': '', 'accounts': [], 'selected': [],
            'start_date': '', 'starting_balance': '', 'currency': 'USD', 'time_zone': 'UTC',
            'interval_seconds': 21600, 'last_scan': None, 'detail': '', 'flow': '',
            'configured': False, 'private_mode': False, 'conversation_id': cid(owner), 'privacy': 'category_totals_only'}
        if not row: return result
        data = row['private']
        result.update({key: row[key] for key in ('revision', 'state', 'enabled', 'bot_id', 'last_scan', 'detail')})
        result.update({key: data[key] for key in ('selected', 'start_date', 'starting_balance', 'currency',
            'time_zone', 'interval_seconds', 'flow') if key in data})
        result['accounts'] = [{key: a[key] for key in ('ref', 'label', 'kind')} for a in data.get('accounts', [])]
        result['configured'] = bool(data.get('configured'))
        result['private_mode'] = bool(data.get('private_mode'))
        if data.get('connected_at'):
            zone = ZoneInfo(data.get('time_zone', 'UTC'))
            result['earliest_start_date'] = (datetime.fromtimestamp(data['connected_at'], zone).date() - timedelta(days=29)).isoformat()
        return result

    def start(self, owner):
        client = provider.Plaid()
        old = self.get(owner)
        if old and old['state'] not in {'disconnected'}:
            if old['state'] == 'linking' and old['private'].get('expires', 0) > time.time() and old['private'].get('url'):
                return {'url': old['private']['url'], 'flow': old['private']['flow']}
            raise provider.BankError('A bank connection or sign-in already exists. Finish it or disconnect before starting another.')
        flow = uuid.uuid4().hex
        data = {'flow': flow, 'expires': time.time() + 1800, 'binding': client.binding,
                'environment': client.environment, 'private_mode': bool(old and old['private'].get('private_mode'))}
        if old: row = self._write(owner, old['revision'], state='creating_link', data=data)
        else:
            with self.db() as db:
                try:
                    db.execute('INSERT INTO budget_bank(owner,revision,state,data) VALUES (?,?,?,?)',
                        (owner, uuid.uuid4().hex, 'creating_link', connectors.seal_tokens(data)))
                except Exception: raise provider.BankError('Bank setup already started. Refresh its status.') from None
            row = self.get(owner)
        try:
            value = client.start(owner, flow)
            self._write(owner, row['revision'], state='linking', data={**data, **value})
            return {'url': value['url'], 'flow': flow}
        except Exception:
            # Do not auto-repeat a possibly accepted Link creation.
            self._write(owner, row['revision'], state='link_error', detail='Bank sign-in could not start. Disconnect this attempt before trying again.')
            raise provider.BankError('Bank sign-in could not start. Check its saved status.') from None

    def _client(self, row):
        client = provider.Plaid()
        if row['private'].get('binding') != client.binding:
            raise provider.BankError('The Plaid deployment account or environment changed. Restore it before continuing.')
        return client

    def finish(self, owner, flow):
        row = self.get(owner)
        if not row or row['private'].get('flow') != flow:
            raise provider.BankError('This bank sign-in belongs to another session or has expired.')
        if row['state'] == 'connected': return self.view(owner)
        if row['state'] not in {'linking', 'exchanged'} or row['private'].get('expires', 0) <= time.time():
            raise provider.BankError('Bank sign-in is unavailable. Check its status before starting again.')
        client = self._client(row)
        data = dict(row['private'])
        if row['state'] == 'linking':
            sessions = client.call('/link/token/get', link_token=data['link_token'])
            tokens = {v.get('public_token') for s in sessions.get('link_sessions', [])
                for v in (s.get('results') or {}).get('item_add_results', []) if v.get('public_token')}
            if not tokens: raise provider.BankError('Bank sign-in has not finished. Complete Plaid, then check again.')
            if len(tokens) != 1: raise provider.BankError('Bank sign-in returned an unexpected number of connections.')
            # Durable single claim before exchanging a one-time public token.
            data['exchange_started'] = time.time()
            row = self._write(owner, row['revision'], state='exchanging', data=data)
            try:
                exchanged = client.call('/item/public_token/exchange', public_token=tokens.pop())
                if not isinstance(exchanged.get('access_token'), str) or not exchanged['access_token']: raise ValueError()
                data['access_token'] = exchanged['access_token']
                row = self._write(owner, row['revision'], state='exchanged', data=data)
            except Exception:
                self._write(owner, row['revision'], state='exchange_unknown', detail=
                    'Sign-in exchange could not be verified. Do not repeat it. Revoke Kyrex in the Plaid Portal before disconnecting this attempt.')
                raise provider.BankError('Bank sign-in outcome is unknown. Check its saved status.') from None
        # Read failures after exchange can resume with the saved credential.
        available = provider.accounts(client.call('/accounts/get', access_token=data['access_token']))
        data = {key: data[key] for key in ('access_token', 'binding', 'environment', 'flow', 'private_mode')}
        data.update(accounts=available, selected=[], cursor='', configured=False, connected_at=time.time())
        self._write(owner, row['revision'], state='connected', data=data, detail='Choose accounts and save private budget tracking to start syncing.')
        return self.view(owner)

    def configure(self, owner, revision, *, enabled, bot_id, selected, start_date,
                  starting_balance='', currency='USD', time_zone='UTC', interval_seconds=21600):
        row = self.get(owner)
        if not row or row['revision'] != revision or row['state'] != 'connected':
            raise provider.BankError('Refresh the connected bank before saving.')
        data = dict(row['private'])
        if type(enabled) is not bool: raise provider.BankError('Choose whether to enable bank tracking.')
        unchanged = (bot_id == row['bot_id'] and all(data.get(k) == v for k, v in {
            'selected': selected, 'start_date': start_date, 'starting_balance': starting_balance,
            'currency': currency, 'time_zone': time_zone, 'interval_seconds': interval_seconds}.items()))
        if not enabled and data.get('configured') and unchanged:
            # Pause must work even after Bot/provider/account permissions disappear.
            self._write(owner, revision, enabled=0)
            return self.view(owner)
        eligible(owner, bot_id, require_running=enabled)
        day(start_date)
        try: zone = ZoneInfo(time_zone)
        except (ValueError, TypeError, ZoneInfoNotFoundError): raise provider.BankError('Choose a valid budget timezone.') from None
        if start_date > datetime.now(zone).date().isoformat(): raise provider.BankError('The budget start date cannot be in the future.')
        history_floor = datetime.fromtimestamp(data.get('connected_at', time.time()), zone).date() - timedelta(days=29)
        if start_date < history_floor.isoformat():
            raise provider.BankError('Choose a start date within the transaction history requested when this bank was connected.')
        if (currency not in provider.CURRENCIES or type(interval_seconds) is not int
                or interval_seconds not in {3600, 7200, 21600, 86400}):
            raise provider.BankError('Choose a supported currency and check frequency.')
        available = {a['ref']: a for a in data['accounts']}
        if (not isinstance(selected, list) or not selected or len(selected) != len(set(selected))
                or any(ref not in available for ref in selected)):
            raise provider.BankError('Select accounts belonging to this bank connection.')
        if starting_balance:
            provider.minor(starting_balance)
            if not any(available[ref]['kind'] == 'depository' for ref in selected):
                raise provider.BankError('A starting cash balance needs a selected checking or savings account.')
        if enabled:
            import bot_provider
            bot_provider.resolve_bot_provider(owner, eligible(owner, bot_id))
        import bots
        # Only narrows authority already checked above. The setting explicitly
        # converts the selected dedicated Budget Bot to totals-only access.
        bots.update_bot(bot_id, policy=dict(POLICY), system_prompt=PROMPT)
        # This dedicated Bot becomes totals-only across future Chat turns.
        # Pause the receipt watcher first; already-sent provider requests cannot be recalled.
        old = BudgetStore(self.tasks).settings(owner)
        BudgetStore(self.tasks).configure(owner, enabled=False, bot_id=old['bot_id'],
            interval_seconds=old['interval_seconds'], dollar_currency=old['dollar_currency'], time_zone=old['time_zone'])
        selection_changed = set(selected) != set(data.get('selected', []))
        if selection_changed:
            data['cursor'] = ''
            data.pop('batch', None)
        data.update(selected=selected, start_date=start_date, starting_balance=starting_balance,
            currency=currency, time_zone=time_zone, interval_seconds=interval_seconds, configured=True,
            private_mode=True, complete=False)
        self._write(owner, revision, enabled=int(enabled), bot_id=bot_id, data=data,
            clear_transactions=selection_changed, next_scan=time.time(), detail='Private bank budget saved. Waiting for a complete transaction sync.')
        return self.view(owner)

    def pause(self, owner, revision):
        row = self.get(owner)
        if not row or row['revision'] != revision:
            raise provider.BankError('Refresh bank settings before pausing.')
        self._write(owner, revision, enabled=0)
        return self.view(owner)

    def disconnect(self, owner, revision):
        row = self.get(owner)
        if not row or row['revision'] != revision: raise provider.BankError('Refresh bank status before disconnecting.')
        if row['state'] == 'exchanging' and time.time() - row['private'].get('exchange_started', time.time()) < 60:
            raise provider.BankError('Bank sign-in is being exchanged. Refresh its status before disconnecting.')
        data = row['private']
        unverified = row['state'] in {'exchanging', 'exchange_unknown'}
        row = self._write(owner, revision, enabled=0, state='disconnect_pending', detail='Local tracking stopped. Provider disconnection is pending.')
        # Local revocation fences in-flight syncs before any remote call.
        with self.db() as db:
            db.execute('DELETE FROM budget_bank_transactions WHERE owner=?', (owner,))
        if data.get('access_token'):
            try: self._client(row).call('/item/remove', access_token=data['access_token'])
            except provider.BankError:
                # Keep only what is needed to retry provider revocation; no ledger/balance.
                minimal = {k: data[k] for k in ('access_token', 'binding', 'environment', 'private_mode') if k in data}
                self._write(owner, row['revision'], data=minimal)
                raise provider.BankError('Local bank data was cleared. Provider disconnection is pending; retry here or revoke Kyrex in the Plaid Portal.') from None
        self._write(owner, row['revision'], state='disconnected', data={'disconnected': True, 'private_mode': bool(data.get('private_mode'))},
            detail=('Local bank records were cleared. Sign-in outcome was not verified; revoke any Kyrex connection in the Plaid Portal.'
                    if unverified else 'Bank tracking disconnected. Local bank records were cleared.'), last_scan=None)
        return self.view(owner)

    def valid_bank(self, owner, revision):
        row = self.get(owner)
        if not row or row['revision'] != revision or row['state'] != 'connected' or not row['enabled']:
            raise provider.BankError('Bank tracking was paused, disconnected or changed.')
        eligible(owner, row['bot_id'])
        self._client(row)
        return row

    def request_refresh(self, owner, revision):
        """Make this connection due for its existing durable worker scan."""
        self.valid_bank(owner, revision)
        with self.db() as db:
            changed = db.execute('''UPDATE budget_bank SET next_scan=CASE WHEN EXISTS (
                SELECT 1 FROM tasks WHERE chat_id=? AND executor_prefix='budget_watch'
                AND status IN ('queued','running','awaiting_approval')
            ) THEN next_scan ELSE MIN(next_scan, ?) END
            WHERE owner=? AND revision=? AND enabled=1 AND state='connected' ''',
                (owner, time.time(), owner, revision)).rowcount
        if not changed:
            raise provider.BankError('Bank tracking was paused, disconnected or changed.')

    def sync(self, owner, revision, active=lambda: None):
        row = self.valid_bank(owner, revision)
        data = row['private']
        client = self._client(row)
        selected = {a['id']: a for a in data['accounts'] if a['ref'] in data['selected']}
        # Stage a logical update across bounded tasks. Publish only when all
        # pages finish; a pagination mutation must restart at the ORIGINAL
        # committed cursor, even if earlier pages came from a previous task.
        staged = data.get('batch') or {}
        cursor = staged.get('cursor', data.get('cursor', ''))
        changes, removed = dict(staged.get('changes', {})), set(staged.get('removed', []))
        for _ in range(10):
            active(); self.valid_bank(owner, revision)
            try:
                page = client.call('/transactions/sync', access_token=data['access_token'], cursor=cursor, count=500,
                    options={'include_original_description': False, 'personal_finance_category_version': 'v2'})
            except provider.BankError as exc:
                if exc.code != 'pagination_changed': raise
                data.pop('batch', None)
                with self.db() as db:
                    db.execute("UPDATE budget_bank SET data=?,next_scan=?,detail=? WHERE owner=? AND revision=? AND enabled=1 AND state='connected'",
                        (connectors.seal_tokens(data), time.time() + 30,
                         'Bank pages changed during sync. Restarting from the last complete update.', owner, revision))
                return 0
            if (not isinstance(page.get('next_cursor'), str) or type(page.get('has_more')) is not bool
                    or any(not isinstance(page.get(k), list) for k in ('added', 'modified', 'removed'))):
                raise provider.BankError('Bank sync response could not be verified.')
            for value in page['removed']:
                identity = value.get('transaction_id')
                if not isinstance(identity, str): raise provider.BankError('Bank removal could not be verified.')
                hashed = provider.digest(identity)
                changes.pop(hashed, None); removed.add(hashed)
            for value in page['added'] + page['modified']:
                record = provider.transaction(value, selected)
                if record:
                    changes[record['identity']] = record
                    removed.discard(record['identity'])
            if len(changes) + len(removed) > 20000:
                raise provider.BankError('This bank update exceeds the supported transaction batch size. No partial update was published.')
            cursor = page['next_cursor']
            if not page['has_more']: break
        active(); self.valid_bank(owner, revision)
        if page['has_more']:
            data['batch'] = {'cursor': cursor, 'changes': changes, 'removed': sorted(removed)}
        else:
            data['cursor'] = cursor
            data.pop('batch', None)
        data['complete'] = not page['has_more'] and page.get('transactions_update_status') == 'HISTORICAL_UPDATE_COMPLETE'
        # Recheck under the same write lock used for all privacy/account edits.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT revision,enabled,state FROM budget_bank WHERE owner=?', (owner,)).fetchone()
            if not current or current['revision'] != revision or not current['enabled'] or current['state'] != 'connected':
                raise provider.BankError('Bank settings changed during sync. Results were discarded.')
            if not page['has_more']:
                for identity in removed:
                    db.execute('DELETE FROM budget_bank_transactions WHERE owner=? AND identity=?', (owner, identity))
                for identity, record in changes.items():
                    db.execute('INSERT INTO budget_bank_transactions VALUES (?,?,?) ON CONFLICT(owner,identity) DO UPDATE SET data=excluded.data',
                        (owner, identity, connectors.seal_tokens(record)))
            db.execute('UPDATE budget_bank SET data=?,last_scan=?,detail=?,next_scan=? WHERE owner=? AND revision=?',
                (connectors.seal_tokens(data), row['last_scan'] if page['has_more'] else time.time(),
                 'Bank history is still loading; totals are incomplete.' if not data['complete'] else 'Latest available transaction pages synced. Bank updates may be delayed.',
                 time.time() + (30 if page['has_more'] else 60 if not data['complete'] else data['interval_seconds']), owner, revision))
        return 0 if page['has_more'] else len(changes)

    def model_view(self, owner, month=''):
        """Build a new schema from enums/numbers. Never redact a raw provider dict."""
        with self.db() as db:
            db.execute('BEGIN')
            row = self._opened(db.execute('SELECT * FROM budget_bank WHERE owner=?', (owner,)).fetchone())
            saved = db.execute('SELECT data FROM budget_bank_transactions WHERE owner=?', (owner,)).fetchall()
        if not row or not row['private'].get('private_mode'):
            raise provider.BankError('Private bank budgeting has not been configured.')
        if row['state'] != 'connected' or not row['private'].get('configured'):
            return {'source': 'bank_category_totals', 'privacy': 'category_totals_only', 'status': 'disconnected',
                    'limitation': 'Bank tracking is disconnected. Reconnect and configure it in Settings before requesting totals.'}
        data = row['private']
        zone = ZoneInfo(data['time_zone'])
        today = datetime.now(zone).date()
        month = month or today.strftime('%Y-%m')
        try:
            start = datetime.strptime(month, '%Y-%m').date()
            if start.strftime('%Y-%m') != month: raise ValueError()
        except (ValueError, TypeError): raise provider.BankError('Choose a month as YYYY-MM.') from None
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        rows = [connectors.unseal_tokens(r['data']) for r in saved]
        # Fail closed on corrupt ciphertext, unknown categories or unexpected keys.
        keys = {'identity', 'account', 'kind', 'date', 'amount_minor', 'currency', 'category', 'pending'}
        for r in rows:
            if (set(r) != keys or r['category'] not in provider.CATEGORIES or r['currency'] not in provider.CURRENCIES
                    or type(r['amount_minor']) is not int or type(r['pending']) is not bool
                    or r['kind'] not in {'credit', 'depository'}):
                raise provider.BankError('Bank records need review before totals can be shared.')
            day(r['date'])
        rows = [r for r in rows if r['account'] in data['selected'] and r['date'] >= data['start_date'] and r['date'] <= today.isoformat()]
        categories, daily = {}, {}
        current_week = today - timedelta(days=6)
        previous_week = current_week - timedelta(days=7)
        weeks = [{}, {}]
        pending = 0
        for r in rows:
            if r['pending']:
                pending += 1
                continue
            currency, category, amount = r['currency'], r['category'], r['amount_minor']
            spending = category not in {'transfer', 'debt_payment', 'income'}
            if start.isoformat() <= r['date'] < end.isoformat():
                key = (currency, category)
                categories[key] = categories.get(key, 0) + amount
                if spending:
                    key = (r['date'], currency)
                    daily[key] = daily.get(key, 0) + amount
            if spending:
                index = 1 if r['date'] >= current_week.isoformat() else 0 if r['date'] >= previous_week.isoformat() else None
                if index is not None: weeks[index][currency] = weeks[index].get(currency, 0) + amount
        totals = {}
        income = {}
        for (currency, category), amount in categories.items():
            if category == 'income': income[currency] = income.get(currency, 0) - amount
            elif category not in {'transfer', 'debt_payment'}: totals[currency] = totals.get(currency, 0) + amount
        estimate = None
        if data.get('starting_balance'):
            change = sum(-r['amount_minor'] for r in rows if not r['pending']
                and r['kind'] == 'depository' and r['currency'] == data['currency'])
            estimate = {'currency': data['currency'], 'start_date': data['start_date'],
                'starting_minor': provider.minor(data['starting_balance']), 'net_change_minor': change,
                'estimated_minor': provider.minor(data['starting_balance']) + change}
        # All output strings below are host constants, validated dates/currencies
        # or an allowlisted category. No source labels, IDs or descriptions.
        return {'source': 'bank_category_totals', 'privacy': 'category_totals_only', 'month': month,
            'as_of_date': today.isoformat(), 'time_zone': zone.key,
            'environment': data['environment'] if data.get('environment') in {'sandbox', 'production'} else 'unknown',
            'totals_minor': totals, 'income_minor': income,
            'categories': [{'currency': k[0], 'category': k[1], 'amount_minor': v} for k, v in sorted(categories.items())],
            'daily': [{'date': k[0], 'currency': k[1], 'spending_minor': v} for k, v in sorted(daily.items())],
            'trend': {'previous_start': previous_week.isoformat(), 'previous_end': (current_week - timedelta(days=1)).isoformat(),
                      'current_start': current_week.isoformat(), 'current_end': today.isoformat(),
                      'previous_spending_minor': weeks[0], 'current_spending_minor': weeks[1],
                      'start_date_covers_both_periods': data['start_date'] <= previous_week.isoformat()},
            'cash_estimate': estimate, 'pending_count': pending, 'enabled': row['enabled'], 'last_scan': row['last_scan'],
            'uncategorized_count': sum(r['category'] == 'other' and not r['pending'] for r in rows),
            'history_loaded': bool(data.get('complete')), 'start_date': data['start_date'],
            'latest_attempt_ok': row['detail'] == 'Latest available transaction pages synced. Bank updates may be delayed.',
            'limitation': 'Selected accounts only. Dates are bank posting dates. Pending transactions do not count. Transfers and debt payments are excluded from spending. Category errors can affect totals. The cash estimate uses the owner-entered balance at the start of the chosen date and all posted changes in selected checking/savings accounts in that currency. It excludes card balances, pending items, unconnected accounts and future bills. Bank history can be incomplete or delayed.'}


def private_bot(owner, bot_id):
    row = BankStore().get(owner)
    return bool(row and row['private'].get('private_mode') and row['bot_id'] == bot_id)


def submit_due(tasks, now):
    from task_store import DuplicateTaskId
    store = BankStore(tasks)
    with store.db() as db:
        owners = db.execute("SELECT owner FROM budget_bank WHERE enabled=1 AND state='connected' AND next_scan<=?", (now,)).fetchall()
    for entry in owners:
        owner = entry['owner']; row = store.get(owner)
        try: store.valid_bank(owner, row['revision'])
        except Exception:
            with store.db() as db:
                db.execute('UPDATE budget_bank SET next_scan=?,detail=? WHERE owner=? AND revision=?',
                    (now + 3600, 'Bank tracking is blocked. Check its Bot and connection settings.', owner, row['revision']))
            continue
        with store.db() as db:
            active = db.execute("SELECT 1 FROM tasks WHERE chat_id=? AND executor_prefix='budget_watch' AND status IN ('queued','running','awaiting_approval')", (owner,)).fetchone()
        if active: continue
        identity = 'budget-bank-' + provider.digest(owner + row['revision'] + str(row['next_scan']))
        try:
            tasks.submit(session_key=row['bot_id'], bot_id=row['bot_id'], chat_id=owner,
                executor_prefix='budget_watch', task_text=json.dumps({'source': 'bank', 'revision': row['revision']}),
                conversation_id=None, task_id=identity, resolve_bot=True)
        except DuplicateTaskId: pass
        with store.db() as db:
            db.execute('UPDATE budget_bank SET next_scan=? WHERE owner=? AND revision=? AND next_scan=?',
                (now + row['private']['interval_seconds'], owner, row['revision'], row['next_scan']))


def run_scan(ctx, owner, text, task_id, send, *, on_result=None, on_progress=None):
    store = BankStore()
    revision = json.loads(text)['revision']
    def active():
        if store.tasks.is_cancel_requested(task_id): raise provider.BankError('Bank scan was cancelled.')
        row = store.valid_bank(owner, revision)
        task = store.tasks.get(task_id) or {}
        if (task.get('status') != 'running' or task.get('chat_id') != owner or task.get('bot_id') != row['bot_id']
                or task.get('task_text') != text or task.get('executor_prefix') != 'budget_watch'
                or ctx.bot_owner != owner or ctx.bot_id != row['bot_id']):
            raise provider.BankError('Bank scan task binding is unavailable.')
    try:
        active()
        if on_progress: on_progress({'message': 'Updating private bank budget totals.'})
        count = store.sync(owner, revision, active)
    except Exception:
        with store.db() as db:
            db.execute('UPDATE budget_bank SET detail=?,next_scan=? WHERE owner=? AND revision=?',
                ('Bank sync could not finish. Saved totals may be incomplete or stale. Check bank settings.', time.time() + 3600, owner, revision))
        raise provider.BankError('Bank sync could not finish. Check bank settings.') from None
    result = {'mode': 'budget_watch', 'status': 'no_changes', 'count': count,
              'final_response': 'Bank budget sync completed. Private category totals are available in Budget settings.'}
    if on_result: on_result(result)
    send(owner, result['final_response'])
