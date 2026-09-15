"""browser_hosts.py — Cloud-side registry for enrolled Browser Hosts.

This is the Cloud half of the Phase-2 secure Cloud <-> Browser Host channel.
It owns the durable IDENTITY and LIVENESS of a Browser Host and nothing else:
which hosts this owner has enrolled, whether each is reachable right now, and
which ``(owner, bot_id)`` pair each host serves. It never speaks the wire
protocol (that is ``browser_host_channel.py``) and never executes a browser
action.

Why a Host is not a Browser Session
-----------------------------------
A :mod:`browser_sessions` record is the durable identity of ONE Bot's browser
*computer* (a persistent profile). A Host is the MACHINE that runs those
computers: one self-hosted box may serve several Bots for one owner. The two
are related but distinct — a session is WHERE state lives, a host is WHERE it
runs — so they are stored and reasoned about separately.

Security model
--------------
* **Enrollment secret, never plaintext.** ``enroll_host`` mints (or accepts) a
  per-host secret and stores ONLY a Fernet-sealed blob of it, using the SAME
  key source and discipline as :mod:`browser_sessions` / :mod:`connectors`,
  domain-separated by a ``kyrex-browser-host:`` prefix so a host secret can
  never be decrypted by, or used to decrypt, any other sealed blob. With no
  secret configured, sealing fails closed — nothing is written.
* **Proof, not the secret, on the wire.** The host proves possession with
  ``HMAC_SHA256(secret, host_id + "." + nonce)``; the secret itself never
  travels, and :func:`public_view` never returns it in any form.
* **Liveness is fail-closed.** A host is ``available`` only while it is
  authenticated AND its last heartbeat is within the timeout. A stale or
  never-connected host reports ``unavailable`` / ``offline`` and the routing
  layer refuses to send it work.
* **Owner-scoped.** A host belongs to exactly one owner; the binding table is
  keyed by owner, so one owner can never route work to another's host.
* **CDP URLs are redacted.** Raw Chromium DevTools endpoints are scrubbed from
  every view, audit line, and log string this module emits; the host's CDP
  socket is never part of this protocol at all.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from paths import data_dir

# ── States ────────────────────────────────────────────────────────────

STATE_OFFLINE = "offline"          # enrolled, not currently connected
STATE_ONLINE = "online"            # connected and heart-beating
STATE_UNAVAILABLE = "unavailable"  # was connected; heartbeat went stale
STATE_REVOKED = "revoked"          # terminal: enrollment withdrawn

STATES = (STATE_OFFLINE, STATE_ONLINE, STATE_UNAVAILABLE, STATE_REVOKED)

# Only a revoked host is permanently dead. ``offline`` and ``unavailable`` are
# recoverable: a heartbeat moves them straight back to ``online``.
TERMINAL_STATES = frozenset({STATE_REVOKED})

DEFAULT_HEARTBEAT_INTERVAL = 15    # seconds the host should beat at
DEFAULT_HEARTBEAT_TIMEOUT = 45     # seconds without a beat -> unavailable

_HOST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_LOCK = threading.RLock()


class HostError(Exception):
    """A host registry operation was refused (fail closed)."""


class HostUnavailable(HostError):
    """The target host is not reachable; work must not be sent to it."""


# ── Redaction (CDP URLs + secrets) ────────────────────────────────────

# A raw CDP endpoint is a credential-adjacent secret: it grants full control of
# the browser. It must never appear in a view, audit line, or log. These
# patterns scrub the websocket form and the HTTP discovery form.
_CDP_URL_RE = re.compile(
    r"(?i)\bwss?://[^\s\"'<>]*?(?:devtools|/json(?:/version)?)[^\s\"'<>]*"
)
_CDP_HTTP_URL_RE = re.compile(
    r"(?i)\bhttps?://[^\s\"'<>]*?(?:/devtools/|/json(?:/version)?)[^\s\"'<>]*"
)
_SECRET_KV_RE = re.compile(
    r"(?i)\b(secret|token|password|passwd|api[_-]?key|credential|authorization)"
    r"\b\s*[:=]\s*[^\s,;]+"
)


def redact_text(text) -> str:
    """Scrub CDP endpoints and secret-shaped key/values from *text*."""
    if text is None:
        return ""
    out = str(text)
    out = _CDP_URL_RE.sub("[redacted-cdp]", out)
    out = _CDP_HTTP_URL_RE.sub("[redacted-cdp]", out)
    out = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=[redacted]", out)
    return out


def redact_obj(obj):
    """Recursively redact a JSON-shaped object (never returns ``None``)."""
    if isinstance(obj, dict):
        return {str(k): redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


# ── Sealing (enrollment secret at rest) ───────────────────────────────

def _box():
    """A Fernet for host-secret blobs, or a fail-closed error.

    Same key source as browser sessions / connector tokens, domain-separated by
    a prefix so no sealed blob is interchangeable with another subsystem's.
    Imported lazily so this module stays importable where ``cryptography`` is
    absent — the failure is deferred to the moment a secret would be written.
    """
    secret = (
        os.environ.get("WEB_SESSION_SECRET")
        or os.environ.get("KYREX_PROVIDER_SECRETS_KEY")
    )
    if not secret:
        raise HostError("browser host secret encryption is not configured")
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:  # pragma: no cover - dependency present in prod
        raise HostError(f"browser host encryption unavailable: {exc}")
    key = base64.urlsafe_b64encode(
        hashlib.sha256(("kyrex-browser-host:" + secret).encode()).digest()
    )
    return Fernet(key)


def seal_secret(secret: str | None) -> str | None:
    """Seal an enrollment secret into an opaque blob (``None`` stays ``None``)."""
    if not secret:
        return None
    return _box().encrypt(str(secret).encode()).decode()


def unseal_secret(blob: str | None) -> str:
    """Decrypt a sealed secret, or ``""`` when absent/undecryptable."""
    if not blob:
        return ""
    try:
        from cryptography.fernet import InvalidToken
    except Exception:  # pragma: no cover
        InvalidToken = Exception  # type: ignore[assignment]
    try:
        return _box().decrypt(str(blob).encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        return ""


def proof_for(secret: str, host_id: str, nonce: str) -> str:
    """The HMAC a host presents to prove it holds *secret* (never the secret)."""
    return hmac.new(
        str(secret).encode(), f"{host_id}.{nonce}".encode(), hashlib.sha256
    ).hexdigest()


# ── Paths / small helpers ─────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def heartbeat_interval() -> int:
    return _env_int("KYREX_HOST_HEARTBEAT_INTERVAL", DEFAULT_HEARTBEAT_INTERVAL)


def heartbeat_timeout() -> int:
    return _env_int("KYREX_HOST_HEARTBEAT_TIMEOUT", DEFAULT_HEARTBEAT_TIMEOUT)


def _root() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _registry_path() -> Path:
    return _root() / "browser_hosts.json"


def _read() -> dict:
    path = _registry_path()
    if not path.exists():
        return {"hosts": {}, "bindings": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # A corrupt registry is never silently treated as empty: that would
        # silently drop every enrollment and un-bind live hosts.
        raise HostError(f"browser host registry is unreadable: {exc}")
    if not isinstance(data, dict):
        return {"hosts": {}, "bindings": {}}
    data.setdefault("hosts", {})
    data.setdefault("bindings", {})
    return data


def _write(data: dict) -> None:
    path = _registry_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    tmp.replace(path)


def _require(owner, host_id) -> tuple[str, str]:
    owner = str(owner or "").strip()
    host_id = str(host_id or "").strip()
    if not owner:
        raise HostError("a browser host requires an owner")
    if not _HOST_ID_RE.match(host_id):
        raise HostError(
            "a browser host id must be 1-64 chars of [A-Za-z0-9_-]"
        )
    return owner, host_id


# ── Public representation ─────────────────────────────────────────────

@dataclass
class BrowserHost:
    """A decrypted, in-memory view of one enrolled host record."""

    host_id: str
    owner: str
    name: str = ""
    state: str = STATE_OFFLINE
    allowlist: list = field(default_factory=list)
    registered_at: float = 0.0
    connected_at: float = 0.0
    last_seen_at: float = 0.0
    has_secret: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def is_available(self, now: float | None = None) -> bool:
        """True only while authenticated AND within the heartbeat timeout."""
        now = _now() if now is None else now
        if self.state != STATE_ONLINE:
            return False
        return (now - float(self.last_seen_at or 0.0)) < heartbeat_timeout()

    def effective_state(self, now: float | None = None) -> str:
        """The state a caller should act on (stale online -> unavailable)."""
        now = _now() if now is None else now
        if self.state == STATE_ONLINE and not self.is_available(now):
            return STATE_UNAVAILABLE
        return self.state


def _view(rec: dict) -> BrowserHost:
    return BrowserHost(
        host_id=str(rec.get("host_id") or ""),
        owner=str(rec.get("owner") or ""),
        name=str(rec.get("name") or ""),
        state=str(rec.get("state") or STATE_OFFLINE),
        allowlist=list(rec.get("allowlist") or []),
        registered_at=float(rec.get("registered_at") or 0.0),
        connected_at=float(rec.get("connected_at") or 0.0),
        last_seen_at=float(rec.get("last_seen_at") or 0.0),
        has_secret=bool(rec.get("sealed")),
    )


def public_view(host: BrowserHost | dict, *, now: float | None = None) -> dict:
    """The NON-SECRET view of a host — never the secret, never a CDP URL."""
    if isinstance(host, dict):
        host = _view(host)
    now = _now() if now is None else now
    return {
        "host_id": host.host_id,
        "owner": host.owner,
        "name": host.name,
        "state": host.effective_state(now),
        "allowlist": [redact_text(a) for a in host.allowlist],
        "available": host.is_available(now),
        "registered_at": host.registered_at,
        "connected_at": host.connected_at,
        "last_seen_at": host.last_seen_at,
        "has_credential": host.has_secret,
    }


# ── Enrollment / revocation ───────────────────────────────────────────

def _validated_allowlist(allowlist) -> list[str]:
    if allowlist is None:
        return []
    if not isinstance(allowlist, (list, tuple)):
        raise HostError("a host allowlist must be a list of hostnames")
    out: list[str] = []
    for entry in allowlist:
        if not isinstance(entry, str):
            continue
        host = entry.strip().lower()
        if "://" in host or "/" in host or " " in host:
            raise HostError(f"invalid host allowlist entry {entry!r}")
        if host and host not in out:
            out.append(host)
    return out


def enroll_host(
    owner,
    host_id,
    *,
    name: str = "",
    allowlist=None,
    secret: str | None = None,
    now: float | None = None,
) -> dict:
    """Register (or update) a host for *owner*.

    Returns ``{"host": <public_view>, "secret": <plaintext ONCE>}``. The
    plaintext secret is returned ONLY when this call mints a new one, so the
    caller can hand it to the host out of band; it is never returned again and
    never stored in the clear. Re-enrolling an existing host without a new
    secret preserves the old one (so a live host stays connected).

    Fail closed: a host id that already belongs to ANOTHER owner is refused,
    and enrolling with no configured encryption key raises rather than writing
    a plaintext secret.
    """
    owner, host_id = _require(owner, host_id)
    now = _now() if now is None else now
    name = str(name or "").strip()[:120]
    allow = _validated_allowlist(allowlist)

    explicit = secret is not None

    with _LOCK:
        data = _read()
        hosts = data["hosts"]
        existing = hosts.get(host_id)
        if existing is not None and str(existing.get("owner")) != owner:
            raise HostError(
                f"host id {host_id!r} is enrolled to another owner"
            )

        sealed = existing.get("sealed") if existing else None
        returned_secret = None
        if explicit:
            # An explicit secret ROTATES the enrollment secret.
            sealed = seal_secret(str(secret))  # may raise (fail closed)
        elif existing is None or not sealed:
            # A brand-new host (or one with no usable secret) mints one, which
            # is returned ONCE so the caller can hand it to the host.
            returned_secret = secrets.token_urlsafe(32)
            sealed = seal_secret(returned_secret)  # may raise (fail closed)

        if existing is None:
            hosts[host_id] = {
                "host_id": host_id,
                "owner": owner,
                "name": name,
                "state": STATE_OFFLINE,
                "allowlist": allow,
                "sealed": sealed,
                "registered_at": now,
                "connected_at": 0.0,
                "last_seen_at": 0.0,
            }
        else:
            existing["name"] = name or existing.get("name") or ""
            existing["allowlist"] = allow
            existing["sealed"] = sealed
            if existing.get("state") == STATE_REVOKED:
                existing["state"] = STATE_OFFLINE
        _write(data)
        view = public_view(hosts[host_id], now=now)
    return {"host": view, "secret": returned_secret}


def revoke_host(owner, host_id) -> bool:
    """Withdraw a host's enrollment (terminal). Returns True on change."""
    owner, host_id = _require(owner, host_id)
    with _LOCK:
        data = _read()
        rec = data["hosts"].get(host_id)
        if rec is None or str(rec.get("owner")) != owner:
            return False
        if rec.get("state") == STATE_REVOKED:
            return False
        rec["state"] = STATE_REVOKED
        rec["sealed"] = None            # the secret no longer exists on disk
        # Drop every binding that pointed at it, so nothing routes to it.
        for owner_key in list(data["bindings"].keys()):
            binds = data["bindings"][owner_key]
            for bot_id in [b for b, h in binds.items() if h == host_id]:
                binds.pop(bot_id, None)
        _write(data)
        return True


