"""Pytest interop for a script-style test suite.

The primary test runner here is direct execution: script-style harnesses
(test_*.py) run their checks at import time, print PASS/FAIL lines, and exit
nonzero on failure — run them as `python3 test_<name>.py`. Collecting those
files with pytest executes every harness during collection, and the harness
sys.exit aborts the run with INTERNALERROR before any test runs.

A handful of files ARE pytest-style (bare `def test_` + asserts, safe to
import). This hook auto-detects the difference by AST: a module whose top
level is only imports, definitions, docstrings, or an
`if __name__ == "__main__":` block is collected by pytest; anything that
executes at import time (harnesses) is skipped — run it directly instead.
Worst case, a hybrid file is skipped by pytest and still runs standalone.
"""
import ast


def _import_safe(path):
    """True if the module executes nothing at import time."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return False  # unreadable/broken -> don't collect
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom,
                             ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring / bare literal
        if isinstance(node, ast.If):
            test = node.test
            guarded = (isinstance(test, ast.Compare)
                       and isinstance(test.left, ast.Name)
                       and test.left.id == "__name__"
                       and any(isinstance(c, ast.Constant) and c.value == "__main__"
                               for c in test.comparators))
            if guarded:
                continue
        return False  # anything else runs at import time -> harness
    return True


def pytest_ignore_collect(collection_path, config):
    if collection_path.name.startswith("test_") and collection_path.suffix == ".py":
        return not _import_safe(str(collection_path))
    return None
