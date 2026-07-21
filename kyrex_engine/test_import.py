import sys
import os

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from kyrex.core import PlaneExecute

print("core import OK")
print("has get_usage_stats:", hasattr(PlaneExecute, "get_usage_stats"))