# ── Authentication ────────────────────────────────────────────────────

def verify_proof(host_id, nonce, proof, *, now: float | None = None) -> bool:
    """Constant-time check of a host's possession proof.

    Returns ``False`` for an unknown/revoked host, an unreadable secret, an
    empty nonce, or a mismatched proof. Never raises for a bad credential.
    """
    host_id = str(host_id or "").strip()
    nonce = str(nonce or "")
    proof = str(proof or "")
    if not host_id or not nonce or not proof:
        return False
    with _LOCK:
        rec = _read()["hosts"].get(host_id)
    if rec is None or rec.get("state") == STATE_REVOKED:
        return False
    secret = unseal_secret(rec.get("sealed"))
    if not secret:
        return False
    expected = proof_for(secret, host_id, nonce)
    return hmac.compare_digest(expected, proof)


# ── Liveness ──────────────────────────────────────────────────────────

def get_host(host_id, *, now: float | None = None) -> BrowserHost | None:
    """Return a host view by id (or ``None``). Read-only."""
    host_id = str(host_id or "").strip()
    if not host_id:
        return None
    with _LOCK:
        rec = _read()["hosts"].get(host_id)
        return _view(rec) if rec is not None else None


def _record(host_id: str) -> dict | None:
    with _LOCK:
        return _read()["hosts"].get(host_id)


