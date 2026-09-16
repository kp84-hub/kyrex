"""manual_mode.py — the durable manual-control boundary for the Browser Host.

When the owner takes eyes-on/keyboard on a Bot's persistent Chromium profile
(the manual viewer), browser automation for that profile MUST stop. This module
is that boundary: a tiny, dependency-free, filesystem-only state machine that
BOTH the Cloud agent and the viewer stack agree on through ONE shared location.

One lock, one location
----------------------
The single source of truth is a per-``(owner, bot_id)`` ``flock`` held for the
entire life of whichever side owns the profile:

  * the **manual viewer** acquires it before it starts any X/Chromium/VNC
    process and holds it until its session ends;
  * the **automation agent** acquires the SAME lock before it starts the
    browser operator and holds it through the whole task lifetime (approvals,
    cancellation, subprocess shutdown, terminal result).

Because both sides *acquire and hold a kernel lock* — rather than a lone
check-then-start — there is no TOCTOU window in either direction. If the
viewer owns the profile the agent's acquire fails and it refuses
(``ManualControlActive``); if automation owns it the viewer's acquire fails and
it refuses. Two Chromium processes can never share one profile.

The lock and its record live together under a single dedicated state directory
that sits ON THE SHARED PERSISTENT PROFILES VOLUME, so the agent container and
the viewer container (which already mount the same ``profiles`` volume) see the
exact same files at the exact same path:

  ``<profiles_root>/.kyrex-viewer-state/{locks,records}/<key>.{lock,json}``

resolved from ``KYREX_VIEWER_STATE_DIR`` (explicit) or, failing that, from
``KYREX_BROWSER_PROFILES_ROOT``. No ``/run`` split: /run is per-container and
would make the two sides invisible to each other.

Containment
-----------
Every path is built from slugged components and re-checked with ``realpath``
before use; a component that is ``..``/separators is refused, a path that
resolves outside the state root is refused, and lock/record files are opened
with ``O_NOFOLLOW`` so a symlink planted at the lock/record name cannot redirect
the write off the shared volume.

Crash / reboot safety
---------------------
The lock is a kernel ``flock``: the instant the holder's process, container, or
VPS dies, the kernel drops it. A stray record left behind is a HINT only — a
record counts as active solely while the lock is genuinely held AND the TTL has
not expired; anything else is reaped on the next ``active()`` / ``is_any_active``
call. So no crash can leave automation refused forever, and no crash can leave
two processes on one profile.

Nothing here ever writes a secret. Records carry only shape metadata (ids,
timestamps, ports, kind). The VNC password lives only in the per-session
``x11vnc`` auth file created by ``viewer_ctl.py`` at 0600 and is never
persisted, logged, or copied anywhere.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import stat
import tempfile
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────

STATE_DIR_ENV = "KYREX_VIEWER_STATE_DIR"
PROFILES_ROOT_ENV = "KYREX_BROWSER_PROFILES_ROOT"

# The dedicated state directory that lives on the shared profiles volume.
STATE_DIR_NAME = ".kyrex-viewer-state"
LOCKS_SUBDIR = "locks"
RECORDS_SUBDIR = "records"

# TTL bounds. The manual viewer is short by design (one hour). Automation may
# legitimately run longer (a long task plus a human approval), so it gets a
# larger bound — but the TTL only bounds a STALE RECORD'S hint value: the
# authoritative liveness signal is always the held flock, which the holder
# releases in every path and the kernel releases on any death.
DEFAULT_TTL = 2700.0      # 45 minutes
MAX_TTL = 3600.0          # 1 hour — manual control is short by design
MIN_TTL = 60.0
AUTOMATION_MAX_TTL = 8 * 3600.0

# The two roles that contend for one profile lock.
KIND_MANUAL = "manual"
KIND_AUTOMATION = "automation"


class ManualControlActive(Exception):
    """The profile is under exclusive control; the caller must not proceed."""


class ViewerPathError(Exception):
    """A lock/record path would escape the shared state root (fail closed)."""


class ViewerSession:
    """A held, exclusive, TTL-bounded session for ``(owner, bot_id)``.

    Owning the open lock fd is the whole trick: the lock is held exactly as long
    as this object is alive. Dropping / exiting / crashing releases it in the
    kernel — there is nothing to "clean up" after a crash.
    """

    def __init__(self, owner: str, bot_id: str, *, record: dict, lock_fd: int,
                 root=None, profiles_root=None):
        self.owner = owner
        self.bot_id = bot_id
        self._record = record
        self._lock_fd = lock_fd
        self._root = root
        self._profiles_root = profiles_root

    def record(self) -> dict:
        return dict(self._record)

    def session_id(self) -> str:
        return str(self._record.get("session_id") or "")

    def kind(self) -> str:
        return str(self._record.get("kind") or "")

    def expires(self) -> float:
        """The Unix time at which this session's TTL makes its record stale."""
        try:
            return float(self._record.get("expires") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def release(self) -> None:
        """End the session: delete the record, close (free) the lock."""
        try:
            _record_path(self.owner, self.bot_id, self._root,
                         self._profiles_root).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — releasing must never raise
            pass
        finally:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(self._lock_fd)
                except OSError:
                    pass
                self._lock_fd = -1

    def __enter__(self) -> "ViewerSession":
        return self

    def __exit__(self, *_exc) -> None:
        if self._lock_fd != -1:
            self.release()


# ── Paths (containment-safe) ──────────────────────────────────────────

def _slug(value: str) -> str:
    """Sanitize an id into one safe path component (no separators, no ``..``)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = re.sub(r"\.{2,}", "_", cleaned)   # collapse any ".." run
    cleaned = cleaned.strip("._")
    return cleaned[:96] or "unbound"


def _ensure_dir(path: Path) -> Path:
    """Create *path* (and parents) and refuse a symlinked / non-dir target."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ViewerPathError(f"cannot create state directory {path}: {exc}")
    try:
        st = path.lstat()
    except OSError as exc:
        raise ViewerPathError(f"cannot stat state directory {path}: {exc}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ViewerPathError(
            f"state directory is not a real directory: {path}")
    return path


def _contained_child(base: Path, name: str) -> Path:
    """Return ``base/name``, refusing traversal or a symlink that escapes *base*.

    *name* must be a single, separator-free component (already slugged), so no
    ``..`` or path separator can appear. It is then resolved with ``realpath``
    and must remain inside *base* — which rejects a pre-planted symlink that
    points off the shared volume.
    """
    if (not name or name in (".", "..") or "/" in name or "\\" in name
            or os.sep in name or (os.altsep and os.altsep in name)):
        raise ViewerPathError(f"unsafe path component: {name!r}")
    base_real = Path(os.path.realpath(base))
    candidate = base_real / name
    resolved = Path(os.path.realpath(candidate))
    if resolved != base_real and base_real not in resolved.parents:
        raise ViewerPathError(f"path escapes the shared viewer state: {name!r}")
    return candidate


def state_dir(root: str | os.PathLike | None = None,
              profiles_root: str | os.PathLike | None = None) -> Path:
    """The shared state directory (same path in the agent and viewer containers).

    Explicit *root* > ``KYREX_VIEWER_STATE_DIR`` > ``<profiles_root>`` +
    ``.kyrex-viewer-state`` > ``<KYREX_BROWSER_PROFILES_ROOT>`` + that name > a
    temp fallback the current user can create. It deliberately NEVER defaults to
    ``/run``: /run is not shared across the two containers.
    """
    if root:
        chosen = Path(root)
    else:
        env = os.environ.get(STATE_DIR_ENV, "").strip()
        if env:
            chosen = Path(env)
        else:
            base = (profiles_root
                    or os.environ.get(PROFILES_ROOT_ENV, "").strip())
            if base:
                chosen = Path(base) / STATE_DIR_NAME
            else:
                chosen = Path(tempfile.gettempdir()) / STATE_DIR_NAME
    _ensure_dir(chosen)
    return chosen


def _record_path(owner: str, bot_id: str, root=None, profiles_root=None) -> Path:
    key = f"{_slug(owner)}__{_slug(bot_id)}"
    records = _ensure_dir(state_dir(root, profiles_root) / RECORDS_SUBDIR)
    return _contained_child(records, f"{key}.json")


def _lock_path(owner: str, bot_id: str, root=None, profiles_root=None) -> Path:
    key = f"{_slug(owner)}__{_slug(bot_id)}"
    locks = _ensure_dir(state_dir(root, profiles_root) / LOCKS_SUBDIR)
    return _contained_child(locks, f"{key}.lock")


# ── Low-level file primitives (symlink-proof) ─────────────────────────

def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    """``os.open`` that refuses to follow a symlink at *path* (O_NOFOLLOW)."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(path, flags | nofollow, mode)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise ViewerPathError(f"refusing to follow a symlink at {path}")
        raise


def _read_record(path: Path):
    try:
        fd = _open_nofollow(path, os.O_RDONLY)
    except (ViewerPathError, FileNotFoundError, OSError):
        return None
    try:
        with os.fdopen(fd, "r") as fh:
            data = json.loads(fh.read())
    except Exception:  # noqa: BLE001 — a torn/corrupt record is simply absent
        return None
    return data if isinstance(data, dict) else None


def _write_record(path: Path, record: dict) -> None:
    """Atomically write *record*: nofollow temp in the same dir, then replace."""
    parent = _ensure_dir(path.parent)
    tmp = parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    fd = _open_nofollow(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(record, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ── Core API ──────────────────────────────────────────────────────────

def _try_lock(owner: str, bot_id: str, root=None,
              profiles_root=None) -> "int | None":
    """Take the exclusive non-blocking flock. Returns the fd, or None.

    The lock is what makes this exclusive at kernel level: two acquires for the
    same profile — a viewer and an automation, or two of either — cannot both
    hold it.
    """
    path = _lock_path(owner, bot_id, root, profiles_root)
    fd = _open_nofollow(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return fd


def active(owner: str, bot_id: str, *, now=time.time, root=None,
           profiles_root=None) -> "dict | None":
    """The ACTIVE record for ``(owner, bot_id)``, or ``None``.

    A record counts only when BOTH hold: the TTL has not expired AND the lock is
    genuinely held (the holder process is alive). Anything else is reaped here —
    expiry, a crashed holder, a reboot — by deleting the record.
    """
    owner = str(owner or "").strip()
    bot_id = str(bot_id or "").strip()
    if not owner or not bot_id:
        return None
    path = _record_path(owner, bot_id, root, profiles_root)
    record = _read_record(path)
    if record is None:
        return None
    try:
        expiry = float(record.get("expires") or 0)
    except (TypeError, ValueError):
        expiry = 0.0
    if now() >= expiry:
        _unlink_quietly(path)
        return None
    # The lock is the truth: if the holder died, the record is dead too.
    probe_fd = _try_lock(owner, bot_id, root, profiles_root)
    if probe_fd is None:
        return record  # genuinely held — the session is live
    try:
        fcntl.flock(probe_fd, fcntl.LOCK_UN)
    finally:
        os.close(probe_fd)
    _unlink_quietly(path)
    return None


def is_any_active(*, now=time.time, root=None, kind=None,
                  profiles_root=None) -> "list[dict]":
    """Every live session on this host, optionally filtered by *kind*.

    ``kind=KIND_MANUAL`` is the agent's host-wide manual-control check (the
    owner watching ANY profile pauses automation as a defense in depth);
    ``kind=None`` returns every live session (manual and automation).
    """
    live = []
    records_dir = _ensure_dir(state_dir(root, profiles_root) / RECORDS_SUBDIR)
    for path in sorted(records_dir.glob("*.json")):
        stem = path.stem
        owner, _, bot = stem.partition("__")
        record = active(owner, bot, now=now, root=root,
                        profiles_root=profiles_root)
        if record is None:
            continue
        if kind is not None and record.get("kind") != kind:
            continue
        live.append(record)
    return live


def acquire(owner: str, bot_id: str, *, ttl: float = DEFAULT_TTL,
            kind: str = KIND_MANUAL, now=time.time, root=None,
            profiles_root=None, pid: int | None = None,
            session_id: str | None = None, meta: dict | None = None) -> ViewerSession:
    """Take ONE exclusive session for ``(owner, bot_id)``: flock + TTL record.

    *kind* is ``KIND_MANUAL`` (the viewer) or ``KIND_AUTOMATION`` (the agent).
    Both take the SAME lock, so whichever asks second is refused.
    """
    owner = str(owner or "").strip()
    bot_id = str(bot_id or "").strip()
    if not owner or not bot_id:
        raise ManualControlActive(
            "a browser session requires both an owner and a bot id")
    if existing := active(owner, bot_id, now=now, root=root,
                          profiles_root=profiles_root):
        raise ManualControlActive(
            "the profile is already under exclusive control for "
            f"owner={owner!r} bot={bot_id!r}: {existing.get('session_id', '')}")
    fd = _try_lock(owner, bot_id, root, profiles_root)
    if fd is None:
        raise ManualControlActive(
            "the profile is exclusively locked by a live session "
            f"(owner={owner!r} bot={bot_id!r})")
    max_ttl = AUTOMATION_MAX_TTL if kind == KIND_AUTOMATION else MAX_TTL
    ttl = min(max(float(ttl), MIN_TTL), max_ttl)
    started = now()
    record = {
        "session_id": session_id or secrets.token_hex(8),
        "owner": owner,
        "bot_id": bot_id,
        "kind": kind,
        "started": started,
        "expires": started + ttl,
        "pid": pid if pid is not None else os.getpid(),
        **(meta or {}),
    }
    path = _record_path(owner, bot_id, root, profiles_root)
    try:
        _write_record(path, record)
    except Exception:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise
    return ViewerSession(owner, bot_id, record=record, lock_fd=fd,
                         root=root, profiles_root=profiles_root)


def reap_expired(*, now=time.time, root=None, profiles_root=None) -> "list[str]":
    """Delete every dead record; return the reaped keys."""
    reaped = []
    records_dir = _ensure_dir(state_dir(root, profiles_root) / RECORDS_SUBDIR)
    for path in sorted(records_dir.glob("*.json")):
        stem = path.stem
        owner, _, bot = stem.partition("__")
        rec = active(owner, bot, now=now, root=root,
                     profiles_root=profiles_root)
        if rec is None:
            reaped.append(stem)
    return reaped


def cli_status_json(root=None, profiles_root=None) -> dict:
    live = is_any_active(root=root, profiles_root=profiles_root)
    return {"active": live}
