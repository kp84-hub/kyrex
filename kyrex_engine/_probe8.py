import os, pytest, sys
sys.argv = ["pytest", "tests/test_toolbox.py::TestIsSafePath::test_blocks_parent_directory_access", "-q"]
print("before pytest, WSR:", os.environ.get("WORKSPACE_ROOT"))
raise SystemExit(pytest.main(sys.argv))
