"""Kbot's short-lived, one-time handoffs in the existing account store.

Use the account database's cross-process atomic consumption and expiry, and
the existing connector encryption. No raw transaction/code/session bearer is
stored; only its keyed digest is used as the database lookup key. This is not
a new long-lived desktop token or refresh-session implementation.
"""
import re

import account_store as accounts
from connectors import seal_tokens, unseal_tokens


def prefix(kind):
    return 'kbot-' + kind + '.'


def matches(token, kind):
    return isinstance(token, str) and token.startswith(prefix(kind))


def issue(kind, binding, value, ttl):
    token = accounts.transaction('kbot-' + kind, binding,
                                 {'sealed': seal_tokens(value)}, ttl=ttl)
    return prefix(kind) + token


def consume(token, kind, binding):
    if not matches(token, kind):
        raise accounts.AccountError('Start sign-in again from Kbot.')
    raw = token.removeprefix(prefix(kind))
    if not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
        raise accounts.AccountError('Start sign-in again from Kbot.')
    saved = accounts.consume(raw, 'kbot-' + kind, binding)
    value = unseal_tokens(saved.get('sealed'))
    if not value:
        raise accounts.AccountError('Start sign-in again from Kbot.')
    return value
