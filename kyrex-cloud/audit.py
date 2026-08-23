"""Append-only audit log for Bot actions.

Each call to *log* writes one JSON line to an append-only file.
*read_entries* returns entries newest first, optionally filtered by bot id.

A malformed line raises rather than being silently skipped, consistent with
the principle that the registry also refuses to load a corrupt file.

Usage::

    from audit import log, read_entries

    log("bot-1", "run_task", "tier2", "approved", "ok", detail="task-42")
    for entry in read_entries(bot_id="bot-1", limit=5):
        ...
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from paths import DATA_DIR

AUDIT_FILE = str(DATA_DIR / "audit.jsonl")

_VALID_DECISIONS = frozenset({
    "auto", "approved", "denied", "timeout",
    "allow", "deny", "approval_required",
})

_lock = threading.Lock()


# ── Public API ─────────────────────────────────────────────────────────


def log(
    bot_id: str,
    operation: str,
    tier: str,
    decision: str,
    outcome: str,
    detail: str | None = None,
) -> None:
    """Append a single audit entry to the JSONL file.

    Args:
        bot_id:    bot identifier (e.g. "nightly-qa").
        operation: what action was attempted.
        tier:      classification tier (e.g. "tier1", "tier2").
        decision:  one of ``"auto"``, ``"approved"``, ``"denied"``, ``"timeout"``.
        outcome:   free-text result summary.
        detail:    optional supplementary information.

    Raises:
        ValueError if *decision* is not a recognised value.
    """
    if decision not in _VALID_DECISIONS:
        raise ValueError(
            f"invalid decision {decision!r}; must be one of "
            f"{sorted(_VALID_DECISIONS)}"
        )

    _ensure_dir()
    with _lock:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bot_id": bot_id,
            "operation": operation,
            "tier": tier,
            "decision": decision,
            "outcome": outcome,
        }
        if detail is not None:
            entry["detail"] = detail

        with open(AUDIT_FILE, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
            f.flush()


def read_entries(
    bot_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read audit entries from the JSONL file, newest first.

    Args:
        bot_id: optional filter — only entries for this bot are returned.
        limit:  optional maximum number of entries to return.

    Returns:
        List of entry dicts, most recent first.

    Raises:
        IOError if the file cannot be read.
        ValueError with the offending line number and file path if a line
        is malformed JSON or the wrong shape.
    """
    try:
        with open(AUDIT_FILE, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    entries: list[dict] = []
    for lineno, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "malformed audit line %d in %s: %s" % (lineno, AUDIT_FILE, exc)
            )
        # Basic shape check: must be a dict with at least timestamp + bot_id
        if not isinstance(obj, dict) or "timestamp" not in obj or "bot_id" not in obj:
            raise ValueError(
                "malformed audit line %d in %s: missing required fields"
                % (lineno, AUDIT_FILE)
            )
        entries.append(obj)

    # Reverse so newest first (the file is append-only, so last = newest).
    entries.reverse()

    if bot_id is not None:
        entries = [e for e in entries if e.get("bot_id") == bot_id]

    if limit is not None:
        entries = entries[:limit]

    return entries


# ── Internal helpers ───────────────────────────────────────────────────


def _ensure_dir() -> None:
    """Create the parent directory of AUDIT_FILE if it doesn't exist."""
    Path(AUDIT_FILE).parent.mkdir(parents=True, exist_ok=True)