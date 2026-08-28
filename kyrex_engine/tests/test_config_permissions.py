"""Security remediation tests for config storage.

The config file may carry a raw api_key, so it must never be world-readable
at rest (CWE-732).
"""
import os
import stat
from pathlib import Path

from kyrex.config import ConfigManager


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_saved_config_is_owner_only(tmp_path):
    cfg_path = tmp_path / ".px" / "config.json"
    manager = ConfigManager(path=cfg_path)

    manager.save({"api_key": "super-secret"})

    assert cfg_path.exists()
    assert _mode(cfg_path) == 0o600


def test_save_tightens_preexisting_loose_file(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"model": "m"}')
    os.chmod(cfg_path, 0o644)
    manager = ConfigManager(path=cfg_path)

    manager.save({"api_key": "k"})

    assert _mode(cfg_path) == 0o600
    # The merge behaviour is unchanged.
    assert manager.load()["api_key"] == "k"


def test_created_config_dir_is_owner_only(tmp_path):
    cfg_path = tmp_path / "deep" / ".px" / "config.json"
    manager = ConfigManager(path=cfg_path)

    manager.save({"model": "m"})

    assert cfg_path.exists()
    assert _mode(cfg_path.parent) == 0o700
