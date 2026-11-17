"""Encrypted per-user provider profiles for Kyrex Chat."""
from __future__ import annotations
import base64, hashlib, json, os, re
import threading
import time
from functools import wraps
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
import httpx
from paths import data_dir
from kyrex.opencode_go import GO_BASE, go_base, suggestions

_PROFILE_LOCK = threading.RLock()
_GO_CATALOG_LOCK = threading.Lock()
_go_catalog = None
_go_catalog_expires = 0
_GO_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


def _fetch_go_catalog() -> list[str]:
    # Public provider metadata. The request carries no profile or credentials.
    with httpx.Client(timeout=3, follow_redirects=False) as client:
        with client.stream("GET", GO_BASE + "/models") as response:
            response.raise_for_status()
            payload = bytearray()
            for part in response.iter_bytes():
                if len(payload) + len(part) > 1024 * 1024:
                    raise ValueError("OpenCode Go catalog exceeded its size limit")
                payload.extend(part)
    data = json.loads(payload).get("data")
    if not isinstance(data, list) or not 0 < len(data) <= 1024:
        raise ValueError("Invalid OpenCode Go catalog")
    models = dict.fromkeys(entry["id"] for entry in data
                           if isinstance(entry, dict) and isinstance(entry.get("id"), str)
                           and _GO_MODEL_ID.fullmatch(entry["id"]))
    if not models:
        raise ValueError("OpenCode Go catalog contained no model IDs")
    return list(models)


def go_model_catalog() -> list[str]:
    """Cache public IDs across pickers; an empty list leaves profile suggestions in place."""
    global _go_catalog, _go_catalog_expires
    with _GO_CATALOG_LOCK:
        if time.monotonic() >= _go_catalog_expires:
            try:
                _go_catalog = _fetch_go_catalog()
            except (httpx.HTTPError, ValueError, AttributeError):
                _go_catalog_expires = time.monotonic() + 60
            else:
                _go_catalog_expires = time.monotonic() + 3600
        return list(_go_catalog or ())

def _locked(function):
    @wraps(function)
    def call(*args, **kwargs):
        with _PROFILE_LOCK:
            return function(*args, **kwargs)
    return call

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

def _user_dir(user: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in user)
    path = data_dir() / "chat" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path

def _box() -> Fernet:
    secret = os.environ.get("WEB_SESSION_SECRET") or os.environ.get("KYREX_PROVIDER_SECRETS_KEY")
    if not secret:
        raise RuntimeError("provider secret encryption is not configured")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)

def _path(user: str) -> Path:
    return _user_dir(user) / "provider_profiles.json"

def _read(user: str) -> list[dict]:
    path = _path(user)
    if not path.exists(): return []
    raw = json.loads(path.read_text())
    out = []
    for item in raw:
        try:
            item = json.loads(_box().decrypt(item.encode()).decode())
            if isinstance(item, dict): out.append(item)
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
            continue
    return out

def _write(user: str, profiles: list[dict]) -> None:
    values = [_box().encrypt(json.dumps(p, separators=(",", ":")).encode()).decode() for p in profiles]
    path = _path(user)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(values))
    tmp.replace(path)

# HTTP field-name token (RFC 7230). A configured header name must be a clean
# token — never a value smuggled into a name.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

def _last4(secret: str) -> str:
    """Last four characters of an API key, for a non-reversible UI hint.

    This is the ONLY fragment of a key that may leave the server. Empty when
    the key is missing or shorter than four characters.
    """
    secret = str(secret or "")
    return secret[-4:] if len(secret) >= 4 else ""

def public_profile(profile: dict) -> dict:
    """The non-secret view of one decrypted profile.

    Exposes identity/routing metadata plus two NON-SECRET hints: whether an
    API key exists (``has_api_key``), its last four characters
    (``api_key_last4``), and the NAMES of configured headers
    (``header_names``). The API key and every header VALUE are never
    included — a header value may itself be a credential.
    """
    profile = profile or {}
    headers = profile.get("headers")
    header_names = sorted(str(k) for k in headers) if isinstance(headers, dict) else []
    return {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "provider": profile.get("provider"),
        "base_url": profile.get("base_url"),
        "opencode_go": bool(go_base(profile.get("base_url"))),
        "models": suggestions(profile.get("base_url"), profile.get("models") or []),
        "aliases": list(profile.get("aliases") or []),
        "has_api_key": bool(profile.get("api_key")),
        "api_key_last4": _last4(profile.get("api_key")),
        "header_names": header_names,
    }

