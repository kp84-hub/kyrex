"""test the ConfigManager explicit-path-no-fallback fix."""
from pathlib import Path
import tempfile, sys, os

# find engine dir relative to this test file
engine_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(engine_dir))

from kyrex.config import ConfigManager

# ── Test: explicit path to a file that doesn't exist yet ──
tmpdir = Path(tempfile.mkdtemp())
nonexistent = tmpdir / "nonexistent_subdir" / "config.json"

cfg = ConfigManager(nonexistent)
actual = cfg.config_path
expected = nonexistent.resolve()

assert actual == expected, (
    f"FAIL: config_path={actual}\n"
    f"      expected (explicit path)={expected}\n"
    f"      = silently fell back to global config instead of honoring "
    f"the explicit path. This is the exact consult/race lane bug."
)
print(f"PASS test1: explicit path honored even though file does not exist")
print(f"  config_path = {actual}")

# ── Test: load() on nonexistent file returns empty dict ──
data = cfg.load()
assert data == {}, f"load() should return empty dict for nonexistent file, got {data}"
print("PASS test2: load() returns {} on nonexistent file")

# ── Test: no-explicit-path still uses workspace/global fallback ──
# (the else branch is untouched, just verify it doesn't crash)
cfg2 = ConfigManager()
assert cfg2.config_path is not None, "no-explicit-path should still resolve"
print(f"PASS test3: no-explicit-path resolved to {cfg2.config_path}")

print("All tests passed.")
tmpdir.rmdir()