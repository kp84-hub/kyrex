"""profiles.py — saved provider profiles for Bots (encrypted at rest).

A provider profile stores ONE owner's credentials for ONE provider:

  provider         — "openai" / "anthropic" / "openrouter" / any
                     OpenAI-compatible vendor name
  base_url         — e.g. https://openrouter.ai/api/v1
  api_key          — stored ENCRYPTED (Fernet); never returned by any
                     read API, only its last 4 characters for display
  models           — the exact model IDs validated into this profile;
                     a Bot may only use a model listed here
  custom_headers   — extra HTTP headers sent with each provider request;
                     hop-by-hop and identity headers are stripped on
                     write and on apply (they can never override the
                     Authorization / x-api-key headers the provider flow
                     sets itself)

Bots reference a profile by id: the Bot registry stores only
``provider_profile_id`` and ``model`` — never key material (see
``bots.py``). On every Bot-bound LLM turn, ``resolve_for_bot`` is the
single resolution chokepoint: it returns a :class:`ProviderConfig`
carrying the profile's provider, base URL, decrypted key, approved
headers, and the Bot's exact model — or raises :class:`ProfileError`
with a clear, user-facing message. There is deliberately NO fallback to
KYREX_PROVIDER / KYREX_API_KEY on this path.

Storage: JSON at ``DATA_DIR/provider_profiles.json`` (override
``PROFILES_FILE`` for tests — same pattern as ``bots.BOTS_FILE``). Only
the API key is encrypted at rest; base_url/models/headers are
configuration, not secrets.

Encryption key: ``KYREX_PROFILE_SECRET``. Any non-empty string is
accepted and converted to a Fernet key (SHA-256 → urlsafe base64), so
operators don't have to generate a proper Fernet key by hand. With no
secret set the store is disabled: writes and resolution fail closed with
a clear error. Changing the secret invalidates previously stored keys
(decrypt errors raise a clear ProfileStoreError — never a wrong key).

Owner rules: profiles are owner-scoped. A profile matches a requester
when ``profile.owner == requester_owner`` OR the requester is the
operator (``owner == ""`` — legacy/operator bots in today's
single-operator deployment: one allowed GitHub username, one Telegram
chat). A multi-operator deployment must set ``owner`` explicitly on
both the profile and every Bot.

Custom headers for the ENGINE child process: the cloud-side LLM calls
(chat, classification, review) apply ``custom_headers`` directly. The
engine child receives provider/base_url/key/model via its environment
(``ProviderConfig.env_overrides``); engine-side custom-header support
is a documented follow-up, not silently missing.
"""
import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from paths import DATA_DIR

PROFILES_FILE = str(DATA_DIR / "provider_profiles.json")

# Headers a profile may never set: they are hop-by-hop or identity
# headers whose injection would corrupt transport or bypass auth.
_HOP_HEADERS = frozenset({
    "authorization", "proxy-authorization", "cookie", "host",
    "content-length", "connection", "keep-alive", "transfer-encoding",
    "upgrade", "te", "trailer",
})


# ── Errors ─────────────────────────────────────────────────────────────

class ProfileError(Exception):
    """Base class — user-facing messages are safe to display."""


class ProfileStoreError(ProfileError):
    """The profile store cannot be read/written or decrypted."""


class ProfileResolutionError(ProfileError):
    """A Bot's provider profile could not be resolved for this turn."""


# ── Resolved config ────────────────────────────────────────────────────

@dataclass
class ProviderConfig:
    """One Bot turn's resolved provider configuration.

    ``api_key`` lives only in worker-process memory (and, via
    ``env_overrides``, the executor child's environment). It must never
    be serialised into a registry, store, API response, or log line.
    """

    provider: str
    base_url: str
    api_key: str
    model: str
    headers: dict = field(default_factory=dict)
    profile_id: str = ""
    profile_name: str = ""

    def env_overrides(self) -> dict:
        """Environment overrides for an executor child process.

        The child (git_workflow.py → engine) reads the standard KYREX_*
        variables, so injecting them here — for this child only — swaps
        the global provider for the Bot's profile without touching the
        parent process environment.
        """
        is_anthropic = self.provider == "anthropic"
        env = {
            "KYREX_PROVIDER": "anthropic" if is_anthropic else "openai",
            "KYREX_MODEL": self.model,
            "KYREX_API_KEY": self.api_key,
        }
        if is_anthropic:
            env["ANTHROPIC_BASE_URL"] = self.base_url
        else:
            env["OPENAI_BASE_URL"] = self.base_url
        if self.headers:
            env["KYREX_CUSTOM_HEADERS"] = json.dumps(self.headers)
        return env


def sanitize_headers(headers: dict | None) -> dict:
    """Return a copy of *headers* without hop-by-hop/identity headers."""
    clean = {}
    for k, v in (headers or {}).items():
        if str(k).strip().lower() in _HOP_HEADERS:
            continue
        clean[str(k)] = str(v)
    return clean


# ── Storage helpers ────────────────────────────────────────────────────

def _ensure_dir():
    Path(PROFILES_FILE).parent.mkdir(parents=True, exist_ok=True)


