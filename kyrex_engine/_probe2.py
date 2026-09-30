import os
os.environ.pop("PROJECT_SOURCE_ROOT", None)
os.environ.pop("WORKSPACE_ROOT", None)
from kyrex.toolbox import is_safe_path
p = os.path.join(os.getcwd(), "..", "outside.txt")
print("cwd:", os.getcwd())
print("path:", p)
print("resolved:", os.path.realpath(p))
print("is_safe_path:", is_safe_path(p))
