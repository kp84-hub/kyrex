"""Quick debug to test telegram_bot re-exports work correctly."""
import os, sys
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_ID", "12345")
os.environ.setdefault("KYREX_APPROVAL_TIMEOUT", "3")
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import telegram_bot as tb

print("STATUS_LABELS:", "STATUS_LABELS" in dir(tb))
print("resolve_executor:", callable(tb.resolve_executor))
print("busy_lock:", type(tb.busy_lock).__name__)
print("pending_approvals:", type(tb.pending_approvals).__name__)
print("APPROVAL_TIMEOUT:", tb.APPROVAL_TIMEOUT)
print("EXECUTORS:", tb.EXECUTORS)
print("run_task module:", tb.run_task.__module__)
print("launch module:", tb.launch.__module__)
print("send_message:", callable(tb.send_message))

# Verify resolve_executor works
p, t, e = tb.resolve_executor("hello world")
print("resolve_executor('hello world'):", p, t, e)

# Verify busy_lock is shared
tb.busy_lock.acquire()
print("busy_lock locked:", tb.busy_lock.locked())
tb.busy_lock.release()
print("busy_lock unlocked:", not tb.busy_lock.locked())

print("ALL CHECKS PASSED")