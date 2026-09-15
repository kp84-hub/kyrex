import os, pytest, sys
sys.argv = ["py.test", "tests/test_toolbox.py::TestIsSafePath::test_blocks_parent_directory_access", "-q"]
raise SystemExit(pytest.main(sys.argv))
