import os, pytest, sys
sys.argv = ["", "tests/test_toolbox.py", "-q", "--no-header", "--rootdir=.", "-p", "no:cacheprovider", "--co", "-q"]
raise SystemExit(pytest.main(sys.argv))