def _set_state(host_id: str, state: str, now: float, *, touch: bool = False) -> dict | None:
    with _LOCK:
        data = _read()
        rec = data["hosts"].get(host_id)
        if rec is None or rec.get("state") == STATE_REVOKED:
            return None
        rec["state"] = state
        if touch:
            rec["last_seen_at"] = now
        if state == STATE_ONLINE and not rec.get("connected_at"):
            rec["connected_at"] = now
        _write(data)
        return public_view(rec, now=now)


def mark_online(host_id, *, now: float | None = None) -> dict | None:
    """Record a successful authentication/heartbeat (host is reachable)."""
    return _set_state(str(host_id or "").strip(), STATE_ONLINE,
                      _now() if now is None else now, touch=True)


def heartbeat(host_id, *, now: float | None = None, load=None) -> dict | None:
    """Record one heartbeat. Any live beat restores ``online``."""
    return mark_online(host_id, now=now)


def mark_unavailable(host_id, *, now: float | None = None) -> dict | None:
    """Force a host ``unavailable`` (e.g. its socket dropped)."""
    return _set_state(str(host_id or "").strip(), STATE_UNAVAILABLE,
                      _now() if now is None else now)


def mark_offline(host_id, *, now: float | None = None) -> dict | None:
    """Return a host to ``offline`` (never authenticated this run)."""
    return _set_state(str(host_id or "").strip(), STATE_OFFLINE,
                      _now() if now is None else now)


