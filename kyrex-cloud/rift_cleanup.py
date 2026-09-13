#!/usr/bin/env python3
"""rift_cleanup.py — explicit, guarded cleanup of runtime Bot Rifts.

Scope: the runtime Bot-Rift directories under ``<DATA_DIR>/rifts/<bot_id>``
created by the Cloud layer (``telegram_bot`` /newbot, web ``chat_api``
``create_bot``, and ``git_workflow`` ``--rift``). This is NOT ``internal/rift``
(the Go copy-on-write package); those clones live under ``.rifts/<project>``
next to a source tree and are swept by the TUI.

This module is the ONLY sanctioned deletion path for a Cloud Bot Rift. It is
deliberately conservative — it refuses far more often than it deletes:

  * a Bot being stopped never triggers deletion: nothing calls this module on a
    status change (see :func:`cleanup_bot_rift`'s docstring);
  * a Rift carrying the ``.rift-persistent`` marker is protected and never
    removed, under any invocation;
  * deletion requires an explicit invocation by the Bot's owner or an operator;
  * a retention (recovery) window must have elapsed since the Rift was last
    modified;
  * a Rift with uncommitted changes, an in-flight task, or an active lease is
    refused;
  * the Rift path is canonicalized and must be contained inside
    ``<DATA_DIR>/rifts`` — arbitrary filesystem paths are rejected outright.

Every decision (allow/deny) is written to the existing audit trail.

CLI::

    python3 rift_cleanup.py --list-stale
    python3 rift_cleanup.py --list-stale --json
    python3 rift_cleanup.py --bot <id> --owner <user>            # delete
    python3 rift_cleanup.py --bot <id> --operator                # delete
    python3 rift_cleanup.py --bot <id> --owner <user> --dry-run  # inspect only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import audit  # existing append-only audit trail
import bots  # existing Bot registry
import paths  # data_dir() resolves DATA_DIR at call time

# ── Constants ──────────────────────────────────────────────────────────

RIFT_DIR_NAME = "rifts"
PERSISTENT_MARKER = ".rift-persistent"
LEASE_FILE = ".rift-lease"

# Default recovery window before a Rift becomes eligible for permanent
# deletion. An operator may shorten it deliberately by passing
# ``retention_seconds`` explicitly; it is never bypassed implicitly.
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60  # 7 days

# A lease older than this (by heartbeat) is considered expired.
DEFAULT_LEASE_TTL_SECONDS = 300

# Task statuses that mean "work is in flight for this Bot".
_ACTIVE_STATUSES = frozenset({"queued", "running", "awaiting_approval"})

_GIT_TIMEOUT = 20


class RiftRefused(Exception):
    """A cleanup request was refused by a guard.

    Carries the machine-readable ``reason`` code and the ``bot_id`` so a caller
    can branch on the refusal without parsing a message.
    """

    def __init__(self, reason: str, bot_id: str, detail: str = ""):
        self.reason = reason
        self.bot_id = bot_id
        self.detail = detail
        super().__init__(f"rift cleanup refused for {bot_id!r}: {reason}"
                         + (f" ({detail})" if detail else ""))


# ── Paths ──────────────────────────────────────────────────────────────

def rifts_root() -> Path:
    """The runtime Bot-Rift root: ``<DATA_DIR>/rifts``.

    Resolved at call time via :func:`paths.data_dir`, which honours the
    ``KYREX_DATA_DIR`` environment variable — so tests can redirect it.
    """
    return paths.data_dir() / RIFT_DIR_NAME


def _canonical(path: os.PathLike | str) -> Path:
    """Best-effort canonical absolute path (resolves symlinks when present)."""
    p = Path(path)
    try:
        return p.resolve()
    except OSError:
        # A missing path cannot resolve; fall back to an absolute, lexically
        # cleaned path so containment still compares something real.
        return Path(os.path.abspath(str(p)))


def contained_rift_path(raw: str, *, root: Path | None = None) -> Path:
    """Return the canonical Rift path if it is inside the Rift root.

    Refuses (raising :class:`RiftRefused`) when *raw* is empty, is the root
    itself, or escapes the root via ``..``/symlink. Arbitrary filesystem paths
    are therefore never accepted.
    """
    root = _canonical(root if root is not None else rifts_root())
    if not raw or not str(raw).strip():
        raise RiftRefused("no_rift_registered", "<unknown>")
    candidate = _canonical(raw)
    if candidate == root:
        raise RiftRefused("rift_is_root", "<unknown>", str(candidate))
    try:
        candidate.relative_to(root)
    except ValueError:
        raise RiftRefused("outside_rifts_root", "<unknown>", str(candidate))
    return candidate


# ── Guards (pure-ish helpers; no deletion) ─────────────────────────────

def is_persistent(rift: Path) -> bool:
    """True if the Rift carries the durable ``.rift-persistent`` marker."""
    try:
        return (Path(rift) / PERSISTENT_MARKER).exists()
    except OSError:
        return False


def is_empty_dir(rift: Path) -> bool:
    """True if *rift* exists and holds nothing."""
    try:
        return Path(rift).is_dir() and not any(Path(rift).iterdir())
    except OSError:
        return False


def _is_git_repo(rift: Path) -> bool:
    """True if *rift* looks like a git work tree (has a ``.git`` entry)."""
    return (Path(rift) / ".git").exists()


def git_dirty(rift: Path) -> bool | None:
    """Return True/False for a git work tree, or None if not a git repo.

    ``git status --porcelain`` reports modified, staged, and untracked
    (non-ignored) paths. A copy that succeeds is not a copy of the right state:
    a non-empty ``porcelain`` output means real, uncommitted work would be lost.
    """
    rift = Path(rift)
    if not _is_git_repo(rift):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(rift), "status", "--porcelain"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        # Cannot verify -> treat as unverifiable, never as clean.
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def active_task_count(bot_id: str, *, db_path: Path | None = None) -> int:
    """Count in-flight tasks bound to *bot_id*.

    Reads the Cloud task store (identity chain ``task -> bot_id -> rift``).
    Only opens the database when it already exists, so a pure listing never
    creates files under ``DATA_DIR``.
    """
    if db_path is None:
        db_path = paths.data_dir() / "cloud_tasks.db"
    db_path = Path(db_path)
    if not db_path.exists():
        return 0
    try:
        import task_store  # local import: keep listing cheap and decoupled
        store = task_store.CloudTaskStore(db_path)
    except Exception:
        # An unreadable store must fail closed for deletion purposes: report a
        # sentinel "active" so cleanup refuses rather than guessing there is
        # no work in flight.
        return -1
    try:
        count = 0
        for status in sorted(_ACTIVE_STATUSES):
            count += len(store.list_tasks(status=status, bot_id=bot_id,
                                          limit=1000))
        return count
    finally:
        try:
            store.close()
        except Exception:
            pass


def lease_active(rift: Path, *, now: float | None = None,
                 ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS) -> bool:
    """True if *rift* holds a lease marker with a fresh heartbeat.

    The lease file (``.rift-lease``) is JSON: ``{"holder": str, "pid": int,
    "heartbeat_at": float}``. An absent or unparseable lease is treated as
    inactive (best-effort); a fresh one blocks cleanup.
    """
    lease_path = Path(rift) / LEASE_FILE
    if not lease_path.exists():
        return False
    now = time.time() if now is None else now
    try:
        data = json.loads(lease_path.read_text())
        hb = float(data.get("heartbeat_at", 0))
    except (OSError, ValueError, TypeError):
        return False
    return (now - hb) < ttl_seconds


def age_seconds(rift: Path, *, now: float | None = None) -> float:
    """Seconds since *rift* was last modified (mtime), or ``inf`` if absent."""
    try:
        mtime = Path(rift).stat().st_mtime
    except OSError:
        return float("inf")
    now = time.time() if now is None else now
    return max(0.0, now - mtime)


def dir_size(path: Path) -> int:
    """Best-effort recursive size in bytes (errors skipped)."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# ── Inspection ─────────────────────────────────────────────────────────

