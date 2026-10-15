import os, sys
print("WORKSPACE_ROOT env:", os.environ.get("WORKSPACE_ROOT"))
print("sys.path[0:3]:", sys.path[:3])
print("pwd:", os.getcwd())
