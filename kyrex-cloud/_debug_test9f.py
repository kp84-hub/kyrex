#!/usr/bin/env python3
import os
os.environ.setdefault('TELEGRAM_BOT_TOKEN','test-token')
os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_ID','12345')
os.environ.setdefault('KYREX_TASK_TIMEOUT','30')
os.environ.setdefault('KYREX_APPROVAL_TIMEOUT','3')

import sys
sys.path.insert(0, '.')
import telegram_bot as tb
import dis

# Check what handle_message actually references
print("=== handle_message bytecode (launch ref) ===")
for instr in dis.get_instructions(tb.handle_message):
    if 'launch' in str(instr.argrepr):
        print(f"  {instr.opname} {instr.argrepr}")

CHAT = 12345
launched = []

# Patch module globals
import telegram_bot
telegram_bot.launch = lambda chat_id, repo_url, task_text: launched.append(task_text)
telegram_bot.send_message = lambda c, t: None

print("\nlaunch is now:", telegram_bot.launch)
print("handle_message.__globals__['launch'] is telegram_bot.launch:", 
      tb.handle_message.__globals__['launch'] is telegram_bot.launch)

tb.pending_approvals.clear()
msg = {'chat': {'id': CHAT}, 'text': 'fix the parser',
       'message_id': 7001,
       'reply_to_message': {'message_id': 4242}}

# Check what getattr does
print("\nhandle_approval_reply result:", tb.handle_approval_reply(msg))

# Now manually check what handle_message would do
print("\nManual trace:")
chat_id = msg.get("chat", {}).get("id")
text = msg.get("text", "")
print(f"  chat_id={chat_id}, text={text!r}")
repo_url, task_text = tb.resolve_repo(text)
print(f"  repo_url={repo_url!r}, task_text={task_text!r}")
print(f"  calling launch...")
telegram_bot.launch(chat_id, repo_url, task_text)
print(f"  launched={launched}")

# Also check if there's an exception issue
print("\nCalling handle_message directly (with try/except)...")
launched.clear()
try:
    tb.handle_message(msg)
except Exception as e:
    print(f"  EXCEPTION: {type(e).__name__}: {e}")
print(f"  launched={launched}")