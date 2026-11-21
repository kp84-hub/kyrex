"""Private cash-flow plans on the existing encrypted budget store."""
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import re
import uuid
from zoneinfo import ZoneInfo

import budget_bank
from budget_watch import eligible
import connectors

HORIZON = 30
FREQUENCIES = {'once', 'weekly', 'biweekly', 'monthly'}
_NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?'
AMOUNT = rf'[+-]?(?:\$ *{_NUMBER}|{_NUMBER}(?: *\$)?)'
COMMAND_GUIDE = ('Include the item, amount, and due date. Try '
                 '“Add car insurance $125 on Nov. 24” or '
                 '“Internet is $80 on the 25th every month.”')
_DAY_NAMES = tuple(calendar.day_name)
_DATE_TEXT = r'\d{4}-\d{2}-\d{2}|today|tomorrow|(?:(?:this|next) )?(?:' + '|'.join(_DAY_NAMES) + ')'
_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTHS['sept'] = 9
_COMMAND_QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’'}
_MONTH_DATE = re.compile(
    rf'(?P<month>{"|".join(_MONTHS)})\.? (?P<day>\d{{1,2}})(?:st|nd|rd|th)?'
    r'(?:,? (?P<year>\d{4}))?', re.I)
_ENTRY = re.compile(
    rf'(?:add (?P<added>[^$]{{1,80}}?)|(?P<named>[^$]{{1,80}}?) (?:is|will be)) '
    rf'(?P<amount>{AMOUNT}) (?:on )?(?P<date>{_DATE_TEXT}|{_MONTH_DATE.pattern}|the \d{{1,2}}(?:st|nd|rd|th))'
    r'(?: every (?P<repeat>week|two weeks|month))?', re.I)
_BALANCE = re.compile(rf'(?:set )?(?:my )?starting balance (?:is |to )?({AMOUNT})(?: (?:on )?({_DATE_TEXT}))?', re.I)
_SETTLE = re.compile(r'mark (.{1,80}) (?:paid|received|settled)', re.I)
_CHANGE_AMOUNT = re.compile(rf'(?:change|update) (.{{1,80}}) to ({AMOUNT})', re.I)
_SHOW = re.compile(r'(?:(?:(?:show|open|view|pull up) (?:me )?(?:my |the )?)?'
                   r'(?:budget )?worksheet(?: for (?:this month|the next 30 days))?|'
                   r'how much (?:do|will) i have (?:left )?(?:until|before) (?:my next )?payday)', re.I)


def _day(value):
    return date.fromisoformat(budget_bank.day(value))


def _amount(value):
    if not isinstance(value, str) or len(value) > 24 or not re.fullmatch(AMOUNT, value):
        raise ValueError('Enter an amount with up to two decimal places.')
    amount = int(Decimal(value.replace('$', '').replace(',', '').replace(' ', '')) * 100)
    if abs(amount) > 10**9:
        raise ValueError('Enter a smaller amount.')
    return amount


def _title(value):
    title = ' '.join(value.split()) if isinstance(value, str) else ''
    if not title or len(title) > 80 or any(ord(c) < 32 for c in title):
        raise ValueError('Enter an item name up to 80 characters.')
    return title


def _monthly(first, index, day):
    year, month = divmod(first.year * 12 + first.month - 1 + index, 12)
    return date(year, month + 1, min(day, calendar.monthrange(year, month + 1)[1]))


def _dates(row, end):
    first = _day(row['date'])
    repeat = row['repeat']
    current, index = first, 0
    while current <= end:
        yield current
        if repeat == 'once':
            break
        index += 1
        current = (_monthly(first, index, row.get('month_day', first.day)) if repeat == 'monthly'
                   else first + timedelta(days=index * (7 if repeat == 'weekly' else 14)))


def _pending(row, end):
    if row['active']:
        yield from (due for due in _dates(row, end) if due.isoformat() not in row['settled'])


def _next_due(row, today):
    if not row['active']:
        return None
    first = _day(row['date'])
    last = max(row['settled'], default=row['date'])
    end = max(today, first, _day(last)) + timedelta(days=31)
    return next(_pending(row, end), None)


def _signed(row):
    return row['amount_minor'] if row['kind'] == 'income' else -row['amount_minor']


