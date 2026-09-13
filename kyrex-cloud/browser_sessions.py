"""browser_sessions.py — persistent, managed Browser Operator sessions.

A managed browser session is the durable identity of ONE Bot's isolated
browser *computer*, keyed by ``(owner, bot_id)``. It is the difference
between "spawn a browser, run actions, throw it away" and "this Bot has a
browser that survives the Chat tab being closed".

Lifecycle
---------
``starting``      a fresh session record exists; the browser is not up yet.
``connected``     the browser is live and owned by this (owner, bot) pair.
``disconnected``  the UI detached; the session (and any parked approval)
                  survives and is reconnectable.
``expired``       the retention deadline passed; the record is dead but kept
                  for the retention window so a reconnect can explain itself.
``ended``         explicitly ended (or superseded). Terminal.

Only a live session (``starting`` / ``connected`` / ``disconnected`` whose
``expires_at`` is in the future) may be reused. Every other transition is
refused with :class:`SessionError` — the state machine is the contract, not a
label.

Isolation
---------
The session key is ``(owner, bot_id)``. Two owners of the same Bot, or two
Bots of the same owner, resolve to different records AND different on-disk
session directories, so neither the browser profile nor the sealed metadata
can be shared. There is exactly one live session per key.

Sealed metadata
---------------
The session token / credentials live in a Fernet-sealed ``metadata`` blob;
the index file never contains them in the clear. Key derivation mirrors
``web/backend/provider_profiles.py`` (``WEB_SESSION_SECRET`` or
``KYREX_PROVIDER_SECRETS_KEY``), domain-separated so a provider-profile key
is never usable as a browser-session key. With no secret configured, sealing
fails closed — no plaintext credential is ever written.

What is NOT here
----------------
This module never executes a browser action. It manages lifecycle only: the
allowlist, approval protocol, Rift containment and the action vocabulary all
remain owned by ``browser_operator.py`` / ``serve.py``. The API surface
(:func:`public_view`) never returns the sealed token, so a session can be
inspected and reconnected without a credential ever leaving the server.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from paths import data_dir

# ── States ────────────────────────────────────────────────────────────

STATE_STARTING = "starting"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_EXPIRED = "expired"
STATE_ENDED = "ended"

STATES = (
    STATE_STARTING,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    STATE_EXPIRED,
    STATE_ENDED,
)

# A session in one of these states whose deadline has not passed can be
# reconnected. Everything else is dead and must be superseded.
LIVE_STATES = frozenset({STATE_STARTING, STATE_CONNECTED, STATE_DISCONNECTED})

# Kept for the retention window, never reusable.
TERMINAL_STATES = frozenset({STATE_EXPIRED, STATE_ENDED})

# The ONLY legal transitions. ``None`` is the pre-creation state, so a new
# record may enter only ``starting``. Any transition not listed here raises.
_ALLOWED_TRANSITIONS: dict[str | None, frozenset] = {
    None: frozenset({STATE_STARTING}),
    STATE_STARTING: frozenset(
        {STATE_CONNECTED, STATE_DISCONNECTED, STATE_EXPIRED, STATE_ENDED}
    ),
    STATE_CONNECTED: frozenset(
        {STATE_DISCONNECTED, STATE_EXPIRED, STATE_ENDED}
    ),
    STATE_DISCONNECTED: frozenset(
        {STATE_CONNECTED, STATE_EXPIRED, STATE_ENDED}
    ),
    STATE_EXPIRED: frozenset({STATE_ENDED}),
    STATE_ENDED: frozenset(),
}

DEFAULT_TTL = 3600          # seconds a session stays reconnectable after use
DEFAULT_RETENTION = 86400   # seconds a terminal record is kept before removal

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_LOCK = threading.RLock()


class SessionError(Exception):
    """The requested session operation is not permitted by the state machine."""


# ── Environment / paths ───────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _ttl() -> int:
    return _env_int("KYREX_BROWSER_SESSION_TTL", DEFAULT_TTL)


def _retention() -> int:
    return _env_int("KYREX_BROWSER_SESSION_RETENTION", DEFAULT_RETENTION)


def _root() -> Path:
    """The browser-session storage root (re-resolved per call for tests)."""
    path = data_dir() / "browser_sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _index_path() -> Path:
    return _root() / "index.json"


def session_dir(session_id: str) -> Path:
    """On-disk directory for one session's persistent browser profile."""
    safe = _slug(session_id)
    return _root() / f"sess-{safe}"


def _slug(value, default: str = "unbound") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = re.sub(r"\.{2,}", "_", cleaned).strip("._")
    return cleaned[:96] or default


# ── Sealing ───────────────────────────────────────────────────────────

