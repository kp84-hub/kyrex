"""Smoke-test probe: inspect toolbox return shapes (temporary, will be deleted)."""
import json

from kyrex.toolbox import ToolBox

from unittest.mock import MagicMock
tb = ToolBox(MagicMock())
import tempfile, os
tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", dir=os.getcwd(), delete=False)
tf.write("Line 1\nLine 2\nLine 3\n")
tf.close()
r1 = tb.read_local_file(tf.name)
print("TEMP FILE:", tf.name)
print("read_local_file keys:", sorted(r1.keys()))
print(json.dumps(r1, indent=2)[:400])
