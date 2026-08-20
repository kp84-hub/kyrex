"""Run test_approval_protocol with shorter timeouts to fit within 10s."""
import os, sys

# Override BEFORE importing the test
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_ALLOWED_CHAT_ID"] = "12345"
os.environ["KYREX_APPROVAL_TIMEOUT"] = "1"
os.environ["KYREX_TASK_TIMEOUT"] = "30"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# We need to run the test content with shortened waits.
# The test has lines like: `tb.APPROVAL_TIMEOUT + 15` which wait 16s.
# We'll patch those to be shorter.
# Instead, let's just import and run a trimmed version.

# Load and modify the test content
test_path = os.path.join(os.path.dirname(__file__), "test_approval_protocol.py")
with open(test_path) as f:
    code = f.read()

# Shorten the long waits
code = code.replace("tb.APPROVAL_TIMEOUT + 15", "3")
code = code.replace("APPROVAL_TIMEOUT + 15", "3")
# Also make TASK_TIMEOUT = 2 for test 8
# but it's already imported, so change before import
os.environ["KYREX_TASK_TIMEOUT"] = "2"

exec(code)