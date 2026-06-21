import pytest
import os
import tempfile
import json
from pathlib import Path


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
