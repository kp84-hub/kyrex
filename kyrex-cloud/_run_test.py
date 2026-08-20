"""Run test 1 only and capture exact error."""
import sys, os, json, traceback
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_ID', '12345')
os.environ.setdefault('KYREX_TASK_TIMEOUT', '30')
os.environ.setdefault('KYREX_APPROVAL_TIMEOUT', '3')
sys.path.insert(0, '.')
import telegram_bot as tb

CHAT = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
HERE = os.path.dirname(os.path.abspath(__file__))
sent = []
launched = []
_next_msg_id = [1000]

def fake_send(chat_id, text):
    _next_msg_id[0] += 1
    sent.append({"id": _next_msg_id[0], "text": text})
    return _next_msg_id[0]

tb.send_message = fake_send
tb.edit_message = lambda *a, **k: None

# Record any task launch
tb.launch = lambda chat_id, repo_url, task_text: launched.append(task_text)

real_popen = tb.subprocess.Popen

# Try calling launch
print("Calling launch with 3 args...")
try:
    tb.launch(CHAT, "repo", "task")
    print(f"  OK, launched={launched}")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")
    traceback.print_exc()

# Now try calling it the way a test that passes executor_prefix would
print("\nTrying to call launch with executor_prefix...")
try:
    # This simulates what would happen if some code passes executor_prefix
    tb.launch(CHAT, "repo", "task", executor_prefix="something")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")
    traceback.print_exc()

print("\nChecking pending_approvals...")
print(f"  {tb.pending_approvals}")