def inspect_rift(bot_id: str, rift: Path, *, now: float | None = None,
                 retention_seconds: int = DEFAULT_RETENTION_SECONDS,
                 lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
                 db_path: Path | None = None) -> dict:
    """Compute a Rift's cleanup eligibility and the reasons it is blocked.

    Performs no writes and deletes nothing. Returns a dict describing the
    Rift. ``reasons`` is empty exactly when the Rift is eligible.
    """
    now = time.time() if now is None else now
    rift = Path(rift)
    reasons: list[str] = []

    persistent = is_persistent(rift)
    if persistent:
        reasons.append("persistent")

    dirty = git_dirty(rift)
    if dirty is None and not is_empty_dir(rift):
        # Not a git repo and not empty: changes cannot be verified, so it is
        # never assumed clean.
        reasons.append("unverifiable_non_git")
    elif dirty is True:
        reasons.append("dirty")

    tasks = active_task_count(bot_id, db_path=db_path)
    if tasks != 0:
        # -1 signals an unreadable store; both cases block cleanup.
        reasons.append("active_tasks" if tasks > 0 else "task_store_unreadable")

    leased = lease_active(rift, now=now, ttl_seconds=lease_ttl_seconds)
    if leased:
        reasons.append("active_lease")

    age = age_seconds(rift, now=now)
    within_retention = age < retention_seconds
    if within_retention:
        reasons.append("within_retention")

    return {
        "bot_id": bot_id,
        "rift": str(rift),
        "eligible": not reasons,
        "reasons": reasons,
        "persistent": persistent,
        "dirty": dirty,
        "active_tasks": tasks,
        "leased": leased,
        "age_seconds": age,
        "within_retention": within_retention,
        "size_bytes": dir_size(rift) if rift.exists() else 0,
    }


