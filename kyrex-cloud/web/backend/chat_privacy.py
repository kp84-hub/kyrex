"""Owner-scoped controls for information added to model requests."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from paths import data_dir


def write_private_json(path: Path, value) -> None:
    """Publish JSON atomically with private directory/file permissions."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _path(user: str) -> Path:
    if not isinstance(user, str) or not user:
        raise ValueError("A signed-in user is required.")
    identity = hashlib.sha256(user.encode()).hexdigest()
    return data_dir() / "chat_privacy" / f"{identity}.json"


def settings(user: str) -> dict:
    path = _path(user)
    share = True  # Preserve explicitly saved memory's existing behavior.
    if path.exists():
        try:
            value = json.loads(path.read_text())
            share = isinstance(value, dict) and value.get("share_saved_memory") is True
        except (ValueError, OSError):
            share = False  # A damaged preference must not enable sharing.
    return {"share_saved_memory": share, "secret_filter_enabled": True}


def save_settings(user: str, body) -> dict:
    if (not isinstance(body, dict) or set(body) != {"share_saved_memory"}
            or type(body["share_saved_memory"]) is not bool):
        raise ValueError("share_saved_memory must be a boolean.")
    write_private_json(_path(user), body)
    return settings(user)
