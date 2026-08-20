#!/usr/bin/env python3
"""Quick check: Tests 9 and 10 only."""
import os
os.environ.setdefault('TELEGRAM_BOT_TOKEN','test-token')
os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_ID','12345')
os.environ.setdefault('KYREX_TASK_TIMEOUT','30')
os.environ.setdefault('KYREX_APPROVAL_TIMEOUT','3')

import sys
sys.path.insert(0, '.')
import telegram_bot as tb

CHAT = 12345
failures = []
launched = []

def check(name, cond, detail=''):
    if cond:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name} {detail}')
        failures.append(name)

tb.send_message = lambda chat_id, text: None
tb.edit_message = lambda *a, **k: None
tb.launch = lambda chat_id, repo_url, task_text: launched.append(task_text)

print('Test 9: real task sent as reply-to must be launched')
launched.clear()
tb.pending_approvals.clear()
tb.handle_message({'chat': {'id': CHAT}, 'text': 'fix the parser',
                   'message_id': 7001,
                   'reply_to_message': {'message_id': 4242}})
check('reply-to task launched', launched == ['fix the parser'],
      f'launched={launched!r}')

print('Test 10: non-approval msg not eaten by pending approval')
import threading
launched.clear()
tb.pending_approvals.clear()
evt = threading.Event()
tb.pending_approvals[5555] = {'event': evt, 'chat_id': CHAT, 'tier': 1,
                              'token': '', 'result': None}
tb.handle_message({'chat': {'id': CHAT}, 'text': 'add a changelog entry',
                   'message_id': 7002})
check('unrelated text did not silently deny', not evt.is_set(),
      'consumed as T1 denial')
check('approval still pending', 5555 in tb.pending_approvals)
tb.pending_approvals.clear()

print()
print('ALL TESTS PASSED' if not failures else f'{len(failures)} FAILURE(S): {failures}')
sys.exit(1 if failures else 0)