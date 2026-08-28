"""Shared data root for Kyrex Cloud.

Every module that needs to persist data (bots registry, audit log, MCP
configuration) derives its path from ``data_dir()`` rather than hardcoding
``~/.kyrex``.

The root is read from the ``KYREX_DATA_DIR`` environment variable at call
time, with ``~/.kyrex`` as the default.  Relative paths are resolved to
absolute.  The directory is created if it does not exist.

The module-level ``DATA_DIR`` constant is evaluated once at import time.
Modules that need per-call resolution (e.g. because the environment changes
between imports) should call ``data_dir()`` directly.
"""

import os
from pathlib import Path


def data_dir() -> Path:
    """Return the resolved Kyrex data root, creating it if absent.

    The root is read from the ``KYREX_DATA_DIR`` environment variable.
    When unset, ``~/.kyrex`` is used.

    Returns:
        An absolute :class:`~pathlib.Path` to the data directory, which is
        guaranteed to exist on return.
    """
    raw = os.environ.get("KYREX_DATA_DIR", "")
    if raw:
        path = Path(raw)
    else:
        path = Path.home() / ".kyrex"
    path = path.resolve()
    # The data root holds the bot registry, audit log, task store, and MCP
    # credentials — create it owner-only so nothing inside is world-reachable.
    # Mode applies at creation only; an existing directory's permissions are
    # left untouched.
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


# Module-level convenience constant — evaluated once at import time.
DATA_DIR = data_dir()