def _fernet():
    secret = os.environ.get("KYREX_PROFILE_SECRET", "")
    if not secret:
        raise ProfileStoreError(
            "KYREX_PROFILE_SECRET is not set — saved provider profiles are "
            "disabled (set it to enable per-bot provider profiles)"
        )
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def _load() -> dict:
    try:
        with open(PROFILES_FILE, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ProfileStoreError(
            "provider profiles at %s are not valid JSON: %s" % (PROFILES_FILE, exc)
        ) from exc
    if not isinstance(data, dict):
        raise ProfileStoreError(
            "provider profiles at %s have an unexpected shape" % PROFILES_FILE
        )
    return data


def _save(profiles: dict) -> None:
    _ensure_dir()
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f, indent=2, sort_keys=True)
        f.write("\n")


def _encrypt(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode()).decode()


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception as exc:
        raise ProfileStoreError(
            "stored provider profile key cannot be decrypted — was "
            "KYREX_PROFILE_SECRET changed? (%s)" % type(exc).__name__
        ) from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner_ok(profile_owner: str, requester_owner: str) -> bool:
    """Owner match rule (see module docstring): exact match, or the
    requester is the operator (``""``) in a single-operator deployment."""
    return profile_owner == requester_owner or requester_owner == ""


def _public(profile: dict) -> dict:
    """The ONLY shape any read API may return — no key material."""
    return {
        "id": profile["id"],
        "owner": profile.get("owner", ""),
        "name": profile.get("name", ""),
        "provider": profile.get("provider", ""),
        "base_url": profile.get("base_url", ""),
        "models": list(profile.get("models", [])),
        "custom_headers": dict(profile.get("custom_headers", {})),
        "api_key_last4": profile.get("api_key_last4", ""),
        "created_at": profile.get("created_at", ""),
        "updated_at": profile.get("updated_at", ""),
    }


def _validate_fields(provider: str, base_url: str, models, custom_headers):
    if not str(provider or "").strip():
        raise ProfileError("provider is required (e.g. openai, anthropic, openrouter)")
    base_url = str(base_url or "").strip()
    if not base_url.startswith("https://"):
        raise ProfileError("base_url must be an https:// URL")
    if not isinstance(models, list) or not models:
        raise ProfileError("models must be a non-empty list of model IDs")
    clean_models = []
    for m in models:
        m = str(m or "").strip()
        if not m:
            raise ProfileError("model IDs must be non-empty")
        if m not in clean_models:
            clean_models.append(m)
    return provider.strip(), base_url, clean_models, sanitize_headers(custom_headers)


# ── CRUD (owner-scoped; api_key is write-only) ─────────────────────────

def create_profile(owner: str, name: str, provider: str, base_url: str,
                   api_key: str, models: list, custom_headers: dict | None = None) -> dict:
    """Create a profile. Returns the public shape (never the key)."""
    if not str(api_key or "").strip():
        raise ProfileError("api_key is required")
    provider, base_url, models, headers = _validate_fields(
        provider, base_url, models, custom_headers)
    profiles = _load()
    pid = "prf_" + secrets.token_hex(6)
    now = _now_iso()
    profiles[pid] = {
        "id": pid,
        "owner": str(owner or ""),
        "name": str(name or "").strip() or provider,
        "provider": provider,
        "base_url": base_url,
        "api_key_encrypted": _encrypt(api_key.strip()),
        "api_key_last4": api_key.strip()[-4:],
        "models": models,
        "custom_headers": headers,
        "created_at": now,
        "updated_at": now,
    }
    _save(profiles)
    return _public(profiles[pid])


def update_profile(profile_id: str, owner: str, **fields) -> dict:
    """Update an owned profile. ``api_key`` is re-encrypted on change."""
    profiles = _load()
    profile = profiles.get(profile_id)
    if profile is None:
        raise ProfileError(f"provider profile {profile_id!r} not found")
    if not _owner_ok(profile.get("owner", ""), str(owner or "")):
        raise ProfileError(
            f"provider profile {profile_id!r} is not owned by {owner!r}"
        )
    allowed = {"name", "provider", "base_url", "api_key", "models", "custom_headers"}
    for k in fields:
        if k not in allowed:
            raise ProfileError(f"cannot update profile field {k!r}")
    if "api_key" in fields:
        new_key = str(fields.pop("api_key") or "").strip()
        if not new_key:
            raise ProfileError("api_key cannot be empty")
        profile["api_key_encrypted"] = _encrypt(new_key)
        profile["api_key_last4"] = new_key[-4:]
    merged = {
        "provider": fields.pop("provider", profile["provider"]),
        "base_url": fields.pop("base_url", profile["base_url"]),
        "models": fields.pop("models", profile["models"]),
        "custom_headers": fields.pop("custom_headers", profile["custom_headers"]),
    }
    provider, base_url, models, headers = _validate_fields(**merged)
    profile["provider"] = provider
    profile["base_url"] = base_url
    profile["models"] = models
    profile["custom_headers"] = headers
    if "name" in fields:
        profile["name"] = str(fields.pop("name") or "").strip() or profile["provider"]
    if fields:  # pragma: no cover — whitelist above makes this unreachable
        raise ProfileError(f"cannot update profile fields: {sorted(fields)}")
    profile["updated_at"] = _now_iso()
    _save(profiles)
    return _public(profile)


