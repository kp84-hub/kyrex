"""browser-host/profiles.py - persistent profile directories for the local host.

Each ``(owner, bot_id)`` pair resolves to its own directory under the profiles
root, so two Bots - or one Bot serving two owners - never share cookies,
storage, or logins. A single, opt-in *shared* profile exists for the owner's
personal account; it is never the default and must be requested explicitly.

Phase 1 is LOCAL ONLY: this module touches the local filesystem and nothing
else. It knows nothing about Cloud, a control channel, or enrollment.

Isolation rules (the contract these paths encode):

  * the key is ``(owner, bot_id)`` - both are required;
  * every component is slugged, so ``..`` and separators cannot escape the root;
  * the shared profile lives under a distinct ``shared/`` subtree, so it can
    never collide with a per-bot path.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_PROFILES_ROOT = "/srv/kyrex/browser-host/profiles"


def _slug(value, default: str = "unbound") -> str:
    """Sanitize an id into one safe path component (no separators, no ``..``)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = re.sub(r"\.{2,}", "_", cleaned)   # collapse any ".." run
    cleaned = cleaned.strip("._")
    return cleaned[:96] or default


def profiles_root(root=None) -> Path:
    """The profiles root: an explicit path, else ``KYREX_BROWSER_PROFILES_ROOT``."""
    raw = (
        root
        or os.environ.get("KYREX_BROWSER_PROFILES_ROOT")
        or DEFAULT_PROFILES_ROOT
    )
    return Path(raw)


def shared_profile_path(root=None) -> Path:
    """The single opt-in shared profile for the owner's personal account."""
    return profiles_root(root) / "shared" / "personal"


def profile_dir(owner, bot_id, *, root=None, shared: bool = False) -> Path:
    """The persistent profile directory for ``(owner, bot_id)`` (or shared).

    Raises ``ValueError`` when a per-bot profile is requested without both an
    owner and a bot id - a profile with no isolation key must not exist.
    """
    if shared:
        return shared_profile_path(root)
    owner = str(owner or "").strip()
    bot_id = str(bot_id or "").strip()
    if not owner or not bot_id:
        raise ValueError(
            "a managed browser profile requires both an owner and a bot id"
        )
    return (
        profiles_root(root)
        / f"bot-{_slug(bot_id)}"
        / f"owner-{_slug(owner)}"
    )


def ensure_profile(owner, bot_id, *, root=None, shared: bool = False) -> Path:
    """Return (creating if needed) the profile directory and its parents."""
    path = profile_dir(owner, bot_id, root=root, shared=shared)
    path.mkdir(parents=True, exist_ok=True)
    return path
