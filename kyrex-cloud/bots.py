"""Bot registry — persisted as JSON on disk.

A Bot has:
  id       — short slug (e.g. "nightly-qa")
  name     — human-readable label
  model    — provider/model string (e.g. "anthropic:claude-sonnet-4-20250506")
  rift     — absolute path to the persistent workspace directory
  policy   — arbitrary dict of rules (empty for now)
  status   — one of "stopped", "running", "paused"

The registry file lives at BOTS_FILE (default ~/.kyrex/bots.json) and can be
overridden for testing via bots.BOTS_FILE = "/tmp/test/bots.json".
"""

import json
import os
from pathlib import Path

BOTS_FILE = os.path.join(str(Path.home()), ".kyrex", "bots.json")

# ── Valid statuses ─────────────────────────────────────────────────────
_VALID_STATUSES = frozenset({"stopped", "running", "paused"})


# ── Helpers ────────────────────────────────────────────────────────────

def _ensure_dir():
    """Create the parent directory of BOTS_FILE if it doesn't exist."""
    Path(BOTS_FILE).parent.mkdir(parents=True, exist_ok=True)


def _default_bots() -> dict:
    """Return the empty bots dict — the value stored when the file is missing."""
    return {}


def load_bots() -> dict[str, dict]:
    """Load the registry from BOTS_FILE.

    Returns a dict mapping bot id → bot dict (which always contains
    'id', 'name', 'model', 'rift', 'policy', 'status').

    If the file doesn't exist the empty dict is returned (no error).
    """
    try:
        with open(BOTS_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return _default_bots()
    except json.JSONDecodeError:
        # Corrupt file — treat as empty so the system isn't bricked.
        return _default_bots()

    # Ensure all values pass validation on load.
    cleaned = {}
    for bot_id, bot in data.items():
        if _is_valid_bot(bot):
            cleaned[bot_id] = bot
    return cleaned


def save_bots(bots: dict[str, dict]) -> None:
    """Write the bots registry to BOTS_FILE as JSON."""
    _ensure_dir()
    with open(BOTS_FILE, "w") as f:
        json.dump(bots, f, indent=2, sort_keys=True)
        f.write("\n")


def get_bot(bot_id: str) -> dict:
    """Return the bot dict for *bot_id*.

    Raises KeyError with a clear message if the id is unknown.
    Never returns a default or fallback.
    """
    bots = load_bots()
    if bot_id not in bots:
        raise KeyError(f"unknown bot id: {bot_id!r}")
    return bots[bot_id]


def add_bot(
    bot_id: str,
    name: str,
    model: str,
    rift: str,
    policy: dict | None = None,
    status: str = "stopped",
) -> dict:
    """Register a new bot.

    Args:
        bot_id:   unique slug identifier.
        name:     human-readable label.
        model:    provider/model string.
        rift:     absolute path to the persistent workspace.
        policy:   optional dict of rules (defaults to {}).
        status:   one of "stopped", "running", "paused" (default "stopped").

    Returns the bot dict that was stored.

    Raises ValueError if *bot_id* already exists in the registry (add_bot
    refuses to overwrite).
    """
    bots = load_bots()

    if bot_id in bots:
        raise ValueError(
            f"bot id {bot_id!r} already exists — use remove_bot first "
            "or pick a different id"
        )

    bot = _build_bot(bot_id, name, model, rift, policy, status)
    bots[bot_id] = bot
    save_bots(bots)
    return bot


def remove_bot(bot_id: str) -> dict:
    """Remove the bot registry entry for *bot_id*.

    This function removes the registry entry **only** and never touches
    the rift directory on disk.

    Returns the removed bot dict.

    Raises KeyError if *bot_id* is unknown.
    """
    bots = load_bots()
    if bot_id not in bots:
        raise KeyError(f"unknown bot id: {bot_id!r}")
    removed = bots.pop(bot_id)
    save_bots(bots)
    return removed


# ── Internal helpers ───────────────────────────────────────────────────

def _build_bot(
    bot_id: str,
    name: str,
    model: str,
    rift: str,
    policy: dict | None,
    status: str,
) -> dict:
    """Construct and validate a bot dict."""
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; must be one of "
            f"{sorted(_VALID_STATUSES)}"
        )
    return {
        "id": bot_id,
        "name": name,
        "model": model,
        "rift": rift,
        "policy": policy if policy is not None else {},
        "status": status,
    }


def _is_valid_bot(bot: dict) -> bool:
    """Check that a bot dict has all required keys and valid status."""
    required = {"id", "name", "model", "rift", "policy", "status"}
    if not required.issubset(bot.keys()):
        return False
    if bot.get("status") not in _VALID_STATUSES:
        return False
    return True