def delete_profile(profile_id: str, owner: str) -> dict:
    profiles = _load()
    profile = profiles.get(profile_id)
    if profile is None:
        raise ProfileError(f"provider profile {profile_id!r} not found")
    if not _owner_ok(profile.get("owner", ""), str(owner or "")):
        raise ProfileError(
            f"provider profile {profile_id!r} is not owned by {owner!r}"
        )
    profiles.pop(profile_id)
    _save(profiles)
    return _public(profile)


def get_profile(profile_id: str, owner: str = None) -> dict:
    """Return the public shape (never the key). Owner-checked when given."""
    profiles = _load()
    profile = profiles.get(profile_id)
    if profile is None:
        raise ProfileError(f"provider profile {profile_id!r} not found")
    if owner is not None and not _owner_ok(profile.get("owner", ""), str(owner or "")):
        raise ProfileError(
            f"provider profile {profile_id!r} is not owned by {owner!r}"
        )
    return _public(profile)


def list_profiles(owner: str = None) -> list:
    """Public shapes; filtered by *owner* when given."""
    profiles = _load()
    out = []
    for pid in sorted(profiles):
        if owner is not None and not _owner_ok(profiles[pid].get("owner", ""), str(owner or "")):
            continue
        out.append(_public(profiles[pid]))
    return out


def decrypt_key(profile_id: str) -> str:
    """Decrypt a profile's API key. Internal use only (resolution/child env)."""
    profiles = _load()
    profile = profiles.get(profile_id)
    if profile is None:
        raise ProfileError(f"provider profile {profile_id!r} not found")
    return _decrypt(profile.get("api_key_encrypted", ""))


# ── Bot resolution (the single chokepoint) ─────────────────────────────

def resolve_for_bot(bot: dict) -> ProviderConfig:
    """Resolve a Bot's provider profile into a :class:`ProviderConfig`.

    Rules (module docstring):
      * the Bot must have provider_profile_id and model configured;
      * the profile must exist;
      * the profile must belong to the Bot's owner (or the Bot is the
        operator, ``owner == ""``);
      * the Bot's model must be in the profile's validated model list.

    No global-env fallback exists on this path — every failure raises
    :class:`ProfileResolutionError` with a user-facing message.
    """
    bot_id = bot.get("id", "?")
    pid = str(bot.get("provider_profile_id") or "").strip()
    model = str(bot.get("model") or "").strip()
    if not pid:
        raise ProfileResolutionError(
            f"bot '{bot_id}' has no provider profile configured — set one "
            f"with: /setbot {bot_id} profile <prf_id>"
        )
    profiles = _load()
    profile = profiles.get(pid)
    if profile is None:
        raise ProfileResolutionError(
            f"bot '{bot_id}' references provider profile '{pid}' which does "
            "not exist"
        )
    if not _owner_ok(profile.get("owner", ""), str(bot.get("owner") or "")):
        raise ProfileResolutionError(
            f"provider profile '{pid}' is not owned by bot '{bot_id}' "
            f"(profile owner: {profile.get('owner', '')!r}, "
            f"bot owner: {bot.get('owner', '')!r})"
        )
    if not model:
        raise ProfileResolutionError(
            f"bot '{bot_id}' has no model configured — set one with: "
            f"/setbot {bot_id} model <model-id>"
        )
    if model not in profile.get("models", []):
        raise ProfileResolutionError(
            f"model '{model}' is not approved for profile "
            f"'{profile.get('name', pid)}' (approved: "
            f"{', '.join(profile.get('models', []))})"
        )
    return ProviderConfig(
        provider=profile["provider"],
        base_url=profile["base_url"],
        api_key=_decrypt(profile["api_key_encrypted"]),
        model=model,
        headers=dict(profile.get("custom_headers", {})),
        profile_id=pid,
        profile_name=profile.get("name", ""),
    )


def validate_bot_assignment(profile_id: str, owner: str, model: str) -> dict:
    """Validate a Bot→profile assignment at API/setbot time.

    Same rules as :func:`resolve_for_bot`, minus the bot itself: the
    profile must exist, be owned by *owner* (or *owner* is the operator
    ``""``), and *model* must be in the profile's model list. Returns
    the public profile shape so callers can echo the assignment back.
    """
    profile = get_profile(profile_id, owner=None)
    if not _owner_ok(profile.get("owner", ""), str(owner or "")):
        raise ProfileError(
            f"provider profile {profile_id!r} is not owned by {owner!r}"
        )
    model = str(model or "").strip()
    if not model:
        raise ProfileError("model is required when assigning a provider profile")
    if model not in profile["models"]:
        raise ProfileError(
            f"model '{model}' is not approved for profile "
            f"'{profile.get('name', profile_id)}' "
            f"(approved: {', '.join(profile['models'])})"
        )
    return profile
