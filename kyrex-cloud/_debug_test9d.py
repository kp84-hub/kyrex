#!/usr/bin/env python3
import os
os.environ.setdefault('TELEGRAM_BOT_TOKEN','test-token')
os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_ID','12345')
os.environ.setdefault('KYREX_TASK_TIMEOUT','30')
os.environ.setdefault('KYREX_APPROVAL_TIMEOUT','3')

import sys
sys.path.insert(0, '.')

# Patch BEFORE import
import telegram_bot as tb

CHAT = 12345
launched = []
calls = []

# Instrument handle_message
orig_handle = tb.handle_message

def debug_handle(msg):
    print(f"[handle_message] text={msg.get('text')!r}, reply_to={msg.get('reply_to_message')}")
    ar = tb.handle_approval_reply(msg)
    print(f"[handle_message] handle_approval_reply returned: {ar}")
    if ar:
        print("[handle_message] consumed as approval reply, returning early")
        return
    text = msg.get("text", "")
    print(f"[handle_message] text after approval check: {text!r}")
    print(f"[handle_message] about to call launch")
    # Let original handle the rest
    # But we can't because it would call the real launch
    
    # Manually do the rest
    chat_id = msg.get("chat", {}).get("id")
    if not text:
        print("[handle_message] no text")
        return
    stripped = text.strip()
    if stripped in ("/status", "/repos"):
        print(f"[handle_message] command: {stripped}")
        return
    repo_url, task_text = tb.resolve_repo(text)
    print(f"[handle_message] calling launch with {task_text!r}")
    tb.launch(chat_id, repo_url, task_text)

tb.handle_message = debug_handle
tb.launch = lambda chat_id, repo_url, task_text: calls.append(task_text) or launched.append(task_text)
tb.send_message = lambda chat_id, text: None
tb.edit_message = lambda *a, **k: None

tb.pending_approvals.clear()
msg = {'chat': {'id': CHAT}, 'text': 'fix the parser',
       'message_id': 7001,
       'reply_to_message': {'message_id': 4242}}

print("Calling handle_message...")
tb.handle_message(msg)
print(f"launched={launched}")
print(f"calls={calls}")