#!/usr/bin/env python3
import os
os.environ.setdefault('TELEGRAM_BOT_TOKEN','test-token')
os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_ID','12345')
os.environ.setdefault('KYREX_TASK_TIMEOUT','30')
os.environ.setdefault('KYREX_APPROVAL_TIMEOUT','3')

import sys
sys.path.insert(0, '.')
import telegram_bot as tb

CHAT = 12345
launched = []
_calls = []

# Store original
orig_launch = tb.launch

# Custom launch that records calls AND calls the original for side effects
def tracking_launch(chat_id, repo_url, task_text):
    global _calls
    _calls.append(task_text)
    launched.append(task_text)
    # Don't call original — it would try to acquire busy lock and spawn a thread
    return True

tb.send_message = lambda chat_id, text: None
tb.edit_message = lambda *a, **k: None
tb.launch = tracking_launch

print("launch func id:", id(tb.launch))
print("launch in __dict__ matches:", tb.__dict__.get('launch') is tracking_launch)

tb.pending_approvals.clear()
msg = {'chat': {'id': CHAT}, 'text': 'fix the parser',
       'message_id': 7001,
       'reply_to_message': {'message_id': 4242}}

print("\nCalling handle_message...")
tb.handle_message(msg)
print("launched:", launched)
print("_calls:", _calls)