def project(plan, today, bank=None):
    """Return owner-facing monetary arithmetic in integer cents."""
    end = today + timedelta(days=HORIZON - 1)
    manual = plan['balance']
    rows = plan['rows']
    balance = None
    if manual:
        balance = manual['amount_minor'] + sum(
            item['amount_minor'] for row in rows.values() for item in row['settled'].values()
            if manual['date'] <= item['date'] <= today.isoformat())
        source = 'manual'
    elif bank and bank['cash_estimate']['currency'] == plan['currency']:
        balance = bank['cash_estimate']['estimated_minor']
        source = 'bank'
    else:
        source = 'none'

    by_date = {}
    for row in rows.values():
        for due in _pending(row, end):
            if due < today and row['kind'] == 'income':
                continue
            totals = by_date.setdefault(max(due, today), {'income': 0, 'expense': 0})
            totals[row['kind']] += row['amount_minor']
    current = running = low = balance
    low_date = today if low is not None else None
    payday_date, before_payday = None, None
    chart = []
    for i in range(HORIZON):
        day = today + timedelta(days=i)
        totals = by_date.get(day, {'income': 0, 'expense': 0})
        after_bills = running - totals['expense'] if running is not None else None
        if after_bills is not None and after_bills < low:
            low, low_date = after_bills, day
        if totals['income'] and payday_date is None:
            payday_date, before_payday = day, after_bills
        running = after_bills + totals['income'] if after_bills is not None else None
        chart.append({'date': day.isoformat(), 'balance_minor': running, **totals})
    return {'currency': plan['currency'], 'today': today.isoformat(),
        'balance': manual, 'source': source, 'current_minor': current,
        'next_income': payday_date.isoformat() if payday_date else None,
        'before_income_minor': before_payday, 'low_minor': low,
        'low_date': low_date.isoformat() if low_date else None,
        'bank_last_scan': bank['last_scan'] if source == 'bank' else None,
        'bank_stale': (not bank['latest_attempt_ok'] or not bank['history_loaded']) if source == 'bank' else False,
        'bank_environment': bank['environment'] if source == 'bank' else None,
        'chart': chart, 'rows': [_display_row(key, row, today) for key, row in rows.items()]}


def _display_row(key, row, today):
    due = _next_due(row, today)
    last = max(row['settled'], key=lambda date: (row['settled'][date]['date'], date), default=None)
    return {**{name: row[name] for name in ('title', 'amount_minor', 'kind', 'date', 'repeat', 'active')},
            'id': key, 'next_due': due.isoformat() if due else None, 'last_settled': last}


