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

The data root is read from the ``KYREX_DATA_DIR`` environment variable via
:func:`paths.data_dir`; see :mod:`paths` for details.
"""

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from paths import DATA_DIR

BOTS_FILE = str(DATA_DIR / "bots.json")

# Serialises EVERY read-modify-write of the registry (add_bot, update_bot,
# set_status, remove_bot, claim_bot) so no two writers can interleave between
# their load and their save. Without this a concurrent write is silently lost:
# both writers load the same snapshot and the second save overwrites the
# first, so one Bot registration / status / config change vanishes while both
# callers report success. Reentrant so the helpers can call
# load_bots/save_bots while held.
#
# Scope: this is a threading lock, so it serialises writers WITHIN one
# process (the web backend, a bot adapter, tests). Separate processes each
# hold their own lock and are not mutually excluded by it.
_REGISTRY_LOCK = threading.RLock()

# ── Lifecycle statuses ─────────────────────────────────────────────────
# A Bot's status is a lifecycle LABEL that gates whether the shared Kyrex
# worker / Chat turn path will accept NEW work for it. Kyrex runs Bots on a
# shared worker pool, so a status never starts or stops a separate process:
#   * running — eligible for new Bot-bound conversations and task submissions
#   * paused  — no new turns/tasks; already-accepted work is not interrupted
#   * stopped — no new turns/tasks; already-accepted work is not interrupted
STATUS_STOPPED = "stopped"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"

_VALID_STATUSES = frozenset({STATUS_STOPPED, STATUS_RUNNING, STATUS_PAUSED})


def is_running(bot: dict | None) -> bool:
    """True iff *bot*'s lifecycle permits NEW Chat turns / task submissions.

    Only ``"running"`` admits new work. ``"paused"`` and ``"stopped"`` both
    reject new work, and a missing/unknown status fails closed (not running).
    This is the single source of truth for the lifecycle gate applied in
    ``chat_service.resolve_bot_for_user`` (Bot resolution) and
    ``dev_bot.submit_bot_task`` (task submission). It never starts, stops, or
    observes a process — Kyrex runs Bots on a shared worker.
    """
    return str((bot or {}).get("status") or "").strip() == STATUS_RUNNING


# ── Helpers ────────────────────────────────────────────────────────────

def _ensure_dir():
    """Create the parent directory of BOTS_FILE if it doesn't exist."""
    Path(BOTS_FILE).parent.mkdir(parents=True, exist_ok=True)


def _default_bots() -> dict:
    """Return the empty bots dict — the value stored when the file is missing."""
    return {}


class RegistryError(Exception):
    """The registry exists but cannot be trusted. Never silently empty."""


class BotAlreadyOwned(Exception):
    """The Bot already has an owner.

    A legacy ownership claim may only adopt an OWNERLESS Bot. This is raised
    (never silently ignored) when the target is already owned, so a claim can
    never overwrite — or appear to have won — another owner's Bot.
    """


def _backfill(bot):
    """Supply metadata fields absent from older registries.

    This is deliberately narrow: only fields nothing depends on. A missing
    id, model, rift or status is a real problem and must still be rejected.
    """
    if not isinstance(bot, dict):
        return bot
    if ("created_at" not in bot or "repo" not in bot
            or "system_prompt" not in bot or "owner" not in bot
            or "browser_allowlist" not in bot):
        bot = dict(bot)
        bot.setdefault("created_at", "")
        bot.setdefault("repo", "")
        bot.setdefault("system_prompt", "")
        bot.setdefault("owner", "")
        # Older registries predate the Browser Operator: an absent allowlist
        # backfills to the empty list (deny all navigation), never to a
        # permissive default.
        bot.setdefault("browser_allowlist", [])
    return bot


