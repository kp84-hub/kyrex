import os, pytest, sys
sys.argv = ["", "tests/test_toolbox.py::TestIsSafePath::test_blocks_parent_directory_access", "-q"]
raise SystemExit(pytest.main(sys.argv))
