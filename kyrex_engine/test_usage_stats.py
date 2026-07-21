import sys
import os
import io
import json

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from kyrex.core import PlaneExecute

# Instantiate with minimal config to avoid network/config loading side effects.
engine = PlaneExecute()
engine.session.history = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
]
engine._total_prompt_tokens = 100
engine._total_completion_tokens = 50
engine._compaction_count = 1
engine._last_compaction_before = 10
engine._last_compaction_after = 5
engine.context_limit = 128000
engine.model = "test-model"
engine.provider._name = "test-provider"

stats = engine.get_usage_stats()
print("stats keys:", sorted(stats.keys()))
print("prompt_tokens:", stats["prompt_tokens"])
print("completion_tokens:", stats["completion_tokens"])
print("history_messages:", stats["history_messages"])

# Test /usage command emission via stdout capture
old_stdout = sys.stdout
sys.stdout = io.StringIO()
result = engine.handle_command("/usage")
sys.stdout.seek(0)
output = sys.stdout.read()
sys.stdout = old_stdout

msg = json.loads(output.strip())
print("/usage emitted type:", msg["type"])
print("/usage value:", msg["value"])
print("/usage files keys:", sorted(msg["files"].keys()))
print("manual /usage returns:", result)
