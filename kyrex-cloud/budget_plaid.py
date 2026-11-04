"""Narrow Plaid protocol adapter. No model, payment or identity endpoints."""
from decimal import Decimal, InvalidOperation
import hashlib
import os
from urllib.parse import urlsplit

import httpx


class BankError(ValueError):
    def __init__(self, message, code='unavailable'):
        super().__init__(message)
        self.code = code


ENDPOINTS = frozenset({'/link/token/create', '/link/token/get',
    '/item/public_token/exchange', '/accounts/get', '/transactions/sync', '/item/remove'})
CURRENCIES = frozenset({'USD', 'CAD', 'EUR', 'GBP', 'AUD'})
CATEGORIES = frozenset({'groceries', 'dining', 'shopping', 'transport', 'home',
    'health', 'entertainment', 'education', 'travel', 'income', 'transfer',
    'debt_payment', 'other'})
PRIMARY = {'FOOD_AND_DRINK': 'dining', 'GENERAL_MERCHANDISE': 'shopping',
    'TRANSPORTATION': 'transport', 'GAS_STATIONS': 'transport',
    'HOME_IMPROVEMENT': 'home', 'RENT_AND_UTILITIES': 'home',
    'MEDICAL': 'health', 'PERSONAL_CARE': 'health', 'ENTERTAINMENT': 'entertainment',
    'TRAVEL': 'travel', 'INCOME': 'income', 'TRANSFER_IN': 'transfer',
    'TRANSFER_OUT': 'transfer', 'LOAN_PAYMENTS': 'debt_payment'}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def minor(value):
    if isinstance(value, bool): raise BankError('A bank amount needs review.')
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value() or abs(amount) > 10**13:
            raise ValueError()
        return int(amount)
    except (ValueError, TypeError, InvalidOperation):
        raise BankError('A bank amount needs review.') from None


class Plaid:
    def __init__(self):
        self.environment = os.environ.get('KYREX_PLAID_ENV', 'sandbox')
        self.client_id = os.environ.get('KYREX_PLAID_CLIENT_ID', '')
        self.secret = os.environ.get('KYREX_PLAID_SECRET', '')
        self.return_url = os.environ.get('KYREX_PLAID_RETURN_URL', '')
        parsed = urlsplit(self.return_url)
        if (self.environment not in {'sandbox', 'production'} or not self.client_id or not self.secret
                or parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment):
            raise BankError('Bank setup requires Plaid credentials and the HTTPS Kyrex home URL in deployment settings.')
        self.return_url = self.return_url.rstrip('/') + '/'
        self.binding = digest(self.environment + ':' + self.client_id)

    def call(self, endpoint, **payload):
        if endpoint not in ENDPOINTS:
            raise BankError('This bank operation is unavailable.')
        try:
            # Fixed origins and paths; neither model nor owner input selects a URL.
            with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
                with client.stream('POST', f'https://{self.environment}.plaid.com{endpoint}',
                        json={'client_id': self.client_id, 'secret': self.secret, **payload},
                        headers={'Plaid-Version': '2020-09-14'}) as response:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 4_000_000: raise BankError('Bank response was too large. No sync changes were saved.')
            import json
            result = json.loads(raw, parse_float=Decimal)
            if not isinstance(result, dict): raise ValueError()
            if response.status_code != 200:
                code = ('pagination_changed' if result.get('error_code') == 'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION'
                        else 'unavailable')
                raise BankError('Bank request could not finish. Check the connection in Budget settings.', code)
            return result
        except BankError: raise
        except Exception:
            # Provider bodies, URLs, tokens and raw exception text never escape.
            raise BankError('Bank request could not finish. Check the connection in Budget settings.') from None

    def start(self, owner, flow):
        result = self.call('/link/token/create', client_name='Kyrex Budget', language='en',
            country_codes=['US', 'CA'], products=['transactions'],
            user={'client_user_id': digest('kyrex-budget:' + owner)},
            account_filters={'depository': {'account_subtypes': ['checking', 'savings']},
                             'credit': {'account_subtypes': ['credit card']}},
            transactions={'days_requested': 30}, enable_multi_item_link=False,
            hosted_link={'completion_redirect_uri': self.return_url + '?settings=budget&bank_link=' + flow,
                         'url_lifetime_seconds': 1800})
        parsed = urlsplit(result.get('hosted_link_url', ''))
        if (parsed.scheme != 'https' or parsed.netloc != 'secure.plaid.com'
                or not parsed.path.startswith('/hl/') or not isinstance(result.get('link_token'), str)):
            raise BankError('Bank sign-in link could not be verified.')
        return {'link_token': result['link_token'], 'url': result['hosted_link_url']}


def accounts(result):
    """Keep protocol IDs encrypted for selection, discard names/masks/balances."""
    rows = result.get('accounts')
    if not isinstance(rows, list) or len(rows) > 100: raise BankError('Bank accounts could not be verified.')
    out = []
    for row in rows:
        kind = row.get('type')
        if kind not in {'depository', 'credit'}: continue
        identity = row.get('account_id')
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            raise BankError('Bank accounts could not be verified.')
        subtype = row.get('subtype')
        if (kind == 'depository' and subtype not in {'checking', 'savings'}) or (kind == 'credit' and subtype != 'credit card'):
            continue
        out.append({'id': identity, 'ref': digest(identity), 'kind': kind,
                    'label': f'{subtype.title()} {len(out) + 1}'})
    if not out or len({r['id'] for r in out}) != len(out):
        raise BankError('No supported checking, savings or credit-card accounts were returned.')
    return out


def transaction(row, selected):
    """Positive Plaid amounts are outflows. No free text survives this boundary."""
    from datetime import date
    if not isinstance(row, dict): raise BankError('Bank transaction could not be verified.')
    account = selected.get(row.get('account_id'))
    if not account: return None
    identity = row.get('transaction_id')
    if not isinstance(identity, str) or not identity or len(identity) > 256:
        raise BankError('Bank transaction could not be verified.')
    day = row.get('date')
    try:
        if date.fromisoformat(day).isoformat() != day: raise ValueError()
    except (ValueError, TypeError): raise BankError('Bank transaction date needs review.') from None
    currency = row.get('iso_currency_code')
    if currency not in CURRENCIES: raise BankError('An unsupported bank currency needs review. Sync has not advanced.')
    if type(row.get('pending')) is not bool: raise BankError('Bank transaction status needs review.')
    category = row.get('personal_finance_category') or {}
    if not isinstance(category, dict): raise BankError('Bank transaction category needs review.')
    code = category.get('primary', '')
    fixed = PRIMARY.get(code, 'other')
    if code == 'FOOD_AND_DRINK' and category.get('detailed') == 'FOOD_AND_DRINK_GROCERIES': fixed = 'groceries'
    return {'identity': digest(identity), 'account': account['ref'], 'kind': account['kind'],
        'date': day, 'amount_minor': minor(row.get('amount')), 'currency': currency,
        'category': fixed, 'pending': row['pending']}
