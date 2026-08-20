"""Quick signature check -- will be deleted."""
import sys, os
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'test-token')
os.environ.setdefault('TELEGRAM_ALLOWED_CHAT_ID', '12345')
os.environ.setdefault('KYREX_TASK_TIMEOUT', '30')
os.environ.setdefault('KYREX_APPROVAL_TIMEOUT', '3')
sys.path.insert(0, '.')
import telegram_bot as tb
import inspect
print("launch sig:", inspect.signature(tb.launch))
print("run_task sig:", inspect.signature(tb.run_task))