"""Minimal reproduction of an approval protocol test scenario."""
import os, sys, threading, time, json
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_TASK_TIMEOUT", "30")
os.environ.setdefault("KYREX_APPROVAL_TIMEOUT", "3")
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import telegram_bot as tb

CHAT = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])

sent = []
_next = [1000]

def fake_send(chat_id, text):
    _next[0] += 1
    sent.append({"id": _next[0], "text": text})
    return _next[0]

tb.send_message = fake_send
tb.edit_message = lambda *a, **k: None
tb.launch = lambda *a, **kw: None

# Write an approver script
HERE = os.path.dirname(os.path.abspath(__file__))
script_path = os.path.join(HERE, "_test_approver.py")
lines = []
lines.append("import sys, json")
lines.append("req = {'tier': 1, 'summary': 'test op', 'token': ''}")
lines.append("sys.stdout.write('KYREX_APPROVAL:' + json.dumps(req) + '\\\\n')")
lines.append("sys.stdout.flush()")
lines.append("decision = sys.stdin.readline().strip()")
lines.append("sys.stdout.write('KYREX_RESULT_JSON:' + json.dumps(")
lines.append("    {'status': 'no_changes', 'final_response': 'decision=' + decision}) + '\\\\n')")
lines.append("sys.stdout.flush()")
with open(script_path, "w") as f:
    f.write("\n".join(lines) + "\n")

real_popen = tb.subprocess.Popen
tb.subprocess.Popen = lambda cmd, **kw: real_popen([sys.executable, script_path], **kw)

def reply():
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if tb.pending_approvals:
            break
        time.sleep(0.05)
    else:
        print("TIMEOUT waiting for approval to be registered")
        return
    approval_id = next(iter(tb.pending_approvals))
    time.sleep(0.3)
    msg = {"chat": {"id": CHAT}, "text": "y", "message_id": 9999,
           "reply_to_message": {"message_id": approval_id}}
    tb.handle_message(msg)

tb.pending_approvals.clear()
sent.clear()
tb.busy_lock.acquire()

threading.Thread(target=reply, daemon=True).start()
t0 = time.monotonic()
tb.run_task(CHAT, "https://example.com/repo.git", "test task")
elapsed = time.monotonic() - t0

tb.subprocess.Popen = real_popen

# Check results
decision = None
for m in sent:
    if "decision=" in m["text"]:
        decision = m["text"].split("decision=")[1].split()[0].strip()
print(f"Elapsed: {elapsed:.2f}s")
print(f"Decision seen: {decision}")
print(f"Lock released: {not tb.busy_lock.locked()}")
print("PASS" if decision == "APPROVED" else "FAIL")

# Cleanup
if os.path.exists(script_path):
    os.remove(script_path)