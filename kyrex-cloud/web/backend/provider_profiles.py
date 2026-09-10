"""Encrypted per-user provider profiles for Kyrex Chat."""
from __future__ import annotations
import base64, hashlib, json, os, re
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from paths import data_dir

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

def list_profiles(user: str) -> list[dict]:
    return [{k: p.get(k) for k in ("id","name","provider","base_url","models")}
            | {"has_api_key": bool(p.get("api_key"))} for p in _read(user)]

def save_profile(user: str, profile: dict) -> dict:
    profile_id = str(profile.get("id") or "").strip().lower()
    if not _ID_RE.fullmatch(profile_id): raise ValueError("invalid profile id")
    name = str(profile.get("name") or profile_id).strip()[:100]
    provider = str(profile.get("provider") or "openai").strip().lower()
    base_url = str(profile.get("base_url") or "").strip()
    api_key = str(profile.get("api_key") or "").strip()
    models = [str(m).strip() for m in (profile.get("models") or []) if str(m).strip()]
    if not name or not base_url or not models: raise ValueError("name, base_url, and models are required")
    existing = _read(user)
    old = next((p for p in existing if p.get("id") == profile_id), None)
    if not api_key and old: api_key = old.get("api_key", "")
    if not api_key: raise ValueError("api_key is required for a new profile")
    value = {"id": profile_id, "name": name, "provider": provider, "base_url": base_url, "models": models, "api_key": api_key}
    existing = [p for p in existing if p.get("id") != profile_id] + [value]
    _write(user, existing)
    return next(p for p in list_profiles(user) if p["id"] == profile_id)

def delete_profile(user: str, profile_id: str) -> bool:
    existing = _read(user)
    filtered = [p for p in existing if p.get("id") != profile_id]
    if len(filtered) == len(existing): return False
    _write(user, filtered)
    return True
