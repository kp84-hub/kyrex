"""Tests for paths.py — shared data root discovery and initialisation.

Covers:
  - Default is ~/.kyrex when KYREX_DATA_DIR is unset
  - KYREX_DATA_DIR overrides the default
  - The directory is created if it does not exist
  - A relative path is resolved to an absolute one

Run: python3 test_paths.py
"""

import os
import sys
import tempfile
from pathlib import Path

# Prevent the real ~/.kyrex from being created by the import.
# We patch the environment *before* importing paths so the module-level
# DATA_DIR constant uses our test directory.
# Strategy: for each test that needs a specific environment, we import paths
# inside the test with a clean environment, or we test data_dir() directly
# rather than relying on the module-level constant.

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        failures.append(name)


# ── Test 1: default is ~/.kyrex when the variable is unset ──────────────
print("Test 1: default is ~/.kyrex when KYREX_DATA_DIR is unset")

# Unset the variable, then reimport paths.
old_env = os.environ.pop("KYREX_DATA_DIR", None)
try:
    # Import inside the test so the module-level constant is evaluated
    # with the current environment.
    import importlib
    import paths
    importlib.reload(paths)

    expected = Path.home() / ".kyrex"
    check("DATA_DIR equals ~/.kyrex",
          paths.DATA_DIR == expected.resolve(),
          f"expected {expected.resolve()!r}, got {paths.DATA_DIR!r}")
finally:
    if old_env is not None:
        os.environ["KYREX_DATA_DIR"] = old_env


# ── Test 2: KYREX_DATA_DIR overrides the default ───────────────────────
print("\nTest 2: KYREX_DATA_DIR overrides the default")

with tempfile.TemporaryDirectory() as td:
    custom_dir = os.path.join(td, "custom_data")
    # Create the dir so data_dir() doesn't error — data_dir() itself will create it.
    os.environ["KYREX_DATA_DIR"] = custom_dir

    import importlib
    import paths
    importlib.reload(paths)

    check("DATA_DIR equals the overridden path",
          str(paths.DATA_DIR) == str(Path(custom_dir).resolve()),
          f"expected {str(Path(custom_dir).resolve())!r}, got {str(paths.DATA_DIR)!r}")
    check("directory exists after data_dir() call",
          Path(custom_dir).resolve().is_dir(),
          f"{Path(custom_dir).resolve()} does not exist")

del os.environ["KYREX_DATA_DIR"]


# ── Test 3: the directory is created if missing ─────────────────────────
print("\nTest 3: the directory is created if missing")

with tempfile.TemporaryDirectory() as td:
    non_existent = os.path.join(td, "brand_new_dir")
    # Double-check it doesn't exist yet.
    check("precondition: directory does not exist",
          not Path(non_existent).exists(),
          f"{non_existent} already exists")

    os.environ["KYREX_DATA_DIR"] = non_existent

    import importlib
    import paths
    importlib.reload(paths)

    check("DATA_DIR points to the new directory",
          str(paths.DATA_DIR) == str(Path(non_existent).resolve()),
          f"got {str(paths.DATA_DIR)!r}")
    check("directory was created by data_dir()",
          Path(non_existent).resolve().is_dir(),
          f"{Path(non_existent).resolve()} was not created")

del os.environ["KYREX_DATA_DIR"]


# ── Test 4: a relative path is resolved to an absolute one ──────────────
print("\nTest 4: a relative path is resolved to an absolute one")

with tempfile.TemporaryDirectory() as td:
    original_cwd = os.getcwd()
    try:
        os.chdir(td)
        rel_path = "my_kyrex_data"
        abs_path = os.path.join(td, rel_path)

        os.environ["KYREX_DATA_DIR"] = rel_path

        import importlib
        import paths
        importlib.reload(paths)

        # The resolved path should be absolute (not relative).
        check("DATA_DIR is absolute",
              os.path.isabs(str(paths.DATA_DIR)),
              f"got {str(paths.DATA_DIR)!r}")
        check("DATA_DIR resolves to the absolute version",
              str(paths.DATA_DIR) == str(Path(abs_path).resolve()),
              f"expected {str(Path(abs_path).resolve())!r}, got {str(paths.DATA_DIR)!r}")
        check("directory was created",
              Path(abs_path).resolve().is_dir(),
              f"{Path(abs_path).resolve()} was not created")
    finally:
        os.chdir(original_cwd)
        del os.environ["KYREX_DATA_DIR"]


# ── Summary ────────────────────────────────────────────────────────────
print()
if not failures:
    print("ALL TESTS PASSED")
else:
    print(f"{len(failures)} FAILURE(S): {failures}")

sys.exit(1 if failures else 0)