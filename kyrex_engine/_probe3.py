import os
os.environ.pop("PROJECT_SOURCE_ROOT", None)
os.environ["WORKSPACE_ROOT"] = "/tmp/kyrex-task-agent-1788277898-smoke-test-task"
from kyrex.toolbox import is_safe_path
p = os.path.join(os.getcwd(), "..", "outside.txt")
print("cwd:", os.getcwd())
print("resolved:", os.path.realpath(p))
print("is_safe_path:", is_safe_path(p))
