#!/usr/bin/env python3
"""Quick debug: run the first test from test_approval_protocol.py."""
import os
import sys

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")
os.environ.setdefault("KYREX_APPROVAL_TIMEOUT", "3")

sys.path.insert(0, "kyrex-cloud")
import serve
import telegram_bot as tb

CHAT = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
HERE = "kyrex-cloud"

tb.send_message = lambda chat_id, text: 1001
tb.edit_message = lambda *a, **k: None
tb.launch = lambda *a, **kw: None

real_popen = tb.subprocess.Popen

APPROVING_EXECUTOR = '''
import sys, json
req = {"tier": 1, "summary": "test op", "token": ""}
sys.stdout.write("KYREX_APPROVAL:" + json.dumps(req) + "\\n")
sys.stdout.flush()
decision = sys.stdin.readline().strip()
sys.stdout.write("KYREX_RESULT_JSON:" + json.dumps(
    {"status": "no_changes", "final_response": "decision=" + decision}) + "\\n")
sys.stdout.flush()
'''

path = os.path.join(HERE, "_debug_approver.py")
with open(path, "w") as f:
    f.write(APPROVING_EXECUTOR)

tb.subprocess.Popen = lambda cmd, **kw: real_popen([sys.executable, path], **kw)

serve.session_lock(CHAT).acquire()
print("Starting task...", flush=True)
tb.run_task(CHAT, "repo", "task")
print("Task finished!", flush=True)

tb.subprocess.Popen = real_popen

if os.path.exists(path):
    os.remove(path)