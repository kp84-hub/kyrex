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

tb.send_message = lambda chat_id, text: None
tb.edit_message = lambda *a, **k: None

# Monkey-patch launch
original_launch = tb.launch
tb.launch = lambda chat_id, repo_url, task_text: launched.append(task_text)

print("launch before:", tb.launch)
print("launch in globals:", tb.__dict__.get('launch'))

tb.pending_approvals.clear()
msg = {'chat': {'id': CHAT}, 'text': 'fix the parser',
       'message_id': 7001,
       'reply_to_message': {'message_id': 4242}}
print("Calling handle_message...")
result = tb.handle_approval_reply(msg)
print("handle_approval_reply returned:", result)
print("launched after handle_approval_reply:", launched)

# Call handle_message
tb.handle_message(msg)
print("launched after handle_message:", launched)