class WorksheetStore(budget_bank.BankStore):
    def __init__(self, tasks):
        super().__init__(tasks)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS budget_worksheets (
                    owner TEXT PRIMARY KEY, revision TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS budget_worksheet_turns (
                    owner TEXT NOT NULL, identity TEXT NOT NULL, reply TEXT NOT NULL,
                    PRIMARY KEY(owner, identity));
            ''')

    def binding(self, owner):
        bank = self.get(owner)
        private = bool(bank and bank['private'].get('private_mode'))
        settings = bank['private'] if private else self.settings(owner)
        bot_id = bank['bot_id'] if private else settings.get('bot_id')
        return bot_id, settings, private

    def context(self, owner, conv=None):
        """Require the owner's configured Budget Bot and recheck its access."""
        bot_id, settings, private = self.binding(owner)
        if not bot_id:
            raise LookupError('Configure Budget Bot in Settings to open its worksheet.')
        if conv is not None and (bot_id != conv.get('bot_id') or conv.get('workspace_id')):
            raise LookupError('Open your Budget Bot conversation to use the worksheet.')
        eligible(owner, bot_id, require_running=False, email=False)
        currency = settings.get('currency') or settings.get('dollar_currency', 'USD')
        zone = settings.get('time_zone')
        if not zone:
            with self.db() as db:
                _, plan = self._plan(db, owner, currency)
            zone = plan['time_zone']
        return datetime.now(ZoneInfo(zone)).date(), currency, private, zone

    @staticmethod
    def _plan(db, owner, currency):
        row = db.execute('SELECT revision,data FROM budget_worksheets WHERE owner=?', (owner,)).fetchone()
        if not row:
            return '', {'currency': currency, 'balance': None, 'rows': {}, 'time_zone': 'UTC'}
        plan = connectors.unseal_tokens(row['data'])
        if not isinstance(plan, dict) or set(plan) != {'currency', 'balance', 'rows', 'time_zone'}:
            raise ValueError('Worksheet storage is unavailable.')
        return row['revision'], plan

    def view(self, owner, conv=None):
        today, currency, private, _ = self.context(owner, conv)
        with self.db() as db:
            revision, plan = self._plan(db, owner, currency)
        bank = None
        if private and not plan['balance']:
            try:
                snapshot = self.model_view(owner)
                if snapshot.get('last_scan') and snapshot.get('cash_estimate'):
                    bank = snapshot
            except budget_bank.provider.BankError:
                # The worksheet remains usable with a manual balance.
                pass
        return {**project(plan, today, bank), 'revision': revision}

    def mutate(self, owner, conv, actions, *, revision=None, request_id=None):
        today, currency, _, zone = self.context(owner, conv)
        # A replay of a chat turn never settles the next recurring occurrence.
        identity = hashlib.sha256(str(request_id).encode()).hexdigest() if request_id else None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if identity:
                previous = db.execute('SELECT reply FROM budget_worksheet_turns WHERE owner=? AND identity=?',
                                      (owner, identity)).fetchone()
                if previous:
                    return previous['reply']
            current, plan = self._plan(db, owner, currency)
            if revision is not None and revision != current:
                raise ValueError('The worksheet changed. Reload it and retry your edit.')
            plan['time_zone'] = zone
            for action in actions:
                reply = _change(plan, today, action)
            if len(actions) > 1:
                reply = f'Added {len(actions)} rows to your budget worksheet.'
            db.execute('INSERT INTO budget_worksheets(owner,revision,data) VALUES (?,?,?) '
                       'ON CONFLICT(owner) DO UPDATE SET revision=excluded.revision,data=excluded.data',
                       (owner, uuid.uuid4().hex, connectors.seal_tokens(plan)))
            if identity:
                db.execute('INSERT INTO budget_worksheet_turns(owner,identity,reply) VALUES (?,?,?)',
                           (owner, identity, reply))
        return reply


def _change(plan, today, action):
    op = action['op']
    if op == 'balance':
        starting = _day(action['date'])
        if starting > today:
            raise ValueError('The balance date must be today or earlier.')
        plan['balance'] = {'date': starting.isoformat(), 'amount_minor': _amount(action['amount'])}
        return 'Saved your starting balance in the worksheet.'
    if op == 'clear_balance':
        plan['balance'] = None
        return 'Cleared the manual starting balance.'
    rows = plan['rows']
    key = action.get('id')
    if op == 'save':
        if key and key not in rows:
            raise ValueError('The worksheet row is unavailable.')
        title = _title(action['title'])
        due = _day(action['date'])
        if not date(1900, 1, 1) <= due <= today + timedelta(days=365 * 5):
            raise ValueError('Choose a due date within the next five years.')
        repeat, kind = action['repeat'], action['kind']
        amount = _amount(action['amount'])
        if amount <= 0 or repeat not in FREQUENCIES or kind not in {'income', 'expense'}:
            raise ValueError('Choose income or a bill, a frequency, and a positive amount.')
        if not key and len(rows) >= 150:
            raise ValueError('The worksheet supports up to 150 rows.')
        previous = rows.get(key, {'settled': {}, 'active': True})
        month_day = action.get('month_day', due.day)
        if previous.get('date') == due.isoformat() and previous.get('repeat') == repeat:
            month_day = previous.get('month_day', month_day)
        rows[key or uuid.uuid4().hex] = {**previous, 'title': title, 'date': due.isoformat(),
            'repeat': repeat, 'kind': kind, 'amount_minor': amount, 'month_day': month_day}
        return 'Saved the row in your budget worksheet.'
    if op in {'settle', 'change_amount'} and not key:
        matches = [item for item, row in rows.items() if row['active']
                   and row['title'].casefold() == _title(action['title']).casefold()
                   and _next_due(row, today)]
        if len(matches) != 1:
            raise ValueError('Choose the exact row in the worksheet.')
        key = matches[0]
    row = rows.get(key)
    if not row:
        raise ValueError('The worksheet row is unavailable.')
    if op == 'change_amount':
        amount = _amount(action['amount'])
        if amount <= 0:
            raise ValueError('Enter a positive amount.')
        row['amount_minor'] = amount
        return 'Updated the amount for unsettled dates in the worksheet.'
    if op == 'settle':
        due = _next_due(row, today)
        if not due or (action.get('date') and due.isoformat() != action['date']):
            raise ValueError('This due date has changed. Reload the worksheet.')
        row['settled'][due.isoformat()] = {'date': today.isoformat(), 'amount_minor': _signed(row)}
        return 'Marked the worksheet row settled.'
    if op == 'undo':
        if action['date'] not in row['settled']:
            raise ValueError('This settlement has already changed.')
        del row['settled'][action['date']]
        return 'Restored the unsettled row.'
    if op in {'stop', 'resume'}:
        row['active'] = op == 'resume'
        return 'Updated the row’s schedule.'
    raise ValueError('Choose a worksheet action.')


def _spoken_date(value, today, repeat='once'):
    value = value.lower()
    if value == 'today':
        return today
    if value == 'tomorrow':
        return today + timedelta(days=1)
    if match := _MONTH_DATE.fullmatch(value):
        month, day = _MONTHS[match['month']], int(match['day'])
        explicit_year = match['year']
        if explicit_year:
            years = (int(explicit_year),)
        else:
            # Eight years covers the gap between leap years across a century.
            years = range(today.year, min(today.year + 8, 9999) + 1)
        for year in years:
            try:
                due = date(year, month, day)
            except ValueError:
                continue
            if explicit_year or due >= today:
                return due
        raise ValueError('Choose a valid due date.')
    if value.startswith('the ') and repeat == 'monthly':
        day = int(re.match(r'the (\d+)', value)[1])
        if not 1 <= day <= 31:
            raise ValueError('Choose a day from 1 to 31.')
        first = today.replace(day=1)
        current = _monthly(first, 0, day)
        return current if current >= today else _monthly(first, 1, day)
    weekday = value.rsplit(' ', 1)[-1].title()
    if weekday in _DAY_NAMES:
        offset = (_DAY_NAMES.index(weekday) - today.weekday()) % 7
        return today + timedelta(days=(offset or 7) if value.startswith('next ') else offset)
    return _day(value)


def command_text(text):
    """Accept a command copied with quotation marks and keep its punctuation."""
    text = text.strip()
    closing = _COMMAND_QUOTES.get(text[:1])
    if not closing:
        return text
    text = text[1:].lstrip()
    body, quote, suffix = text.rpartition(closing)
    if quote and not suffix.strip(' .!?\t\r\n'):
        return body.rstrip() + suffix.strip()
    return text


def command(text, today):
    """Recognize worksheet commands without sending financial fields to a model."""
    text = command_text(text)
    statement = text.rstrip('.!?').strip()
    if _SHOW.fullmatch(statement):
        return 'show', []
    if text.endswith('?'):
        return None, []
    if match := _BALANCE.fullmatch(statement):
        amount, day = match.groups()
        return 'write', [{'op': 'balance', 'amount': amount,
                          'date': _spoken_date(day, today).isoformat() if day else today.isoformat()}]
    if match := _SETTLE.fullmatch(statement):
        return 'write', [{'op': 'settle', 'title': match[1]}]
    if match := _CHANGE_AMOUNT.fullmatch(statement):
        return 'write', [{'op': 'change_amount', 'title': match[1], 'amount': match[2]}]
    single = _ENTRY.fullmatch(statement)
    parts = re.split(r',?\s+and\s+|;\s*|\n+', statement, maxsplit=8)
    matches = [single] if single else [_ENTRY.fullmatch(part.strip()) for part in parts]
    if all(matches):
        rows = []
        for match in matches:
            title = match['added'] or match['named']
            title, _, explicit = title.partition(' as ')
            income = explicit.lower() == 'income' or bool(re.search(r'\b(payday|paycheck|salary|income)\b', title, re.I))
            repeat = {None: 'once', 'week': 'weekly', 'two weeks': 'biweekly',
                      'month': 'monthly'}[match['repeat'].lower() if match['repeat'] else None]
            row = {'op': 'save', 'title': title, 'kind': 'income' if income else 'expense',
                'amount': match['amount'], 'date': _spoken_date(match['date'], today, repeat).isoformat(),
                'repeat': repeat}
            if match['date'].lower().startswith('the '):
                row['month_day'] = int(re.match(r'the (\d+)', match['date'], re.I)[1])
            rows.append(row)
        return 'write', rows
    if any(matches) or re.match(r'(?:add|edit|update) (?:.*\bworksheet\b)', statement, re.I):
        return 'guide', []
    if re.match(r'add (?:.*\d *\$|.*\$ *\d|(?:bill|expense|income)\b)', statement, re.I):
        return 'guide', []
    return None, []
