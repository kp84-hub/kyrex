"""connectors.py — owner-scoped Google Calendar connector (read-only).

This is the credential foundation for the Kyrex Chat Calendar Reader. It
implements ONLY the Google Calendar ``calendar.readonly`` slice:

  * owner-scoped, ENCRYPTED OAuth token storage (Fernet-sealed). The on-disk
    registry NEVER contains a plaintext access token, refresh token, client
    secret, or authorization code.
  * the OAuth round-trip: ``begin_oauth`` (authorize URL + single-use,
    TTL-bound, OWNER-bound, REDIRECT-bound ``state``) / ``complete_oauth``
    (server-side code exchange) / ``disconnect`` (idempotent, removes the
    sealed blob);
  * the read-only Calendar interface used by the in-process Chat reader.

Explicitly NOT implemented: Gmail, sending mail, creating/updating/deleting
calendar events, or any destructive action. Capability declarations mark
those unsupported and every attempt fails closed.

Sealing uses the same key source as the rest of Kyrex Cloud
(``WEB_SESSION_SECRET`` / ``KYREX_PROVIDER_SECRETS_KEY``), domain-separated
so a calendar token can never be decrypted by, or used to decrypt, a
browser-session or provider-profile blob.

Security boundaries:

  * Tokens exist in plaintext ONLY in memory, for the duration of a provider
    call. They are never returned, logged, persisted, or interpolated into a
    prompt or result.
  * Every read re-checks CONNECTED then UNEXPIRED, each fail closed.
  * Provider responses are redacted before they leave the module and every
    failure is reported as a generic, secret-free message.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

_CLOUD_DIR = Path(__file__).resolve().parent
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

from paths import data_dir  # noqa: E402


# ── Errors (all fail closed) ───────────────────────────────────────────

class ConnectorError(Exception):
    """Base class for connector errors."""


class ConnectorConfigError(ConnectorError):
    """The connector is not configured on this host (missing OAuth client)."""


class ConnectorUnavailable(ConnectorError):
    """The connector cannot be used: disconnected, expired, or unauthorized.

    Raised INSTEAD of a provider call. The message never contains a token,
    code, or provider secret."""


class OAuthStateError(ConnectorError):
    """The OAuth state is missing, unknown, reused, expired, or foreign."""


# ── Provider + capability declarations ─────────────────────────────────

#: The ONLY scope this slice requests.
GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
GOOGLE_READ_SCOPES = (GOOGLE_CALENDAR_READ_SCOPE,)

#: The MINIMUM Google scope that allows creating a calendar event. It is a
#: SEPARATE scope from the Reader's read scope and is requested ONLY when the
#: owner explicitly enables the Calendar Writer (``begin_calendar_write_upgrade``).
GOOGLE_CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
#: The EXACT set of Google scopes this host will ever request.
GOOGLE_ALLOWED_SCOPES = frozenset(
    tuple(GOOGLE_READ_SCOPES) + (GOOGLE_CALENDAR_WRITE_SCOPE,)
)

PROVIDERS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": GOOGLE_READ_SCOPES,
        "calendar_api": "https://www.googleapis.com/calendar/v3",
    },
}

CALENDAR_CAPABILITIES = ("calendar.read",)

#: The DISTINCT write capability: create ONE event. Never the Reader's read.
CALENDAR_WRITER_CAPABILITIES = ("calendar.create",)

#: The DISTINCT destructive capability: delete ONE event by exact id.
CALENDAR_EDITOR_CAPABILITIES = ("calendar.delete",)

CAPABILITY_DECLARATIONS = {
    "calendar_bot": {
        "connector": "google",
        "capabilities": CALENDAR_CAPABILITIES,
        "read_only": True,
        "unsupported": (
            "calendar.create", "calendar.update", "calendar.delete",
            "calendar.invite",
        ),
    },
    "calendar_writer": {
        "connector": "google",
        # EXACTLY one capability: create an event on the owner's primary
        # calendar. No read, update, delete, invite, or availability.
        "capabilities": CALENDAR_WRITER_CAPABILITIES,
        "read_only": False,
        "unsupported": (
            "calendar.read", "calendar.update", "calendar.delete",
            "calendar.invite", "calendar.availability",
        ),
    },
    "calendar_editor": {
        "connector": "google",
        # EXACTLY one capability: DELETE an event from the owner primary
        # calendar, by exact id. No read, create, update, invite.
        "capabilities": CALENDAR_EDITOR_CAPABILITIES,
        "read_only": False,
        "unsupported": (
            "calendar.read", "calendar.create", "calendar.update",
            "calendar.invite", "calendar.availability",
        ),
    },
}

CAPABILITY_ROUTING = {
    **{cap: "calendar_bot" for cap in CALENDAR_CAPABILITIES},
    **{cap: "calendar_writer" for cap in CALENDAR_WRITER_CAPABILITIES},
    **{cap: "calendar_editor" for cap in CALENDAR_EDITOR_CAPABILITIES},
}


# ── Redaction (results, views, logs) ───────────────────────────────────

_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|proxy-authorization|cookie|set-cookie|token|secret|"
    r"password|passwd|api[_-]?key|session|credential|code_verifier|"
    r"refresh[_-]?token|access[_-]?token|client[_-]?secret)",
    re.IGNORECASE,
)
_HEADER_LINE_RE = re.compile(
    r"\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\n]+",
    re.IGNORECASE,
)
_USERINFO_RE = re.compile(r"https?://[^/\s:]+:[^/\s@]+@", re.IGNORECASE)
_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:access_token|refresh_token|token|key|api_key|code)=)[^&\s]+",
    re.IGNORECASE,
)


def redact_text(text) -> str:
    """Scrub secret-shaped material from any string leaving this module."""
    if text is None:
        return ""
    out = str(text)
    out = _USERINFO_RE.sub("[redacted]@", out)
    out = _HEADER_LINE_RE.sub(
        lambda m: f"{m.group(1).title()}: [redacted]", out)
    out = _SENSITIVE_QUERY_RE.sub(r"\1[redacted]", out)
    return out


def redact_obj(obj):
    """Recursively redact a JSON-shaped object (sensitive keys drop out)."""
    if isinstance(obj, dict):
        return {
            str(k): ("[redacted]" if _SENSITIVE_KEY_RE.search(str(k))
                     else redact_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


# ── Sealing (encrypted, owner-scoped token storage) ────────────────────

def _box():
    """Fernet for connector token blobs (fail closed without a key)."""
    secret = (
        os.environ.get("WEB_SESSION_SECRET")
        or os.environ.get("KYREX_PROVIDER_SECRETS_KEY")
    )
    if not secret:
        raise ConnectorConfigError("connector token encryption is not configured")
    try:
        from cryptography.fernet import Fernet
    except Exception as exc:  # pragma: no cover — dependency present in prod
        raise ConnectorConfigError(f"connector encryption unavailable: {exc}")
    key = base64.urlsafe_b64encode(
        hashlib.sha256(("kyrex-calendar-connector:" + secret).encode()).digest()
    )
    return Fernet(key)


def seal_tokens(tokens: dict) -> str:
    """Seal a token payload into an opaque blob (plaintext never persists)."""
    if not tokens:
        raise ConnectorError("nothing to seal")
    payload = json.dumps(tokens, sort_keys=True, separators=(",", ":"))
    return _box().encrypt(payload.encode()).decode()


def unseal_tokens(blob) -> dict:
    """Decrypt a sealed token blob; ``{}`` when absent or undecryptable."""
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


# ── Store ──────────────────────────────────────────────────────────────

DEFAULT_CONNECTORS_FILE = "connectors.json"
STATE_TTL_SECONDS = 900          # an OAuth round-trip must complete in 15 min

#: Serialises the read-modify-write of the pending-state registry. A state is
#: validated and marked consumed as ONE atomic step, so a provider round-trip
#: (which happens only after consumption) can never race a replay into a
#: second code exchange. Process-local, matching the rest of the store.
_STATE_LOCK = threading.Lock()
_TOKEN_REFRESH_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


class ConnectorStore:
    """Owner-scoped connector registry with encrypted token storage.

    Records are keyed by (owner, provider). Tokens live in ONE Fernet-sealed
    ``sealed`` field; the on-disk JSON therefore contains no plaintext token,
    secret, or code -- only identities, scopes, status, and timestamps (which
    makes the file safe to include in a bug report).
    """

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else (
            data_dir() / DEFAULT_CONNECTORS_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── file I/O ─────────────────────────────────────────────────────

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"owners": {}}
        if not isinstance(data, dict):
            return {"owners": {}}
        data.setdefault("owners", {})
        data.setdefault("pending_states", {})
        return data

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        tmp.replace(self.path)

    @staticmethod
    def _owner_key(owner: str) -> str:
        owner = str(owner or "").strip()
        if not owner:
            raise ConnectorError("a connector requires an owner")
        return hashlib.sha256(owner.encode()).hexdigest()[:24]

    # ── OAuth: connect / callback / disconnect ───────────────────────

    def _provider(self, provider: str) -> dict:
        conf = PROVIDERS.get(str(provider or "").strip())
        if conf is None:
            raise ConnectorError(f"unknown connector provider {provider!r}")
        return conf

    def client_config(self) -> dict:
        """The host's Google OAuth client (never a per-owner secret)."""
        client_id = str(os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
        client_secret = str(os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
        redirect = str(os.environ.get("GOOGLE_REDIRECT_URI") or "").strip()
        if not client_id or not client_secret or not redirect:
            raise ConnectorConfigError(
                "Google OAuth is not configured on this host")
        return {"client_id": client_id, "client_secret": client_secret,
                "redirect_uri": redirect}

    def begin_oauth(self, owner, provider="google", *, redirect_uri=None,
                    ttl=STATE_TTL_SECONDS, now=None, scopes=None) -> dict:
        """Start an OAuth round-trip for *owner*.

        Returns ``{state, authorization_url, provider, expires_at, scopes}``.
        The raw ``state`` is returned to the caller ONCE; only its hash is
        persisted. It is single-use, owner-bound, redirect-bound, and TTL-bound.

        *scopes* optionally overrides the provider's default scopes. Every
        requested scope must be in :data:`GOOGLE_ALLOWED_SCOPES` (the read scope
        plus the ONE calendar event-write scope); anything else is refused, so a
        caller can never widen the consent to an arbitrary Google scope. When
        omitted the read-only default is requested.
        """
        owner = str(owner or "").strip()
        conf = self._provider(provider)
        client = self.client_config()
        if scopes is None:
            requested = list(conf["scopes"])
        else:
            requested = [str(s).strip() for s in scopes if str(s).strip()]
            if not requested:
                raise ConnectorError(
                    "an OAuth request must ask for at least one scope")
            disallowed = [s for s in requested if s not in GOOGLE_ALLOWED_SCOPES]
            if disallowed:
                raise ConnectorError(
                    "refusing to request unsupported scope(s): "
                    + ", ".join(disallowed))
            requested = list(dict.fromkeys(requested))
        now = _now() if now is None else float(now)
        redirect = redirect_uri or client["redirect_uri"]
        state = uuid.uuid4().hex + uuid.uuid4().hex
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        with _STATE_LOCK:
            data = self._read()
            pending = data.setdefault("pending_states", {})
            # Drop expired states while we are here (bounded growth).
            for key in [k for k, s in pending.items()
                        if float(s.get("expires_at") or 0) <= now]:
                pending.pop(key, None)
            pending[state_hash] = {
                "owner": owner,
                "provider": provider,
                "hash": state_hash,
                "redirect_uri": redirect,
                # Persist the exact bounded request so the callback can retain
                # the approved grant even when Google's token response omits
                # its optional "scope" field.
                "scopes": list(requested),
                "expires_at": now + max(60, int(ttl)),
                "consumed": False,
            }
            self._write(data)  # minted state is durable before we return it
        query = urllib.parse.urlencode({
            "client_id": client["client_id"],
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": " ".join(requested),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        })
        return {
            "provider": provider,
            "state": state,
            "authorization_url": f"{conf['authorize_url']}?{query}",
            "expires_at": now + max(60, int(ttl)),
            "scopes": list(requested),
        }

    def begin_calendar_write_upgrade(self, owner, provider="google", *,
                                     redirect_uri=None, ttl=STATE_TTL_SECONDS,
                                     now=None) -> dict:
        """Start an OAuth round-trip that ADDS the calendar event-write scope.

        Called ONLY when the owner explicitly enables the Calendar Writer. It
        requests the MINIMUM additional Google scope
        (:data:`GOOGLE_CALENDAR_WRITE_SCOPE`) UNIONed with the owner's currently
        granted scopes, so nothing already working is dropped and no arbitrary
        scope is requested. A never-connected owner starts from the read scope.
        The consent is re-shown (``prompt=consent``). Tokens remain owner-scoped
        and sealed exactly as before -- the Reader's read-only token/scope is
        never altered in place; the new grant replaces this owner's sealed blob
        only on a completed consent.
        """
        current = [s for s in (self.status(owner, provider).get("scopes") or [])
                   if s in GOOGLE_ALLOWED_SCOPES]
        if not current:
            current = list(GOOGLE_READ_SCOPES)
        requested = list(dict.fromkeys(current + [GOOGLE_CALENDAR_WRITE_SCOPE]))
        return self.begin_oauth(
            owner, provider, redirect_uri=redirect_uri, ttl=ttl, now=now,
            scopes=requested,
        )

    def consume_state(self, state, *, owner=None, redirect_uri=None,
                      now=None) -> dict:
        """Validate and ATOMICALLY consume a pending OAuth state.

        Returns the consumed record's safe projection
        ``{owner, provider, redirect_uri}``. Fails closed when the state is
        unknown, already consumed, expired, or bound to a different
        owner/redirect. When *owner* is falsy the state ITSELF authenticates
        its owner -- the browser-callback path, which cannot carry a bearer or
        session credential -- so no other proof is required. A caller that
        DOES assert an owner must match the state's owner.

        The read-check-consume-write is performed under ``_STATE_LOCK`` and the
        ``consumed`` flag is persisted BEFORE returning, so callers exchange
        the authorization code only after consumption and a replay can never
        reach the provider.
        """
        now = _now() if now is None else float(now)
        state_hash = hashlib.sha256(str(state or "").encode()).hexdigest()
        with _STATE_LOCK:
            data = self._read()
            pending = data.setdefault("pending_states", {})
            rec = pending.get(state_hash)
            if rec is None:
                raise OAuthStateError("unknown or already-finished OAuth state")
            bound_owner = str(rec.get("owner") or "").strip()
            if not bound_owner:
                raise OAuthStateError("OAuth state is not bound to an owner")
            if owner and str(owner).strip() != bound_owner:
                # A caller that ASSERTS an identity must match the state.
                raise OAuthStateError(
                    "OAuth state does not belong to this owner")
            if rec.get("consumed"):
                raise OAuthStateError("OAuth state was already used")
            if float(rec.get("expires_at") or 0) <= now:
                pending.pop(state_hash, None)
                self._write(data)
                raise OAuthStateError("OAuth state has expired - start again")
            bound_redirect = str(rec.get("redirect_uri") or "")
            if redirect_uri is not None and str(redirect_uri) != bound_redirect:
                raise OAuthStateError(
                    "OAuth redirect does not match this state")
            rec["consumed"] = True
            self._write(data)  # consumed + persisted BEFORE any exchange
        bound_provider = str(rec.get("provider") or "google")
        raw_scopes = rec.get("scopes")
        if raw_scopes is None:
            # Backward compatibility for already-minted read-only states.
            bound_scopes = list(self._provider(bound_provider)["scopes"])
        elif not isinstance(raw_scopes, list):
            raise OAuthStateError("OAuth state scopes are malformed")
        else:
            bound_scopes = [str(s).strip() for s in raw_scopes
                            if str(s).strip()]
            if (not bound_scopes
                    or any(s not in GOOGLE_ALLOWED_SCOPES
                           for s in bound_scopes)):
                raise OAuthStateError("OAuth state scopes are invalid")
        return {
            "owner": bound_owner,
            "provider": bound_provider,
            "redirect_uri": bound_redirect,
            "scopes": bound_scopes,
        }

    def complete_oauth(self, owner, state, code, *, provider="google",
                       exchange=None, redirect_uri=None, now=None) -> dict:
        """Finish an OAuth round-trip and persist the ENCRYPTED tokens.

        The single-use state is validated and consumed FIRST; only then is the
        authorization code exchanged, so a replayed or concurrent callback
        fails closed without touching the provider.

        When *owner* is falsy the state authenticates its owner -- the browser
        callback, which carries no bearer/session credential. A caller that
        DOES supply an owner must match the state's owner or fail closed.
        ``exchange`` is an injected ``callable(code, redirect_uri, client) ->
        dict``; production performs the real POST.
        """
        owner = str(owner or "").strip()
        self._provider(provider)
        client = self.client_config()
        now = _now() if now is None else float(now)
        if not code:
            raise ConnectorError("an authorization code is required")

        consumed = self.consume_state(
            state, owner=owner or None, redirect_uri=redirect_uri, now=now)
        owner = consumed["owner"]
        bound_redirect = consumed["redirect_uri"]

        tokens = exchange(code, bound_redirect, client) if exchange \
            else self._default_exchange(code, bound_redirect, client)
        if not isinstance(tokens, dict):
            raise ConnectorError("token exchange returned a malformed response")
        access = str(tokens.get("access_token") or "")
        if not access:
            raise ConnectorError("token exchange returned no access token")
        try:
            expires_in = int(tokens.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        # Google may omit "scope" when the granted scopes equal the request.
        # In that case the single-use, owner-bound state is the authoritative
        # record of the bounded scopes we requested. Falling back to the
        # provider's read-only default silently discarded a successful writer
        # upgrade.
        token_scope = str(tokens.get("scope") or "").strip()
        effective_scopes = (
            token_scope.split() if token_scope
            else list(consumed.get("scopes") or [])
        )
        if (not effective_scopes
                or any(s not in GOOGLE_ALLOWED_SCOPES
                       for s in effective_scopes)):
            raise ConnectorError("token exchange returned invalid scopes")
        payload = {
            "access_token": access,
            "refresh_token": str(tokens.get("refresh_token") or ""),
            "scope": " ".join(dict.fromkeys(effective_scopes)),
            "token_type": str(tokens.get("token_type") or "Bearer"),
        }

        # Re-read so the token write never clobbers a concurrent state change:
        # the state was already consumed (and persisted) above.
        data = self._read()
        owners = data.setdefault("owners", {})
        rec_owner = owners.setdefault(
            self._owner_key(owner), {"owner": owner, "providers": {}})
        rec_owner["providers"][provider] = {
            "provider": provider,
            "owner": owner,
            "status": "connected",
            "scopes": payload["scope"].split(),
            "sealed": seal_tokens(payload),
            "connected_at": now,
            "expires_at": now + expires_in if expires_in else None,
            "updated_at": now,
        }
        self._write(data)
        return self.status(owner, provider)

    @staticmethod
    def _default_exchange(code: str, redirect_uri: str, client: dict) -> dict:
        """POST the authorization code to the provider's token endpoint."""
        conf = PROVIDERS["google"]
        body = urllib.parse.urlencode({
            "code": code,
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode()
        req = urllib.request.Request(
            conf["token_url"], data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                return json.loads(resp.read().decode() or "{}")
        except Exception as exc:
            # Never echo the response body or the code.
            raise ConnectorUnavailable(
                f"token exchange failed: {type(exc).__name__}")

    @staticmethod
    def _default_refresh(refresh_token: str, client: dict) -> dict:
        """Exchange a stored refresh token without exposing provider details."""
        conf = PROVIDERS["google"]
        body = urllib.parse.urlencode({
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            conf["token_url"], data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                return json.loads(resp.read().decode() or "{}")
        except Exception:
            # Provider bodies often contain credentials or detailed account
            # information. Never include them (or the exception) in a result.
            raise ConnectorUnavailable(
                "authorization refresh failed - reconnect the connector")

    def disconnect(self, owner, provider="google") -> bool:
        """Drop the owner's stored tokens and mark the connector disconnected.

        Idempotent: disconnecting an already-disconnected/never-connected
        connector returns False and changes nothing. The sealed blob is
        REMOVED (not merely flagged), so the secret no longer exists on disk.
        """
        owner = str(owner or "").strip()
        self._provider(provider)
        data = self._read()
        owner_rec = data.get("owners", {}).get(self._owner_key(owner))
        if not owner_rec:
            return False
        rec = owner_rec.get("providers", {}).get(provider)
        if not rec or rec.get("status") != "connected":
            return False
        rec["status"] = "disconnected"
        rec["sealed"] = None           # secret material is gone
        rec["sealed_removed_at"] = _now()
        rec["updated_at"] = _now()
        self._write(data)
        return True

    # ── Owner-scoped access ──────────────────────────────────────────

    def _record(self, owner, provider) -> dict | None:
        data = self._read()
        owner_rec = data.get("owners", {}).get(self._owner_key(owner))
        if not owner_rec:
            return None
        return (owner_rec.get("providers") or {}).get(provider)

    def public_view(self, rec) -> dict:
        """The SAFE shape: identities, scopes, status, timestamps -- no token.

        A never-connected owner gets the SAME key set (all empty/False), so
        callers never special-case a missing record and no branch can leak a
        stored field."""
        if not rec:
            return {
                "provider": None, "owner": None, "status": "disconnected",
                "connected": False, "scopes": [], "connected_at": None,
                "expires_at": None, "updated_at": None,
                "has_stored_token": False, "calendar_id": "primary",
            }
        return {
            "provider": rec.get("provider"),
            "owner": rec.get("owner"),
            "status": rec.get("status"),
            "connected": rec.get("status") == "connected",
            "scopes": list(rec.get("scopes") or []),
            "connected_at": rec.get("connected_at"),
            "expires_at": rec.get("expires_at"),
            "updated_at": rec.get("updated_at"),
            "has_stored_token": bool(rec.get("sealed")),
            # Non-secret destination-calendar preference (default "primary").
            "calendar_id": preferred_calendar_safe(rec),
        }

    def status(self, owner, provider="google") -> dict:
        return self.public_view(self._record(str(owner or "").strip(), provider))

    def access_token(self, owner, provider="google", *, now=None,
                     refresh=None) -> str:
        """The owner's live access token -- INTERNAL ONLY, fail closed.

        Expired Google access tokens are refreshed from the encrypted refresh
        token. Raises :class:`ConnectorUnavailable` when disconnected,
        unreadable, or refresh fails.
        Callers must never log, return, or persist the value.
        """
        owner = str(owner or "").strip()
        rec = self._record(owner, provider)
        if not rec or rec.get("status") != "connected":
            raise ConnectorUnavailable("connector is not connected")
        tokens = unseal_tokens(rec.get("sealed"))
        token = str(tokens.get("access_token") or "")
        if not token:
            raise ConnectorUnavailable(
                "stored authorization is unreadable - reconnect the connector")
        current_time = _now() if now is None else float(now)
        expires_at = rec.get("expires_at")
        if expires_at is None or float(expires_at) > current_time:
            return token

        # Serialize refreshes and re-read after acquiring the lock so two
        # simultaneous calendar reads never spend the same refresh token.
        with _TOKEN_REFRESH_LOCK:
            rec = self._record(owner, provider)
            if not rec or rec.get("status") != "connected":
                raise ConnectorUnavailable("connector is not connected")
            tokens = unseal_tokens(rec.get("sealed"))
            token = str(tokens.get("access_token") or "")
            expires_at = rec.get("expires_at")
            if token and (expires_at is None
                          or float(expires_at) > current_time):
                return token

            refresh_token = str(tokens.get("refresh_token") or "")
            if not refresh_token:
                raise ConnectorUnavailable(
                    "authorization has expired - reconnect the connector")
            try:
                client = self.client_config()
                refreshed = (refresh(refresh_token, client) if refresh
                             else self._default_refresh(refresh_token, client))
                if not isinstance(refreshed, dict):
                    raise ValueError("malformed refresh response")
                new_access = str(refreshed.get("access_token") or "")
                expires_in = int(refreshed.get("expires_in") or 0)
                if not new_access or expires_in <= 0:
                    raise ValueError("incomplete refresh response")
                scope_text = str(refreshed.get("scope") or "").strip()
                scopes = (scope_text.split() if scope_text
                          else list(rec.get("scopes") or []))
                if (not scopes
                        or any(scope not in GOOGLE_ALLOWED_SCOPES
                               for scope in scopes)):
                    raise ValueError("invalid refreshed scopes")
            except ConnectorUnavailable:
                raise
            except Exception:
                raise ConnectorUnavailable(
                    "authorization refresh failed - reconnect the connector")

            updated_tokens = {
                "access_token": new_access,
                "refresh_token": str(
                    refreshed.get("refresh_token") or refresh_token),
                "scope": " ".join(dict.fromkeys(scopes)),
                "token_type": str(
                    refreshed.get("token_type")
                    or tokens.get("token_type") or "Bearer"),
            }
            data = self._read()
            owner_rec = data.get("owners", {}).get(self._owner_key(owner))
            live = ((owner_rec or {}).get("providers") or {}).get(provider)
            if not live or live.get("status") != "connected":
                raise ConnectorUnavailable("connector is not connected")
            live["scopes"] = list(dict.fromkeys(scopes))
            live["sealed"] = seal_tokens(updated_tokens)
            live["expires_at"] = current_time + expires_in
            live["updated_at"] = current_time
            self._write(data)
            return new_access

    # ── Non-secret calendar preference (owner-scoped) ──────────────
    #
    # The DESTINATION calendar a Calendar Bot reads from and writes to. It is
    # a plain, non-secret identifier stored on the owner's connector record
    # (never in the sealed token blob): an unset preference means the
    # provider's own default calendar ("primary"). This is owner-scoped, so
    # two users can never share or overwrite each other's destination.

    def set_calendar(self, owner, calendar_id, provider="google") -> str:
        """Persist the owner's destination calendar id. Returns it.

        Fail closed: a non-string, empty, or credential-shaped value is
        rejected with :class:`ConnectorError` and nothing is written. The
        preference is stored on an EXISTING connected record only — a
        never-connected owner cannot set a destination (the value would be
        meaningless and would silently attach to a future connection).
        """
        owner = str(owner or "").strip()
        self._provider(provider)
        raw = str(calendar_id or "").strip()
        if not raw:
            raise ConnectorError("a calendar id is required")
        if len(raw) > 255 or any(ch.isspace() for ch in raw):
            raise ConnectorError("calendar id is not a valid Google calendar id")
        if "://" in raw or any(ord(ch) < 32 for ch in raw):
            # Credential/URL-shaped values (userinfo, schemes) are never stored.
            raise ConnectorError("calendar id must not carry credentials or a scheme")
        data = self._read()
        owners = data.setdefault("owners", {})
        owner_rec = owners.setdefault(
            self._owner_key(owner), {"owner": owner, "providers": {}})
        rec = owner_rec.get("providers", {}).get(provider)
        if not rec or rec.get("status") != "connected":
            raise ConnectorUnavailable(
                "connect Google Calendar before choosing a destination calendar")
        rec["calendar_id"] = raw
        rec["updated_at"] = _now()
        self._write(data)
        return raw

    def preferred_calendar(self, owner, provider="google") -> str:
        """The owner's destination calendar id, or ``"primary"`` when unset.

        Read-only and fail closed: an unset preference yields the provider's
        default; a malformed stored value is ignored (never surfaced to a
        caller or used as a target).
        """
        rec = self._record(str(owner or "").strip(), provider)
        if not rec:
            return "primary"
        value = str(rec.get("calendar_id") or "").strip()
        if not value or len(value) > 255 or any(ch.isspace() for ch in value):
            return "primary"
        return value

    def account_view(self, owner, provider="google", *, transport=None):
        """The connected Google ACCOUNT (email) — read-only, non-secret.

        Uses the Google tokeninfo endpoint with the owner's live access token
        and returns ONLY the safe identity fields. Fail closed: a
        not-connected/expired connector raises :class:`ConnectorUnavailable`,
        and a malformed provider response never leaks a token.
        """
        owner = str(owner or "").strip()
        try:
            token = self.access_token(owner, provider)
        except ConnectorUnavailable:
            raise ConnectorUnavailable("Google is not connected") from None
        transport = transport or default_transport
        out = transport(
            "GET",
            "https://oauth2.googleapis.com/tokeninfo",
            "",
            {"access_token": token},
        )
        if not isinstance(out, dict):
            raise ConnectorUnavailable(
                "Google account check returned a malformed response")
        email = str(out.get("email") or "").strip() or None
        return {
            "email": email,
            "provider": provider,
            "calendar_id": self.preferred_calendar(owner, provider),
        }

    def list_calendars(self, owner, provider="google", *, transport=None):
        """The owner's readable Google calendars (calendarList, read-only).

        Returns a bounded list of non-secret calendar summaries
        (``id`` + ``summary`` only), always including the owner's preferred
        calendar when it is present. Fail closed: not connected/expired
        raises :class:`ConnectorUnavailable`; a malformed provider response
        raises without leaking anything.
        """
        owner = str(owner or "").strip()
        token = self.access_token(owner, provider)
        transport = transport or default_transport
        api = PROVIDERS[str(provider) or "google"]["calendar_api"]
        out = transport("GET", f"{api}/users/me/calendarList", token, {
            "maxResults": 200,
        })
        if not isinstance(out, dict):
            raise ConnectorUnavailable(
                "calendar list returned a malformed response")
        items = out.get("items") or []
        if not isinstance(items, list):
            raise ConnectorUnavailable(
                "calendar list returned a malformed response (items)")
        preferred = self.preferred_calendar(owner, provider)
        view = []
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            if not cid or len(view) >= 200:
                continue
            view.append({
                "id": cid,
                "summary": str(item.get("summary") or cid)[:200],
                "preferred": cid == preferred,
                "primary": bool(item.get("primary")),
            })
        return view

    # ── Capability routing ───────────────────────────────────────────

    def route_capability(self, owner, capability, provider="google") -> dict:
        """Resolve *capability* to its owning Bot role, fail closed.

        A capability this slice does not declare (e.g. ``calendar.create``) is
        refused outright, as is any capability whose connector is not
        connected for this owner.
        """
        cap = str(capability or "").strip()
        role = CAPABILITY_ROUTING.get(cap)
        if role is None:
            raise ConnectorUnavailable(
                f"capability {cap!r} is not supported by this connector slice")
        if cap not in CAPABILITY_DECLARATIONS[role]["capabilities"]:
            raise ConnectorUnavailable(f"capability {cap!r} is not declared")
        if not self.status(owner, provider)["connected"]:
            raise ConnectorUnavailable(
                f"capability {cap!r} is unavailable: connector is not connected")
        return {
            "capability": cap,
            "connector": provider,
            "bot_role": role,
            "read_only": bool(CAPABILITY_DECLARATIONS[role]["read_only"]),
            "available": True,
        }

    # ── Read-only Calendar interface ─────────────────────────────────

    def calendar(self, owner, *, transport=None, provider="google") -> "CalendarRead":
        return CalendarRead(self, owner, provider=provider, transport=transport)

    def calendar_writer(self, owner, *, transport=None,
                        provider="google") -> "CalendarWrite":
        """The owner-scoped Calendar WRITE interface (create only)."""
        return CalendarWrite(self, owner, provider=provider, transport=transport)

    def calendar_editor(self, owner, *, transport=None,
                        provider="google") -> "CalendarEdit":
        """The owner-scoped Calendar EDITOR interface (delete only)."""
        return CalendarEdit(self, owner, provider=provider, transport=transport)


# ── Transport (injectable; the real one never logs the token) ──────────

def default_transport(method: str, url: str, token: str, params=None,
                      body=None) -> dict:
    """Perform one provider call with the owner's access token.

    The token travels in the ``Authorization`` header only. Nothing here
    logs, echoes, or returns the header value.
    """
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 — fixed provider host
        f"{url}{query}", data=data, method=method.upper(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read().decode() or "{}")
    except Exception as exc:
        raise ConnectorUnavailable(f"provider call failed: {type(exc).__name__}")


class CalendarRead:
    """Read-only Calendar: a bounded, redacted ``events`` query only."""

    def __init__(self, store: "ConnectorStore", owner: str, *,
                 provider="google", transport=None):
        self._store = store
        self._owner = str(owner or "").strip()
        self._provider = provider
        self._transport = transport or default_transport

    def _authorize(self) -> str:
        """Validate DECLARED + CONNECTED + UNEXPIRED, then return the token."""
        self._store.route_capability(self._owner, "calendar.read", self._provider)
        return self._store.access_token(self._owner, self._provider)

    def events(self, *, time_min=None, time_max=None, max_results=25,
               calendar_id="primary") -> list:
        """List the owner's events in a bounded window (read-only).

        Uses ``singleEvents=true`` and ``orderBy=startTime``. A malformed
        provider response fails closed.
        """
        token = self._authorize()
        api = PROVIDERS[self._provider]["calendar_api"]
        limit = max(1, min(int(max_results or 25), 100))
        params = {
            "maxResults": limit,
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if time_min:
            params["timeMin"] = str(time_min)
        if time_max:
            params["timeMax"] = str(time_max)
        out = self._transport(
            "GET",
            f"{api}/calendars/{urllib.parse.quote(str(calendar_id), safe='')}/events",
            token, params)
        if not isinstance(out, dict):
            raise ConnectorUnavailable("malformed calendar response")
        items = out.get("items") or []
        if not isinstance(items, list):
            raise ConnectorUnavailable("malformed calendar response (items)")
        return [_calendar_event(e, self._owner) for e in items
                if isinstance(e, dict)]


class CalendarWrite:
    """Owner-scoped Calendar WRITE interface (``calendar.create`` only).

    Deliberately NOT the Reader: a write must not be served through the read
    path. It re-checks, in order and each fail closed: the capability is
    DECLARED (``calendar.create`` -> ``calendar_writer``), the connector is
    CONNECTED, the token is UNEXPIRED, and the stored grant actually includes
    the WRITE scope. A Reader token (read scope only) can never write.
    """

    def __init__(self, store: "ConnectorStore", owner: str, *,
                 provider="google", transport=None):
        self._store = store
        self._owner = str(owner or "").strip()
        self._provider = provider
        self._transport = transport or default_transport

    def _authorize(self) -> str:
        decl = self._store.route_capability(
            self._owner, "calendar.create", self._provider)
        if decl["bot_role"] != "calendar_writer":
            raise ConnectorUnavailable(
                "calendar writes are not backed by the writer connector")
        granted = set(
            self._store.status(self._owner, self._provider).get("scopes") or [])
        if GOOGLE_CALENDAR_WRITE_SCOPE not in granted:
            raise ConnectorUnavailable(
                "calendar write authorization is missing - enable the Calendar "
                "Writer and grant the calendar event-write scope")
        return self._store.access_token(self._owner, self._provider)

    def create_event(self, event: dict) -> dict:
        """Create ONE event on the owner's DESTINATION calendar. Returns a receipt.

        The event body is an already-validated payload (cal_writer). The
        destination is the owner's stored non-secret calendar preference
        (``preferred_calendar``, default ``primary``) — never a caller
        supplied id. A malformed provider response -- anything without an id
        -- fails closed.
        """
        if not isinstance(event, dict) or not event.get("summary"):
            raise ConnectorError("a validated event payload is required")
        token = self._authorize()
        api = PROVIDERS[self._provider]["calendar_api"]
        calendar_id = self._store.preferred_calendar(self._owner, self._provider)
        out = self._transport(
            "POST",
            f"{api}/calendars/{urllib.parse.quote(str(calendar_id), safe='')}/events",
            token, None, event)
        if not isinstance(out, dict) or not str(out.get("id") or "").strip():
            raise ConnectorError(
                "calendar provider returned a malformed create response")
        return _calendar_created(out, self._owner)


# ── Redacted provider-response projection ─────────────────────────────

def preferred_calendar_safe(rec) -> str:
    """The record's destination calendar id, or ``"primary"`` when unset.

    Fail closed: a malformed stored value (oversized, whitespace-bearing, or
    a URL/credential shape) is never surfaced; the record falls back to the
    provider default.
    """
    if not isinstance(rec, dict):
        return "primary"
    value = str(rec.get("calendar_id") or "").strip()
    if not value or len(value) > 255 or any(ch.isspace() for ch in value):
        return "primary"
    if "://" in value or any(ord(ch) < 32 for ch in value):
        return "primary"
    return value


def _split_when(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    if raw.get("dateTime"):
        return {"dateTime": redact_text(raw.get("dateTime"))}
    if raw.get("date"):
        return {"date": redact_text(raw.get("date"))}
    return {}


def _calendar_event(event: dict, owner: str) -> dict:
    """Normalise ONE event to the minimal, redacted, render-ready shape."""
    return {
        "owner": str(owner or ""),
        "id": str(event.get("id") or ""),
        "status": redact_text(event.get("status")),
        "summary": redact_text(event.get("summary")),
        "start": _split_when(event.get("start")),
        "end": _split_when(event.get("end")),
    }


def _calendar_created(event: dict, owner: str) -> dict:
    """The SAFE projection of a CREATED event: an opaque id plus title/time.

    The provider's ``htmlLink`` (which can carry query tokens), etags, attendees
    and every other field are deliberately never returned.
    """
    return {
        "owner": str(owner or ""),
        "id": str(event.get("id") or ""),
        "status": redact_text(event.get("status")),
        "summary": redact_text(event.get("summary")),
        "start": _split_when(event.get("start")),
        "end": _split_when(event.get("end")),
    }


# ── Module-level convenience (owner-scoped default store) ──────────────

def default_store() -> "ConnectorStore":
    return ConnectorStore()


class CalendarEdit:
    """Owner-scoped Calendar EDITOR interface (calendar.delete only).

    Deliberately NOT the Reader and NOT the Writer: a delete must not be served
    through either. It re-checks, in order and each fail closed: the capability
    is DECLARED (calendar.delete -> calendar_editor), the connector is
    CONNECTED, and the stored grant actually includes the event-write scope. A
    Reader token (read scope only) can never delete.
    """

    def __init__(self, store, owner, *, provider="google", transport=None):
        self._store = store
        self._owner = str(owner or "").strip()
        self._provider = provider
        self._transport = transport or default_transport

    def _authorize(self) -> str:
        decl = self._store.route_capability(
            self._owner, "calendar.delete", self._provider)
        if decl["bot_role"] != "calendar_editor":
            raise ConnectorUnavailable(
                "calendar deletes are not backed by the editor connector")
        granted = set(
            self._store.status(self._owner, self._provider).get("scopes") or [])
        if GOOGLE_CALENDAR_WRITE_SCOPE not in granted:
            raise ConnectorUnavailable(
                "calendar delete authorization is missing - enable the Calendar "
                "Editor and grant the calendar event-write scope")
        return self._store.access_token(self._owner, self._provider)

    def delete_event(self, event_id, *, calendar_id=None) -> dict:
        """DELETE exactly ONE event, by its Google Calendar event id.

        The destination calendar is the owner's stored non-secret preference
        (preferred_calendar, default primary) -- never a caller-supplied id. An
        empty or malformed id fails closed before any provider call.
        """
        eid = str(event_id or "").strip()
        if not eid or len(eid) > 1024 or any(ch.isspace() for ch in eid):
            raise ConnectorError("a valid event id is required to delete")
        token = self._authorize()
        api = PROVIDERS[self._provider]["calendar_api"]
        target = calendar_id or self._store.preferred_calendar(
            self._owner, self._provider)
        self._transport(
            "DELETE",
            f"{api}/calendars/{urllib.parse.quote(str(target), safe='')}"
            f"/events/{urllib.parse.quote(eid, safe='')}",
            token, None)
        return {"deleted": True, "id": eid}