def validate_browser_allowlist(value) -> list[str]:
    """Validate and normalise a Bot's browser site/domain allowlist.

    The allowlist is a list of bare hostnames (``example.com``), optionally
    with a port. A URL, a scheme, a path, whitespace, or a non-list is
    rejected — the caller must supply exactly the hosts it means to trust.
    Returns the trimmed list.

    Raises:
        ValueError: *value* is not a list of clean hostname strings.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            "browser_allowlist must be a list of hostnames"
        )
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(
                f"browser_allowlist entries must be strings, got {entry!r}"
            )
        host = entry.strip().lower()
        if not host:
            raise ValueError("browser_allowlist entries must be non-empty")
        if "://" in host or "/" in host or any(ch.isspace() for ch in host):
            raise ValueError(
                f"browser_allowlist entry {entry!r} must be a bare hostname "
                "(no scheme, path, or whitespace)"
            )
        if host not in out:
            out.append(host)
    return out


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
    except json.JSONDecodeError as exc:
        # Do NOT return an empty registry here. The Bots would look deleted,
        # and the next save_bots would overwrite the damaged file with an
        # empty one - turning recoverable corruption into permanent loss.
        raise RegistryError(
            "bots registry at %s is not valid JSON: %s" % (BOTS_FILE, exc)
        ) from exc

    # A registry is a JSON object mapping bot id -> bot dict. Any other
    # top-level shape ([], "hello", 5, null) is malformed: reject the whole
    # file with RegistryError rather than letting an AttributeError escape
    # from validation — callers treat RegistryError as "registry untrusted",
    # and a raw AttributeError is not that contract.
    if not isinstance(data, dict):
        raise RegistryError(
            "bots registry at %s must be a JSON object mapping bot id to bot, "
            "got %s" % (BOTS_FILE, type(data).__name__)
        )

    # Reject the whole file rather than silently dropping entries: a Bot
    # that quietly disappears from the registry is indistinguishable from
    # one that was never there. A non-object entry is rejected explicitly so
    # validation never reaches a dict-only method with a scalar/list.
    non_objects = sorted(
        str(bot_id) for bot_id, bot in data.items() if not isinstance(bot, dict)
    )
    if non_objects:
        raise RegistryError(
            "bots registry at %s has non-object entries: %s"
            % (BOTS_FILE, ", ".join(non_objects))
        )

    # Backfill metadata added after a registry was written. A field that
    # nothing depends on must not make an older file unloadable.
    data = {bot_id: _backfill(bot) for bot_id, bot in data.items()}
    invalid = [bot_id for bot_id, bot in data.items() if not _is_valid_bot(bot)]
    if invalid:
        raise RegistryError(
            "bots registry at %s has malformed entries: %s"
            % (BOTS_FILE, ", ".join(sorted(invalid)))
        )
    return dict(data)


def save_bots(bots: dict[str, dict]) -> None:
    """Write the bots registry to BOTS_FILE as JSON, atomically.

    The JSON is written to a temporary file in the SAME directory, flushed and
    closed, then moved into place with :func:`os.replace`, so a reader (or a
    crash) can never observe a half-written registry. Writing the target in
    place would risk a torn file, and ``load_bots`` fails closed on invalid
    JSON — turning a recoverable write hiccup into a hard outage of every Bot
    surface (list/create/bind/execute).

    The temporary file is removed if the write fails, so a failed save never
    leaves a stray artifact behind. An existing registry keeps its file mode.
    """
    _ensure_dir()
    target = Path(BOTS_FILE)
    # Same directory as the target: os.replace is only atomic within one
    # filesystem, and a cross-device move would silently degrade to a copy.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(bots, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        # Preserve the existing registry's permissions. A brand-new file
        # keeps mkstemp's 0600, which is stricter than a umask default — this
        # never widens access to the registry.
        try:
            mode = target.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = None
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except BaseException:
        # Never leave the temporary file behind on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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
    repo: str = "",
    system_prompt: str = "",
    owner: str = "",
    browser_allowlist: list | None = None,
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
    with _REGISTRY_LOCK:
        bots = load_bots()

        if bot_id in bots:
            raise ValueError(
                f"bot id {bot_id!r} already exists — use remove_bot first "
                "or pick a different id"
            )

        bot = _build_bot(bot_id, name, model, rift, policy, status, repo,
                         system_prompt, owner, browser_allowlist)
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
    with _REGISTRY_LOCK:
        bots = load_bots()
        if bot_id not in bots:
            raise KeyError(f"unknown bot id: {bot_id!r}")
        removed = bots.pop(bot_id)
        save_bots(bots)
    return removed


def update_bot(bot_id: str, **fields) -> dict:
    """Update whitelisted fields on an existing bot. Returns the updated bot.

    ``owner`` is deliberately NOT updatable here: ownership is granted exactly
    once, to an ownerless Bot, by :func:`claim_bot`. Allowing it through this
    general-purpose path would let any caller transfer (or steal) an already
    owned Bot and silently redirect which user may manage it — an ownership
    change must never be a side effect of a config edit.

    Raises KeyError if bot_id is unknown, ValueError on an unknown field.
    """
    allowed = {"name", "model", "repo", "system_prompt", "rift", "policy",
               "browser_allowlist"}
    with _REGISTRY_LOCK:
        bots = load_bots()
        if bot_id not in bots:
            raise KeyError(f"unknown bot id: {bot_id!r}")
        for k in fields:
            if k not in allowed:
                raise ValueError(f"cannot update field {k!r}; allowed: {sorted(allowed)}")
        # Validate before mutating so a malformed allowlist never lands half-applied.
        if "browser_allowlist" in fields:
            fields = dict(fields)
            fields["browser_allowlist"] = validate_browser_allowlist(
                fields["browser_allowlist"]
            )
        bots[bot_id].update(fields)
        save_bots(bots)
        return bots[bot_id]


def set_status(bot_id: str, status: str) -> dict:
    """Update the status of an existing bot.

    Validates *status* **before** mutating the in-memory dict so that the
    stored bot is never left in a partially-updated state when the value
    is rejected.

    Args:
        bot_id: the id of the bot to update.
        status: one of ``"stopped"``, ``"running"``, ``"paused"``.

    Returns the updated bot dict.

    Raises:
        KeyError if *bot_id* is unknown.
        ValueError if *status* is not a valid status.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; must be one of "
            f"{sorted(_VALID_STATUSES)}"
        )
    with _REGISTRY_LOCK:
        bots = load_bots()
        if bot_id not in bots:
            raise KeyError(f"unknown bot id: {bot_id!r}")
        # Validation is done — safe to mutate and persist.
        bots[bot_id]["status"] = status
        save_bots(bots)
        return bots[bot_id]


