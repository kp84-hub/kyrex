"""Config isolation — no silent ~/.px/config.json fallback for headless runs.

The shared-engine invariant ("no Bot-specific state may have a global
fallback", K_BOT_AUTONOMY.md) reaches into the engine's ConfigManager. When
the engine runs with an explicit or project config, fine. But when it runs
with NEITHER — a lane, a bot, a headless task in a bare directory —
ConfigManager silently resolves self.config_path to ~/.px/config.json, and
then load() READS the operator's global keys and save() WRITES (clobbers)
that global file. This is the "~/.px/config.json clobbered on every
race/consult run" bug, whose write path is just save() writing to a
config_path that fell back to the global.

The runtime callers that hit this: core_bridge.py (5 sites), core.py:234,
cli.py:11 — all `ConfigManager()` with no explicit path.

SAFETY: this test WRITES config files. It redirects HOME to a temp dir and
refuses to run if that redirect didn't take, so it can never touch a real
~/.px/config.json. Run: pytest kyrex_engine/tests/test_config_no_global_fallback.py

Expectation against CURRENT code: the explicit-path and project-preference
tests PASS; the two "no silent global" tests FAIL — and those failures ARE
the bug, localized to config.py __init__ (the `else: self.config_path =
global_cfg` branch) plus save() writing there.
"""
import json
import os
from pathlib import Path

import pytest

from kyrex.config import ConfigManager


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect HOME to a temp dir; abort if it didn't take (never touch real ~/.px)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # windows safety
    resolved = Path(os.path.expanduser("~")).resolve()
    assert resolved == home.resolve(), (
        f"HOME isolation failed (~ -> {resolved}); refusing to run so a real "
        f"~/.px/config.json is never at risk"
    )
    return home


def _global_path(home: Path) -> Path:
    return home / ".px" / "config.json"


def _write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# --- SAFE patterns: these already hold and should stay green ---------------

def test_explicit_path_never_touches_global(isolated_home, tmp_path):
    """A ConfigManager given an explicit path reads/writes only that file."""
    explicit = tmp_path / "lane" / "config.json"
    cm = ConfigManager(explicit)
    cm.save({"api_key": "lane-key", "model": "lane-model"})

    assert json.loads(explicit.read_text())["api_key"] == "lane-key"
    # The global must not have been created as a side effect.
    assert not _global_path(isolated_home).exists(), (
        "explicit-path save leaked into the global config"
    )


def test_project_config_preferred_over_global(isolated_home, tmp_path, monkeypatch):
    """With a project .px/config.json present, that wins over the global."""
    _write(_global_path(isolated_home), {"api_key": "GLOBAL-SECRET"})
    workspace = tmp_path / "proj"
    (workspace / ".git").mkdir(parents=True)          # workspace-root marker
    _write(workspace / ".px" / "config.json", {"api_key": "project-key"})
    monkeypatch.chdir(workspace)

    cm = ConfigManager()
    assert cm._config_source == "project"
    assert cm.load().get("api_key") == "project-key"


# --- THE INVARIANT: no silent global fallback for headless/no-project runs --
# These FAIL against current code. The failures are the bug.

@pytest.mark.xfail(strict=True, reason="silent global read fallback — config.py __init__ else-branch; remove when fixed")
def test_no_silent_global_read(isolated_home, tmp_path, monkeypatch):
    """A no-project headless run must NOT silently read the operator's global keys."""
    _write(_global_path(isolated_home), {"api_key": "GLOBAL-SECRET"})
    bare = tmp_path / "bare"                            # no .git / .px markers
    bare.mkdir()
    monkeypatch.chdir(bare)

    cm = ConfigManager()
    loaded = cm.load()
    assert loaded.get("api_key") != "GLOBAL-SECRET", (
        "SILENT GLOBAL READ: a bare/headless ConfigManager picked up the "
        "operator's ~/.px/config.json keys with no explicit or project config"
    )


@pytest.mark.xfail(strict=True, reason="global clobber via save() on fallback path; remove when fixed")
def test_no_silent_global_clobber(isolated_home, tmp_path, monkeypatch):
    """A no-project headless save() must NOT overwrite the operator's global."""
    gpath = _global_path(isolated_home)
    _write(gpath, {"api_key": "USER-REAL-KEY", "model": "user-model"})
    before = gpath.read_text()

    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)

    cm = ConfigManager()
    cm.save({"model": "lane-model"})                   # e.g. per-lane model injection

    assert gpath.read_text() == before, (
        "GLOBAL CLOBBER: a bare/headless save() wrote into ~/.px/config.json "
        "(user's global model was overwritten). This is the race/consult clobber."
    )