def sweep_stale(*, now: float | None = None) -> list[dict]:
    """Sweep online hosts whose heartbeat went stale into ``unavailable``.

    Returns the swept hosts as public views. Idempotent for a given ``now``.
    """
    now = _now() if now is None else now
    timeout = heartbeat_timeout()
    swept: list[dict] = []
    with _LOCK:
        data = _read()
        changed = False
        for rec in data["hosts"].values():
            if rec.get("state") != STATE_ONLINE:
                continue
            if (now - float(rec.get("last_seen_at") or 0.0)) >= timeout:
                rec["state"] = STATE_UNAVAILABLE
                swept.append(public_view(rec, now=now))
                changed = True
        if changed:
            _write(data)
    return swept


def list_hosts(owner, *, now: float | None = None) -> list[dict]:
    """Owner-scoped host views (never another owner's hosts)."""
    owner = str(owner or "").strip()
    if not owner:
        return []
    with _LOCK:
        hosts = list(_read()["hosts"].values())
    out = [
        public_view(r, now=now) for r in hosts
        if str(r.get("owner") or "").strip() == owner
        and r.get("state") != STATE_REVOKED
    ]
    return sorted(out, key=lambda h: str(h.get("host_id") or ""))


# ── Bindings (which host serves which (owner, bot_id)) ────────────────

