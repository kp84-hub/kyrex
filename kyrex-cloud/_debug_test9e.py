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

# Patch globals directly
import telegram_bot
telegram_bot.launch = lambda chat_id, repo_url, task_text: launched.append(task_text)
telegram_bot.send_message = lambda chat_id, text: None
telegram_bot.edit_message = lambda *a, **k: None

# Verify
print("tb.launch is telegram_bot.launch:", tb.launch is telegram_bot.launch)

tb.pending_approvals.clear()
msg = {'chat': {'id': CHAT}, 'text': 'fix the parser',
       'message_id': 7001,
       'reply_to_message': {'message_id': 4242}}

# Print handle_approval_reply result
print("handle_approval_reply result:", tb.handle_approval_reply(msg))
print("launched after approval check:", launched)

print("\nNow calling handle_message...")
tb.handle_message(msg)
print("launched:", launched)