def _box():
    """A Fernet for the session-metadata blob, or a fail-closed error.

    Same key source as provider profiles, domain-separated by a prefix so a
    provider-profile key can never decrypt a browser-session blob (or vice
    versa). Imported lazily so this module stays importable where
    ``cryptography`` is absent — the failure is deferred to the moment a
    secret would otherwise be written.
    """
    secret = (
        os.environ.get("WEB_SESSION_SECRET")
        or os.environ.get("KYREX_PROVIDER_SECRETS_KEY")
    )
    if not secret:
        raise SessionError("browser session metadata encryption is not configured")
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:  # pragma: no cover - dependency present in prod
        raise SessionError(f"browser session encryption unavailable: {exc}")
    key = base64.urlsafe_b64encode(
        hashlib.sha256(("kyrex-browser-session:" + secret).encode()).digest()
    )
    return Fernet(key)


def seal_metadata(metadata: dict | None) -> str | None:
    """Seal *metadata* into an opaque token (``None`` stays ``None``)."""
    if not metadata:
        return None
    payload = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return _box().encrypt(payload.encode()).decode()


def unseal_metadata(blob: str | None) -> dict:
    """Decrypt a sealed blob, or ``{}`` when absent/undecryptable.

    An unreadable blob (rotated key, tampering) yields ``{}`` rather than
    raising: the session simply has no recoverable metadata, which is the
    safe direction — the metadata is never a precondition for lifecycle.
    """
    if not blob:
        return {}
    try:
        from cryptography.fernet import InvalidToken
    except Exception:  # pragma: no cover
        InvalidToken = Exception  # type: ignore[assignment]
    try:
        raw = _box().decrypt(str(blob).encode()).decode()
        parsed = json.loads(raw)
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── Public representation ─────────────────────────────────────────────

@dataclass
class BrowserSession:
    """A decrypted, in-memory view of one session record.

    ``metadata`` holds the unsealed credential material for INTERNAL use.
    It must never cross the API boundary — :func:`public_view` is the only
    shape a client may see.
    """

    session_id: str
    owner: str
    bot_id: str
    state: str
    driver: str = "playwright"
    endpoint: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    last_seen_at: float = 0.0
    expires_at: float = 0.0
    metadata: dict = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def effective_state(self, now: float | None = None) -> str:
        """The state a caller should act on (past-deadline live => expired)."""
        now = _now() if now is None else now
        if self.state in LIVE_STATES and now >= self.expires_at:
            return STATE_EXPIRED
        return self.state

    def is_reusable(self, now: float | None = None) -> bool:
        now = _now() if now is None else now
        return self.state in LIVE_STATES and now < self.expires_at

    def has_sealed_metadata(self) -> bool:
        return bool(self.metadata)


# ── Index read/write ──────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _read_index() -> list[dict]:
    path = _index_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # A corrupt index is never silently treated as empty: that would
        # orphan live session directories and silently drop isolation.
        raise SessionError(f"browser session index is unreadable: {exc}")
    return raw if isinstance(raw, list) else []


def _write_index(records: list[dict]) -> None:
    path = _index_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, sort_keys=True))
    tmp.replace(path)


def _find(records: list[dict], owner: str, bot_id: str) -> dict | None:
    """The record a caller should act on for ``(owner, bot_id)``.

    Prefers a live record (there is at most one by construction); otherwise
    the newest record, so a dead session can be reported and superseded.
    """
    matches = [
        r for r in records
        if r.get("owner") == owner and r.get("bot_id") == bot_id
    ]
    if not matches:
        return None
    live = [r for r in matches if r.get("state") in LIVE_STATES]
    pool = live or matches
    return max(pool, key=lambda r: float(r.get("updated_at") or 0.0))


def _transition(rec: dict, new_state: str, now: float) -> dict:
    current = rec.get("state")
    allowed = _ALLOWED_TRANSITIONS.get(current)
    if allowed is None or new_state not in allowed:
        raise SessionError(
            f"invalid browser session transition {current!r} -> {new_state!r}"
        )
    rec["state"] = new_state
    rec["updated_at"] = now
    return rec


def _new_record(
    owner: str,
    bot_id: str,
    driver: str,
    endpoint: str,
    metadata: dict | None,
    now: float,
    ttl: int,
) -> dict:
    return {
        "session_id": uuid.uuid4().hex,
        "owner": owner,
        "bot_id": bot_id,
        "state": STATE_STARTING,
        "driver": str(driver or "playwright"),
        "endpoint": str(endpoint or ""),
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
        "expires_at": now + max(int(ttl), 0),
        "sealed": seal_metadata(metadata),
    }