def list_stale_rifts(*, now: float | None = None,
                     retention_seconds: int = DEFAULT_RETENTION_SECONDS,
                     lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
                     root: Path | None = None) -> list[dict]:
    """List every Rift directory under the Rift root with its eligibility.

    A read-only, dry-run view: no directory is created, modified, or removed.
    """
    root = Path(root if root is not None else rifts_root())
    if not root.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        out.append(inspect_rift(entry.name, entry, now=now,
                                retention_seconds=retention_seconds,
                                lease_ttl_seconds=lease_ttl_seconds))
    return out


# ── Cleanup ────────────────────────────────────────────────────────────

def _audit(audit_fn, bot_id: str, decision: str, outcome: str, detail: str) -> None:
    try:
        audit_fn(bot_id, "rift_cleanup", "tier2", decision, outcome, detail=detail)
    except Exception:
        # Auditing must never turn a refusal into a crash, and must never
        # abort a deletion mid-flight. Failures are surfaced to stderr.
        print(f"[rift_cleanup] audit write failed for {bot_id}: ignoring",
              file=sys.stderr)


def cleanup_bot_rift(
    bot_id: str,
    *,
    owner: str | None = None,
    operator: bool = False,
    dry_run: bool = False,
    now: float | None = None,
    retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
    root: Path | None = None,
    bot_lookup=None,
    audit_fn=None,
    db_path: Path | None = None,
) -> dict:
    """Explicitly clean up one Bot's runtime Rift, with guards.

    Invocation is explicit and attributable: pass *owner* (the Bot's owning
    user) or *operator=True* (an operator acting deliberately). This function
    is never called on a status change — a stopped Bot keeps its Rift.

    Returns a decision dict (see :func:`inspect_rift` plus ``removed``,
    ``dry_run``, ``decision``, ``authorized``). Raises :class:`KeyError` for an
    unknown *bot_id* and :class:`RiftRefused` for a non-contained path.
    """
    if not bot_id or not str(bot_id).strip():
        raise ValueError("bot_id is required")
    bot_id = str(bot_id).strip()
    bot_lookup = bot_lookup or bots.get_bot
    audit_fn = audit_fn or audit.log

    bot = bot_lookup(bot_id)  # KeyError propagates for an unknown Bot
    raw_rift = str(bot.get("rift") or "")
    registered_owner = str((bot or {}).get("owner") or "").strip()

    root_path = Path(root if root is not None else rifts_root())

    # Containment first: an arbitrary path is rejected before anything else.
    try:
        rift = contained_rift_path(raw_rift, root=root_path)
    except RiftRefused as exc:
        exc.bot_id = bot_id
        _audit(audit_fn, bot_id, "denied", exc.reason, str(exc.detail or ""))
        raise

    base = inspect_rift(bot_id, rift, now=now,
                        retention_seconds=retention_seconds,
                        lease_ttl_seconds=lease_ttl_seconds, db_path=db_path)

    # Authorization: an owning user (matching the registered owner) or an
    # explicit operator. Legacy ownerless Bots need the operator flag.
    owner = str(owner or "").strip()
    if operator:
        authorized, authz_reason = True, "operator"
    elif owner:
        if registered_owner and owner == registered_owner:
            authorized, authz_reason = True, "owner"
        else:
            # A supplied owner that does not match — or a Bot with no recorded
            # owner — is a mismatch, not a missing claim.
            authorized, authz_reason = False, "owner_mismatch"
    else:
        # No owner supplied and no operator: refuse so an accidental call can
        # never delete a Rift.
        authorized, authz_reason = False, "not_authorized"

    reasons = list(base["reasons"])
    if not authorized:
        reasons.append(authz_reason)

    base.update({
        "removed": False,
        "dry_run": bool(dry_run),
        "authorized": authorized,
        "authorization": authz_reason,
        "reasons": reasons,
        "eligible": not reasons,
        "decision": "denied" if reasons else "allow",
    })

    if reasons:
        _audit(audit_fn, bot_id, "denied", ",".join(reasons), rift.as_posix()
               if rift else raw_rift)
        return base

    if dry_run:
        _audit(audit_fn, bot_id, "allow", "dry_run", rift.as_posix())
        return base

    # All guards passed: this is the single deletion site.
    try:
        shutil.rmtree(rift)
    except OSError as exc:
        base.update({"reasons": ["remove_failed"], "eligible": False,
                     "decision": "denied"})
        _audit(audit_fn, bot_id, "denied", f"remove_failed: {exc}",
               rift.as_posix())
        return base

    removed = not rift.exists()
    base.update({"removed": removed,
                 "decision": "allow" if removed else "denied"})
    if not removed:
        base["reasons"] = ["remove_did_not_take"]
    _audit(audit_fn, bot_id, base["decision"],
           "removed" if removed else "remove_did_not_take", rift.as_posix())
    return base


