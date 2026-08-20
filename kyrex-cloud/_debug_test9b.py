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

tb.send_message = lambda chat_id, text: (_ := None) or print(f"[send_message] {text[:80]}")
tb.edit_message = lambda *a, **k: None
tb.launch = lambda chat_id, repo_url, task_text: launched.append(task_text) or print(f"[launch] task={task_text!r}")

print("=== Bypassing handle_message, directly calling the launch logic ===")
tb.pending_approvals.clear()
msg = {'chat': {'id': CHAT}, 'text': 'fix the parser',
       'message_id': 7001,
       'reply_to_message': {'message_id': 4242}}

# Simulate what handle_message does after handle_approval_reply
chat_id = msg.get("chat", {}).get("id")
text = msg.get("text", "")
print(f"chat_id={chat_id}, text={text!r}")
print(f"handle_approval_reply returns: {tb.handle_approval_reply(msg)}")

if not text:
    print("no text, returning")
else:
    stripped = text.strip()
    print(f"stripped={stripped!r}")
    if stripped == "/status":
        print("status command")
    elif stripped == "/repos":
        print("repos command")
    else:
        repo_url, task_text = tb.resolve_repo(text)
        print(f"resolve_repo: repo_url={repo_url!r}, task_text={task_text!r}")
        if not task_text:
            print("no task text")
        else:
            pending = tb.take_pending_docs(chat_id)
            print(f"pending docs: {pending}")
            if pending:
                task_text = tb.build_task_with_attachments(task_text, pending)
            print(f"Calling launch with {task_text!r}")
            tb.launch(chat_id, repo_url, task_text)

print(f"launched={launched!r}")