def _view(rec: dict) -> BrowserSession:
    return BrowserSession(
        session_id=str(rec.get("session_id") or ""),
        owner=str(rec.get("owner") or ""),
        bot_id=str(rec.get("bot_id") or ""),
        state=str(rec.get("state") or STATE_STARTING),
        driver=str(rec.get("driver") or "playwright"),
        endpoint=str(rec.get("endpoint") or ""),
        created_at=float(rec.get("created_at") or 0.0),
        updated_at=float(rec.get("updated_at") or 0.0),
        last_seen_at=float(rec.get("last_seen_at") or 0.0),
        expires_at=float(rec.get("expires_at") or 0.0),
        metadata=unseal_metadata(rec.get("sealed")),
    )


def _require_key(owner, bot_id) -> tuple[str, str]:
    owner = str(owner or "").strip()
    bot_id = str(bot_id or "").strip()
    if not owner or not bot_id:
        # A session always belongs to exactly one owner AND one bot; without
        # both there is no isolation key and no session may exist.
        raise SessionError("a browser session requires both an owner and a bot id")
    return owner, bot_id


# ── Lifecycle API ─────────────────────────────────────────────────────

def get_session(owner, bot_id, *, now: float | None = None) -> BrowserSession | None:
    """Return the current session view for ``(owner, bot_id)`` or ``None``.

    Read-only: it never mutates the record (a past-deadline session reports
    ``effective_state() == "expired"`` without a write). Use
    :func:`expire_stale` to persist that sweep.
    """
    owner, bot_id = _require_key(owner, bot_id)
    with _LOCK:
        rec = _find(_read_index(), owner, bot_id)
        return _view(rec) if rec is not None else None


def create_session(
    owner,
    bot_id,
    *,
    driver: str = "playwright",
    endpoint: str = "",
    metadata: dict | None = None,
    now: float | None = None,
    ttl: int | None = None,
) -> BrowserSession:
    """Create a brand-new ``starting`` session, superseding any existing one.

    The single-live-session-per-key invariant is enforced here: an existing
    live record is transitioned to ``ended`` before the new one is written.
    """
    owner, bot_id = _require_key(owner, bot_id)
    now = _now() if now is None else now
    ttl = _ttl() if ttl is None else ttl
    with _LOCK:
        records = _read_index()
        existing = _find(records, owner, bot_id)
        if existing is not None and existing.get("state") in LIVE_STATES:
            _transition(existing, STATE_ENDED, now)
        rec = _new_record(owner, bot_id, driver, endpoint, metadata, now, ttl)
        records.append(rec)
        _write_index(records)
        return _view(rec)


def get_or_create(
    owner,
    bot_id,
    *,
    driver: str = "playwright",
    endpoint: str = "",
    metadata: dict | None = None,
    now: float | None = None,
    ttl: int | None = None,
) -> tuple[BrowserSession, bool]:
    """Reuse a live session for ``(owner, bot_id)`` or create a fresh one.

    Returns ``(session, reused)``. A reused session is moved to
    ``connected`` with a refreshed deadline (the act of reconnecting IS a
    use). A dead session (past deadline, or terminal) is retired to
    ``expired`` and a fresh ``starting`` session supersedes it — the caller
    never silently inherits a dead session's state.
    """
    owner, bot_id = _require_key(owner, bot_id)
    now = _now() if now is None else now
    ttl = _ttl() if ttl is None else ttl
    with _LOCK:
        records = _read_index()
        existing = _find(records, owner, bot_id)
        if existing is not None:
            state = existing.get("state")
            if state in LIVE_STATES and now < float(existing.get("expires_at") or 0.0):
                _transition(existing, STATE_CONNECTED, now)
                existing["last_seen_at"] = now
                existing["expires_at"] = now + max(int(ttl), 0)
                if metadata:
                    existing["sealed"] = seal_metadata(metadata)
                _write_index(records)
                return _view(existing), True
            # Dead but not yet swept: retire it so the new session is the
            # only live record for this key.
            if state in LIVE_STATES:
                _transition(existing, STATE_EXPIRED, now)
        rec = _new_record(owner, bot_id, driver, endpoint, metadata, now, ttl)
        records.append(rec)
        _write_index(records)
        return _view(rec), False


def reconnect(owner, bot_id, **kwargs) -> BrowserSession:
    """:func:`get_or_create` returning only the session (UI reconnect entry)."""
    session, _ = get_or_create(owner, bot_id, **kwargs)
    return session


def mark_connected(owner, bot_id, *, now: float | None = None) -> BrowserSession | None:
    """Transition the live session to ``connected`` (no-op when absent)."""
    return _mark(owner, bot_id, STATE_CONNECTED, now)