# ── CLI ────────────────────────────────────────────────────────────────

def _print_report(rows: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    if not rows:
        print("(no rifts)")
        return
    for r in rows:
        state = "ELIGIBLE" if r["eligible"] else "blocked"
        reasons = ",".join(r["reasons"]) if r["reasons"] else "-"
        mb = r["size_bytes"] / (1024 * 1024)
        days = r["age_seconds"] / 86400
        print(f"  {r['bot_id']:<24} {state:<8} age={days:7.2f}d "
              f"size={mb:8.2f}MB  reasons={reasons}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Explicit, guarded cleanup of runtime Bot Rifts.")
    ap.add_argument("--list-stale", action="store_true",
                    help="list every Rift and whether it is eligible")
    ap.add_argument("--bot", help="Bot id whose Rift should be cleaned")
    ap.add_argument("--owner", help="owning user; must match the registry")
    ap.add_argument("--operator", action="store_true",
                    help="explicit operator override for authorization")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the decision without deleting")
    ap.add_argument("--retention-days", type=float,
                    default=DEFAULT_RETENTION_SECONDS / 86400,
                    help="recovery window before deletion (default 7)")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args(argv)

    retention_seconds = int(max(0.0, args.retention_days) * 86400)

    if args.list_stale:
        rows = list_stale_rifts(retention_seconds=retention_seconds)
        _print_report(rows, args.json)
        return 0

    if not args.bot:
        ap.error("one of --list-stale or --bot is required")

    try:
        result = cleanup_bot_rift(
            args.bot, owner=args.owner, operator=args.operator,
            dry_run=args.dry_run, retention_seconds=retention_seconds)
    except KeyError:
        print(f"unknown bot: {args.bot}", file=sys.stderr)
        return 2
    except RiftRefused as exc:
        print(f"refused: {exc.reason}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        verb = "DRY-RUN" if result["dry_run"] else (
            "REMOVED" if result["removed"] else "REFUSED")
        reasons = ",".join(result["reasons"]) if result["reasons"] else "-"
        print(f"{verb}: {result['bot_id']} ({reasons})")
    return 0 if result["removed"] or result["dry_run"] or not result["reasons"] \
        else 1


if __name__ == "__main__":
    sys.exit(main())