def get_profile(user: str, profile_id: str) -> dict | None:
    """Return the FULL decrypted profile (including secrets) or ``None``.

    For internal resolution ONLY. Callers must never hand this dict to a
    client — use :func:`public_profile` for anything that crosses the API
    boundary.
    """
    target = str(profile_id or "").strip().lower()
    if not target:
        return None
    for p in _read(user):
        if str(p.get("id") or "").strip().lower() == target:
            return p
    for p in _read(user):
        if target in (p.get("aliases") or []):
            return p
    return None

def list_profiles(user: str) -> list[dict]:
    return [public_profile(p) for p in _read(user)]

@_locked
def save_profile(user: str, profile: dict, *, allow_empty_models: bool = False) -> dict:
    profile_id = str(profile.get("id") or "").strip().lower()
    if not _ID_RE.fullmatch(profile_id): raise ValueError("invalid profile id")
    name = str(profile.get("name") or profile_id).strip()[:100]
    provider = str(profile.get("provider") or "openai").strip().lower()
    base_url = str(profile.get("base_url") or "").strip()
    api_key = str(profile.get("api_key") or "").strip()
    models = [str(m).strip() for m in (profile.get("models") or []) if str(m).strip()]
    if not name or not base_url or (not models and not allow_empty_models): raise ValueError("name, base_url, and models are required")
    # Optional extra request headers (e.g. an OpenRouter referer or an
    # organisation header). Names must be clean HTTP tokens; values are
    # stored as secrets and are NEVER returned by the read view.
    raw_headers = profile.get("headers")
    headers: dict[str, str] = {}
    if raw_headers is not None:
        if not isinstance(raw_headers, dict):
            raise ValueError("headers must be an object of name -> value")
        for hk, hv in raw_headers.items():
            hname = str(hk).strip()
            if not _HEADER_NAME_RE.fullmatch(hname):
                raise ValueError(f"invalid header name {hk!r}")
            headers[hname] = str(hv)
    existing = _read(user)
    old = next((p for p in existing if p.get("id") == profile_id), None)
    if not old and any(profile_id in (p.get("aliases") or []) for p in existing):
        raise ValueError("profile id is reserved by a consolidated profile")
    if not api_key and old: api_key = old.get("api_key", "")
    # Omitted/null headers preserve existing secrets. An explicit empty
    # object clears them; otherwise owners cannot remove bad saved headers.
    if raw_headers is None and old: headers = dict(old.get("headers") or {})
    if not api_key: raise ValueError("api_key is required for a new profile")
    value = {"id": profile_id, "name": name, "provider": provider, "base_url": base_url, "models": models, "api_key": api_key, "headers": headers}
    if old and old.get("aliases"):
        value["aliases"] = old["aliases"]
    existing = [p for p in existing if p.get("id") != profile_id] + [value]
    _write(user, existing)
    return next(p for p in list_profiles(user) if p["id"] == profile_id)

@_locked
def delete_profile(user: str, profile_id: str) -> bool:
    existing = _read(user)
    filtered = [p for p in existing if p.get("id") != profile_id]
    if len(filtered) == len(existing): return False
    _write(user, filtered)
    return True


@_locked
def consolidate_go_profiles(user, source_id, target_id):
    """Owner-requested same-key consolidation. Aliases keep saved references usable."""
    existing = _read(user)
    source = next((p for p in existing if p['id'] == source_id), None)
    target = next((p for p in existing if p['id'] == target_id), None)
    if not source and target and source_id in (target.get('aliases') or []):
        return public_profile(target)
    if not source or not target or source_id == target_id:
        raise ValueError('Choose two existing OpenCode profiles.')
    if not go_base(source['base_url']) or not go_base(target['base_url']):
        raise ValueError('Only OpenCode Go profiles can be consolidated.')
    if source['api_key'] != target['api_key']:
        raise ValueError('These profiles use different API keys; keep them separate.')
    # Write routing aliases before removing the duplicate in the SAME atomic
    # encrypted-file replacement. Existing Bot/chat references keep resolving.
    target = {**target, 'base_url': go_base(target['base_url']), 'provider': 'opencode',
        'models': list(dict.fromkeys([*target['models'], *source['models']])),
        'aliases': list(dict.fromkeys([*(target.get('aliases') or []), source_id,
                                     *(source.get('aliases') or [])]))}
    _write(user, [p for p in existing if p['id'] not in {source_id, target_id}] + [target])
    return public_profile(target)