def mark_disconnected(owner, bot_id, *, now: float | None = None) -> BrowserSession | None:
    """Detach the UI without ending the session.

    This is the state a session occupies while its Chat/IDE surface is
    closed: the browser, the record and any parked approval all survive, and
    :func:`get_or_create` reconnects to exactly this session.
    """
    return _mark(owner, bot_id, STATE_DISCONNECTED, now)


def _mark(owner, bot_id, new_state, now) -> BrowserSession | None:
    owner, bot_id = _require_key(owner, bot_id)
    now = _now() if now is None else now
    with _LOCK:
        records = _read_index()
        rec = _find(records, owner, bot_id)
        if rec is None:
            return None
        if rec.get("state") in LIVE_STATES and now < float(rec.get("expires_at") or 0.0):
            _transition(rec, new_state, now)
            rec["last_seen_at"] = now
            _write_index(records)
            return _view(rec)
        return _view(rec)


def end_session(owner, bot_id, *, now: float | None = None) -> bool:
    """Explicitly end the live session. Returns False when there was none."""
    owner, bot_id = _require_key(owner, bot_id)
    now = _now() if now is None else now
    with _LOCK:
        records = _read_index()
        rec = _find(records, owner, bot_id)
        if rec is None or rec.get("state") in TERMINAL_STATES:
            return False
        _transition(rec, STATE_ENDED, now)
        _write_index(records)
        return True


def expire_stale(*, now: float | None = None) -> list[dict]:
    """Sweep every live session whose deadline passed into ``expired``.

    Returns the swept records as public views (never sealed material).
    Idempotent: a second call with the same ``now`` finds nothing.
    """
    now = _now() if now is None else now
    swept: list[dict] = []
    with _LOCK:
        records = _read_index()
        changed = False
        for rec in records:
            if (rec.get("state") in LIVE_STATES
                    and now >= float(rec.get("expires_at") or 0.0)):
                _transition(rec, STATE_EXPIRED, now)
                swept.append(public_view(_view(rec)))
                changed = True
        if changed:
            _write_index(records)
    return swept


def cleanup(*, now: float | None = None, retention: int | None = None) -> list[str]:
    """Remove terminal records past the retention window and their dirs.

    Expires first so the sweep and the purge agree on one clock. Returns the
    removed session ids. A record is removed only once it has been terminal
    for ``retention`` seconds, so a reconnect can still explain a nearby
    expiry before it disappears.
    """
    now = _now() if now is None else now
    retention = _retention() if retention is None else retention
    expire_stale(now=now)
    removed: list[str] = []
    with _LOCK:
        records = _read_index()
        keep: list[dict] = []
        for rec in records:
            is_terminal = rec.get("state") in TERMINAL_STATES
            age = now - float(rec.get("updated_at") or 0.0)
            if is_terminal and age >= retention:
                removed.append(str(rec.get("session_id") or ""))
                _remove_dir(str(rec.get("session_id") or ""))
                continue
            keep.append(rec)
        if removed:
            _write_index(keep)
    return removed


def _remove_dir(session_id: str) -> None:
    """Best-effort removal of one session directory (never raises)."""
    if not session_id or not _SESSION_ID_RE.match(session_id):
        return
    import shutil

    try:
        shutil.rmtree(session_dir(session_id))
    except FileNotFoundError:
        pass
    except OSError:
        pass


# ── Views / env ───────────────────────────────────────────────────────

def public_view(session: BrowserSession | dict, *, now: float | None = None) -> dict:
    """The NON-SECRET view of a session — the only shape a client may see.

    Exposes lifecycle/routing metadata plus ``has_credential`` (a boolean).
    The sealed token, and any decrypted ``metadata`` value, are never
    included.
    """
    if isinstance(session, dict):
        session = _view(session)
    now = _now() if now is None else now
    return {
        "session_id": session.session_id,
        "owner": session.owner,
        "bot_id": session.bot_id,
        "state": session.effective_state(now),
        "driver": session.driver,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "last_seen_at": session.last_seen_at,
        "expires_at": session.expires_at,
        "reusable": session.is_reusable(now),
        "has_credential": session.has_sealed_metadata(),
    }


def session_env(session: BrowserSession) -> dict:
    """Environment the host hands the Browser Operator for this session.

    The operator uses ``KYREX_BROWSER_SESSION_DIR`` as its persistent
    user-data directory (that persistence IS the reconnect) and echoes
    ``KYREX_BROWSER_SESSION_ID`` for correlation. Neither value is a secret:
    the credential never leaves the sealed blob.
    """
    return {
        "KYREX_BROWSER_SESSION_ID": session.session_id,
        "KYREX_BROWSER_SESSION_DIR": str(session_dir(session.session_id)),
    }
