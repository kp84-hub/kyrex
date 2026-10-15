import os, json
print("in script:", os.environ.get("WORKSPACE_ROOT"))
import pytest
print("pytest sees:", os.environ.get("WORKSPACE_ROOT"))
