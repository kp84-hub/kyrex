"""bot_roles.py — legacy Bot role compatibility for Kyrex Chat.

Bot presets remain readable for backward-compatible registry/configuration
records, but they no longer define an owned Bot's runtime rights.  Owned Bots
are routing specializations; owner rights and operation tiers are installed by
``owner_bot_rights`` at startup.  The legacy role helpers below stay available
for migrations and old API consumers until those records are retired.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent            # web/backend/
_CLOUD_DIR = _SCRIPT_DIR.parent.parent                   # kyrex-cloud/
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

import serve as _serve  # noqa: E402


# ── Legacy capability table (compatibility only) ──────────────────────
ROLES: dict[str, dict] = {
    "chief-of-staff": {
        "label": "Chief of Staff",
        "preset": _serve.COORDINATOR_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Legacy coordinator preset. Runtime rights are now owner-scoped; "
            "Chief of Staff is a routing specialization."
        ),
    },
    "calendar": {
        "label": "Calendar Bot",
        "preset": _serve.CALENDAR_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Legacy Calendar preset. Google access is now owner-scoped and "
            "independent of Bot specialization."
        ),
    },
    "calendar-editor": {
        "label": "Calendar Editor",
        "preset": _serve.CALENDAR_EDITOR_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Legacy Calendar Editor preset. Delete safety remains operation-"
            "scoped with the existing T2 approval."
        ),
    },
    "developer": {
        "label": "Developer Bot",
        "preset": _serve.DEVELOPER_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Legacy repository-ready preset. Repository execution still needs "
            "a real workspace; runtime operation rights are owner-scoped."
        ),
    },
    "browser": {
        "label": "Browser Bot",
        "preset": _serve.BROWSER_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Legacy Browser preset. Browser use now depends on Browser Host "
            "and allowlist readiness, not Bot rights."
        ),
    },
}

INTERNAL_SPECIALISTS: dict[str, dict] = {
    "qa": {
        "label": "QA Specialist", "preset": None,
        "primary": False, "internal": True,
        "description": "Optional internal QA specialist label.",
    },
    "security": {
        "label": "Security Specialist", "preset": None,
        "primary": False, "internal": True,
        "description": "Optional internal security specialist label.",
    },
}

ALL_ROLE_IDS: tuple[str, ...] = tuple(list(ROLES) + list(INTERNAL_SPECIALISTS))
PRIMARY_ROLE_IDS: tuple[str, ...] = (
    "chief-of-staff", "calendar", "calendar-editor", "developer", "browser")


def _role_entry(role_id: str) -> dict | None:
    return ROLES.get(role_id) or INTERNAL_SPECIALISTS.get(role_id)


def role_for_policy(policy) -> str:
    """Legacy preset identity for migrations/compatibility, never authority."""
    try:
        if _serve.is_calendar_bot_policy(policy):
            return "calendar"
        if _serve.is_calendar_editor_policy(policy):
            return "calendar-editor"
        if _serve.is_coordinator_policy(policy):
            return "chief-of-staff"
        if _serve.is_writable_bot_policy(policy) and _valid_developer_shape(policy):
            return "developer"
        if _serve.is_browser_bot_policy(policy):
            return "browser"
    except Exception:
        return "custom"
    return "custom"


def _valid_developer_shape(policy) -> bool:
    try:
        if not isinstance(policy, dict):
            return False
        want = dict(_serve.DEVELOPER_PRESET)
        for op, tier in want.items():
            if policy.get(op) != tier:
                return False
        for op, tier in policy.items():
            if op in want:
                continue
            if isinstance(tier, int):
                return False
        return True
    except Exception:
        return False


def role_view(policy) -> dict:
    """Legacy, non-secret preset view retained for old API consumers."""
    role_id = role_for_policy(policy)
    entry = _role_entry(role_id)
    if entry is not None:
        return {
            "id": role_id,
            "label": entry["label"],
            "description": entry["description"],
            "primary": entry["primary"],
            "internal": entry["internal"],
            "preset": entry["preset"],
        }
    return {
        "id": "custom",
        "label": "Specialist",
        "description": (
            "Bot specialization is defined by its identity and model; runtime "
            "operation rights are owner-scoped."
        ),
        "primary": False,
        "internal": True,
        "preset": None,
    }


def capability_options() -> list[dict]:
    """Legacy API compatibility; the new Bots UI does not expose this list."""
    out = []
    for rid in PRIMARY_ROLE_IDS:
        entry = _role_entry(rid)
        out.append({
            "id": rid,
            "label": entry["label"],
            "description": entry["description"],
            "preset": entry["preset"],
        })
    return out


# ── Legacy calendar-kind detection (migration compatibility) ──────────
LEGACY_CALENDAR_KINDS: dict[str, str] = {
    _serve.CALENDAR_READER_PRESET_ID: "can read a calendar",
    _serve.CALENDAR_WRITER_PRESET_ID: "can create calendar events",
    _serve.GLOFOX_READER_PRESET_ID: "can read the Glofox schedule",
    _serve.LEVEL6_WEEKLY_PRESET_ID: "can run the pinned Level 6 weekly capture",
    _serve.LEVEL6_CALENDAR_PRESET_ID: "can read the Level 6 workout week",
}


def legacy_calendar_kind(policy) -> str | None:
    try:
        if _serve.is_calendar_reader_policy(policy):
            return _serve.CALENDAR_READER_PRESET_ID
        if _serve.is_calendar_writer_policy(policy):
            return _serve.CALENDAR_WRITER_PRESET_ID
        if _serve.is_glofox_reader_policy(policy):
            return _serve.GLOFOX_READER_PRESET_ID
        if _serve.level6_weekly_granted(policy):
            return _serve.LEVEL6_WEEKLY_PRESET_ID
        if _serve.level6_calendar_granted(policy):
            return _serve.LEVEL6_CALENDAR_PRESET_ID
    except Exception:
        return None
    return None


# ── Runtime installers ─────────────────────────────────────────────────
def _install_jev_routing() -> None:
    if "chat_api" not in sys.modules:
        return
    try:
        import chat_service as _chat_service
        import dev_bot as _dev_bot
        import jev_stream_router as _jev_stream_router
        _jev_stream_router.install(_chat_service, _dev_bot)
    except Exception:
        pass


def _install_owner_rights() -> None:
    """Install owner-scoped rights after Jev so wrapper order is deterministic."""
    if "chat_api" not in sys.modules:
        return
    try:
        import chat_service as _chat_service
        import dev_bot as _dev_bot
        import bot_capabilities as _bot_capabilities
        import delegation as _delegation
        import jev_stream_router as _jev_stream_router
        import owner_bot_rights as _owner_bot_rights
        _owner_bot_rights.install(
            _chat_service, _dev_bot, _bot_capabilities, _delegation, _serve,
            jev_stream_router=_jev_stream_router)
    except Exception:
        # Startup remains fail-safe: an installation fault leaves the legacy
        # policy behavior intact rather than partially widening authority.
        pass


_install_jev_routing()
_install_owner_rights()
