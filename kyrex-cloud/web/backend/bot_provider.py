"""bot_provider.py — resolve one Bot's LLM configuration from its profile.

Per-Bot LLM configuration: a Bot stores ONLY a reference
(``provider_profile_id``) and the exact ``model`` it uses. The secrets — API
key, base URL, extra headers — live in the encrypted per-user provider
profile store (``provider_profiles.py``) and are never copied into the Bot
registry.

This module is the single resolver both turn paths use:

    Bot (registry)                bots.get_bot / resolve_bot_for_user
      -> provider_profile_id      reference (never a secret)
      -> encrypted profile        provider_profiles.get_profile(user, id)
      -> {provider, base_url, api_key, headers, model}

Fail-closed contract (never silently falls back to globals):

  * A Bot with NO ``provider_profile_id`` raises :class:`BotProviderError`.
    It is NOT served with ``KYREX_PROVIDER`` / ``KYREX_API_KEY`` /
    ``KYREX_MODEL`` — an unconfigured Bot fails clearly.
  * A Bot whose referenced profile is missing, belongs to another user, or
    whose stored ``model`` is not in that profile's model list raises
    :class:`BotProviderError`.
  * A profile with no API key raises :class:`BotProviderError`.

The resolver returns decrypted secrets for INTERNAL use only. Any value that
crosses the API boundary must come from :func:`bot_provider_view`, which
exposes identity/routing metadata and non-secret hints (last four of the key,
header NAMES) and never a key or header value.
"""

from __future__ import annotations

import sys
from pathlib import Path

# kyrex-cloud/web/backend/bot_provider.py sits inside kyrex-cloud/ — resolve
# the Cloud package the same way chat_service.py / dev_bot.py do.
_SCRIPT_DIR = Path(__file__).resolve().parent            # web/backend/
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import provider_profiles as _profiles  # noqa: E402


class BotProviderError(Exception):
    """The Bot's LLM configuration cannot be resolved — fail closed.

    Raised instead of substituting any global/env provider. The caller turns
    this into a clear, user-visible failure (ChatUnavailable on the turn path).
    """


def _model_name(model: str) -> str:
    """Extract the bare model name from a ``provider:model`` or ``model``str.

    The registry stores the exact model the Bot runs; a ``provider:model``
    prefix is honoured (the text after the first colon) so legacy Bot model
    strings keep working, but the name is what must belong to the profile.
    """
    raw = str(model or "").strip()
    if ":" in raw:
        _, _, tail = raw.partition(":")
        return tail.strip()
    return raw


def resolve_bot_provider(user: str, bot: dict) -> dict:
    """Resolve *bot*'s provider configuration for *user*, fail-closed.

    Returns ``{"provider", "base_url", "api_key", "headers", "model",
    "profile_id", "profile"}`` with decrypted secrets for internal use.

    Raises:
        BotProviderError: the Bot is unconfigured, references a profile that
        does not exist for *user*, has a model outside the profile, or the
        profile has no API key.
    """
    bot = bot or {}
    bot_id = str(bot.get("id") or "").strip()
    profile_id = str(bot.get("provider_profile_id") or "").strip().lower()
    model = str(bot.get("model") or "").strip()

    if not profile_id:
        raise BotProviderError(
            f"bot {bot_id!r} has no provider profile configured — "
            "assign one before using it in Kyrex Chat"
        )
    if not model:
        raise BotProviderError(f"bot {bot_id!r} has no model configured")

    profile = _profiles.get_profile(user, profile_id)
    if profile is None:
        raise BotProviderError(
            f"bot {bot_id!r} references provider profile {profile_id!r}, "
            "which is not configured for this user"
        )

    models = [
        str(m).strip() for m in (profile.get("models") or []) if str(m).strip()
    ]
    model_name = _model_name(model)
    if model_name not in models:
        raise BotProviderError(
            f"model {model_name!r} is not available on provider profile "
            f"{profile_id!r}"
        )

    api_key = str(profile.get("api_key") or "").strip()
    if not api_key:
        raise BotProviderError(
            f"provider profile {profile_id!r} is not configured (no API key)"
        )

    headers = profile.get("headers")
    if not isinstance(headers, dict):
        headers = {}

    return {
        "provider": str(profile.get("provider") or "openai").strip().lower(),
        "base_url": str(profile.get("base_url") or "").strip(),
        "api_key": api_key,
        "headers": {str(k): str(v) for k, v in headers.items()},
        "model": model_name,
        "profile_id": profile_id,
        # Alias kept for callers that key on "profile" (matches the
        # chat_service provider-config shape).
        "profile": profile_id,
    }


def bot_provider_view(user: str, bot: dict) -> dict:
    """The NON-SECRET provider summary for one Bot's API payload.

    Never includes the API key or any header value. When the Bot is
    unconfigured (or the referenced profile is gone) the caller still gets a
    stable shape so the UI can explain why the Bot cannot serve a turn.
    """
    bot = bot or {}
    profile_id = str(bot.get("provider_profile_id") or "").strip().lower()
    model = str(bot.get("model") or "").strip() or None
    if not profile_id:
        return {"configured": False, "profile": None, "model": model}
    profile = _profiles.get_profile(user, profile_id)
    if profile is None:
        return {
            "configured": False,
            "profile": {"id": profile_id, "missing": True},
            "model": model,
        }
    return {
        "configured": True,
        "profile": _profiles.public_profile(profile),
        "model": model,
    }
