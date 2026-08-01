import pytest
import os
import tempfile
import json
from pathlib import Path
from unittest.mock import patch


@pytest.fixture(autouse=True)
def auto_approve_gates():
    """Auto-approve all interactive/gate paths so tests don't hang.

    Three blocking code paths exist in ToolBox that rely on protocol
    messages on stdout/stdin that no one answers in a bare pytest run:

      1. _diff_gate() -- emits confirm_request, blocks on event.wait(300).
      2. _propose_edit() -- emits propose_edit, blocks on event.wait(300),
         used when KYREX_VSCODE is set.
      3. _propose_deletion() -- emits confirm_request (deletion), blocks
         on event.wait(300), reached via run_command when _is_interactive()
         returns True.

    Additionally, run_command has a needs_confirm path that calls input()
    when _is_interactive() returns True, which also hangs.

    This fixture patches the three blocking methods to return True
    immediately, and forces _is_interactive to False so the
    needs_confirm `input()` path in run_command is skipped too.
    """
    patches = [
        patch("kyrex.toolbox.ToolBox._diff_gate", return_value=True),
        patch("kyrex.toolbox.ToolBox._propose_edit", return_value=True),
        patch("kyrex.toolbox.ToolBox._propose_deletion", return_value=True),
        patch("kyrex.toolbox._is_interactive", return_value=False),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


@pytest.fixture
def temp_config_file():
    """Create a temporary config.json for testing."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        yield f.name
        os.unlink(f.name)


@pytest.fixture
def clean_env():
    """Remove relevant env vars for test isolation."""
    env_vars = [
        "KYREX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "KYREX_PROVIDER", "PROVIDER",
        "KYREX_BASE_URL", "OPENAI_BASE_URL",
    ]
    saved = {}
    for var in env_vars:
        saved[var] = os.environ.get(var)
        if var in os.environ:
            del os.environ[var]
    yield
    for var, val in saved.items():
        if val is not None:
            os.environ[var] = val


@pytest.fixture
def config_manager(temp_config_file, clean_env):
    """Create a ConfigManager with a temp config file."""
    from kyrex.config import ConfigManager
    cm = ConfigManager(Path(temp_config_file))
    cm._data = {}
    return cm