def bind_bot(owner, bot_id, host_id, *, now: float | None = None) -> dict:
    """Bind ``(owner, bot_id)`` to one of the owner's hosts.

    Fail closed: a foreign/unknown/revoked host is refused, and the host must
    belong to the SAME owner — a Bot can never be routed to another owner's
    machine.
    """
    owner = str(owner or "").strip()
    bot_id = str(bot_id or "").strip()
    host_id = str(host_id or "").strip()
    if not owner or not bot_id:
        raise HostError("a host binding requires an owner and a bot id")
    rec = _record(host_id)
    if rec is None:
        raise HostError(f"unknown host {host_id!r}")
    if str(rec.get("owner") or "").strip() != owner:
        raise HostError(f"host {host_id!r} is not owned by you")
    if rec.get("state") == STATE_REVOKED:
        raise HostError(f"host {host_id!r} is revoked")
    with _LOCK:
        data = _read()
        data["bindings"].setdefault(owner, {})[bot_id] = host_id
        _write(data)
    return {"owner": owner, "bot_id": bot_id, "host_id": host_id}


def unbind_bot(owner, bot_id) -> bool:
    """Remove a binding. Returns True when one existed."""
    owner = str(owner or "").strip()
    bot_id = str(bot_id or "").strip()
    with _LOCK:
        data = _read()
        binds = data["bindings"].get(owner, {})
        if bot_id not in binds:
            return False
        binds.pop(bot_id, None)
        _write(data)
        return True


def host_for(owner, bot_id=None, *, now: float | None = None) -> BrowserHost | None:
    """The host that serves ``(owner, bot_id)``, or the owner's sole host.

    Fail closed and never ambiguous: an explicit binding wins; with no binding,
    the owner's ONE non-revoked host is used; two-or-more candidates yield
    ``None`` (the caller must bind explicitly rather than guess).
    """
    owner = str(owner or "").strip()
    if not owner:
        return None
    with _LOCK:
        data = _read()
        hosts = data["hosts"]
        binds = data["bindings"].get(owner, {})
        if bot_id:
            hid = binds.get(str(bot_id).strip())
            rec = hosts.get(hid) if hid else None
            if rec is not None and rec.get("state") != STATE_REVOKED:
                return _view(rec)
            return None
        owned = [
            r for r in hosts.values()
            if str(r.get("owner") or "").strip() == owner
            and r.get("state") != STATE_REVOKED
        ]
        if len(owned) == 1:
            return _view(owned[0])
        return None


def binding_for(owner, bot_id) -> str:
    """The bound host id for ``(owner, bot_id)``, or ``""``."""
    owner = str(owner or "").strip()
    bot_id = str(bot_id or "").strip()
    if not owner or not bot_id:
        return ""
    with _LOCK:
        return str(_read()["bindings"].get(owner, {}).get(bot_id) or "")
