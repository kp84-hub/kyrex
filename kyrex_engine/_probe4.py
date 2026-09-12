import os
from kyrex.toolbox import is_safe_path
print("WSR set?", "WORKSPACE_ROOT" in os.environ)
p = os.path.join(os.getcwd(), "..", "outside.txt")
print("is_safe_path:", is_safe_path(p))