def claim_bot(bot_id: str, owner: str) -> dict:
    """One-time legacy claim: assign *owner* to an OWNERLESS Bot.

    Legacy Bots were created before ownership was persisted, so they carry an
    empty ``owner``. They are visible to users but not manageable, because the
    lifecycle/configuration endpoints require ``bot.owner == user``. This
    function performs the ONLY mutation that makes such a Bot manageable: it
    records the claiming user as ``owner``.

    Safety invariants (all fail closed):

    * Only an ownerless Bot can be claimed. A Bot owned by ANYONE — another
      user, or the caller — raises :class:`BotAlreadyOwned` and is never
      overwritten. Ownership is never transferred or stolen.
    * Only ``owner`` is written. ``status``, ``policy``, ``rift``, ``model``,
      ``system_prompt`` and every other field are left byte-for-byte intact —
      claiming grants control, it does not start the Bot or change its policy.
    * The check-then-set is atomic under the registry lock, so two concurrent
      claims cannot both win and a claim cannot race another writer.

    Returns the updated bot dict.

    Raises:
        KeyError if *bot_id* is unknown.
        ValueError if *owner* is empty (a claim must record a real owner).
        BotAlreadyOwned if the Bot already has an owner.
    """
    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("claim requires a non-empty owner")
    with _REGISTRY_LOCK:
        bots = load_bots()
        if bot_id not in bots:
            raise KeyError(f"unknown bot id: {bot_id!r}")
        existing = str(bots[bot_id].get("owner") or "").strip()
        if existing:
            raise BotAlreadyOwned(
                f"bot {bot_id!r} is already owned by {existing!r}; "
                "only an unowned legacy Bot can be claimed"
            )
        bots[bot_id]["owner"] = owner
        save_bots(bots)
        return bots[bot_id]


def list_bots() -> list[dict]:
    """Return all bots sorted by their ``id`` field (case-sensitive)."""
    bots = load_bots()
    return [bots[bid] for bid in sorted(bots)]


# ── Internal helpers ───────────────────────────────────────────────────

def _build_bot(
    bot_id: str,
    name: str,
    model: str,
    rift: str,
    policy: dict | None,
    status: str,
    repo: str = "",
    system_prompt: str = "",
    owner: str = "",
    browser_allowlist: list | None = None,
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
        "repo": repo,
        "system_prompt": system_prompt,
        "owner": owner,
        "browser_allowlist": validate_browser_allowlist(browser_allowlist),
        "policy": policy if policy is not None else {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }


def _is_valid_bot(bot: dict) -> bool:
    """Check that a bot dict has all required keys and valid status.

    A non-dict is invalid, never a crash: validation must surface a malformed
    registry as RegistryError, so every caller reaches a single documented
    failure type instead of an AttributeError from a dict-only method.
    """
    if not isinstance(bot, dict):
        return False
    required = {"id", "name", "model", "rift", "policy", "created_at", "status"}
    if not required.issubset(bot.keys()):
        return False
    if bot.get("status") not in _VALID_STATUSES:
        return False
    return True