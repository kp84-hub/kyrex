"""Verify that telegram_bot.py references serve.py's symbols correctly."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'kyrex-cloud'))

import serve as s
import telegram_bot as tb

# Check telegram_bot references serve's symbols
assert tb.EXECUTORS is s.EXECUTORS, 'EXECUTORS not same object'
assert tb.DEFAULT_EXECUTOR is s.DEFAULT_EXECUTOR, 'DEFAULT_EXECUTOR not same'
assert tb.EXECUTOR_PREFIX_RE is s.EXECUTOR_PREFIX_RE, 'EXECUTOR_PREFIX_RE not same'
assert tb.REPO_ALIASES is s.REPO_ALIASES, 'REPO_ALIASES not same'
assert tb.resolve_executor is s.resolve_executor, 'resolve_executor not same'

# Check function behavior is identical
assert s.resolve_executor('repo: fix it') == ('repo', 'fix it', None)
assert s.resolve_executor('no prefix') == ('repo', 'no prefix', None)
assert s.resolve_executor('bogus: x') == (None, None, 'bogus')

print('All assertions passed.')