"""bot_roles.py — server-side Bot routing/specialization labels (Kyrex Chat).

A Bot's role is a ROUTING/persona label, not the authorization boundary for
owner-connected services.  Gmail and Calendar availability are derived from
the OWNER's live connections; operation-level gates and approvals remain
server-authoritative.  Legacy preset policies are still recognised here for
backward compatibility, migration, repo/browser specialization, and truthful
reporting, but changing a role is no longer the way a user grants Gmail or
Calendar access.

The Chief-of-Staff roster and Chat roster read the SAME deterministic role
view.  No role may invent credentials, connector scopes, Browser Host bindings,
Rifts, or approval authority.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent            # web/backend/
_CLOUD_DIR = _SCRIPT_DIR.parent.parent                   # kyrex-cloud/
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

import serve as _serve  # noqa: E402


# ── Routing/specialization table ───────────────────────────────────────
# id -> {label, description, preset (legacy/backcompat), primary, internal}
ROLES: dict[str, dict] = {
    "chief-of-staff": {
        "label": "Chief of Staff",
        "preset": _serve.COORDINATOR_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Coordinates your other Bots and their status. Connected services "
            "belong to you, not to this role; Kyrex applies each service's "
            "operation gates and approvals when it is used."
        ),
    },
    "calendar": {
        "label": "Calendar Bot",
        "preset": _serve.CALENDAR_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Calendar-focused routing specialization. It is a useful destination "
            "for scheduling work, but your connected Calendar is owner-scoped "
            "and can be used safely from any of your running Bots."
        ),
    },
    "calendar-editor": {
        "label": "Calendar Editor",
        "preset": _serve.CALENDAR_EDITOR_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Calendar-delete routing specialization. Deletion is still an "
            "operation-level T2 action with an exact target and explicit owner "
            "approval, regardless of which Bot routes the request."
        ),
    },
    "developer": {
        "label": "Developer Bot",
        "preset": _serve.DEVELOPER_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Repository-focused routing specialization. Repo execution still "
            "requires its configured workspace/Rift; owner-connected services "
            "remain available independently of this role."
        ),
    },
    "browser": {
        "label": "Browser Bot",
        "preset": _serve.BROWSER_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Browser-focused routing specialization. Browser navigation still "
            "requires an explicit Browser Host binding and domain allowlist; "
            "connected account access is independent of the role."
        ),
    },
}

# Optional internal specialist labels (never offered as permissions).
INTERNAL_SPECIALISTS: dict[str, dict] = {
    "qa": {
        "label": "QA Specialist",
        "preset": None,
        "primary": False,
        "internal": True,
        "description": "Optional internal QA routing specialization.",
    },
    "security": {
        "label": "Security Specialist",
        "preset": None,
        "primary": False,
        "internal": True,
        "description": "Optional internal security routing specialization.",
    },
}

#: Every role id the server can report (primary + internal).
ALL_ROLE_IDS: tuple[str, ...] = tuple(
    list(ROLES) + list(INTERNAL_SPECIALISTS)
)

#: Legacy/backcompat role ids. Kept for old API clients and migration. The
#: current Chat UI no longer exposes them as a mutually-exclusive rights picker.
PRIMARY_ROLE_IDS: tuple[str, ...] = ("chief-of-staff", "calendar",
                                     "calendar-editor", "developer",
                                     "browser")


def _role_entry(role_id: str) -> dict | None:
    entry = ROLES.get(role_id)
    if entry is None:
        entry = INTERNAL_SPECIALISTS.get(role_id)
    return entry


def role_for_policy(policy) -> str:
    """The deterministic legacy/specialization role id for *policy*, or custom."""
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
    """True iff *policy* is shape-compatible with the legacy Developer preset."""
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
    """Deterministic non-secret legacy/specialization view for a Bot."""
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
        "label": "Custom Bot",
        "description": (
            "Custom routing/persona Bot. Owner-connected services are shared; "
            "repo/browser execution still follows its configured resources and "
            "operation gates."
        ),
        "primary": False,
        "internal": True,
        "preset": None,
    }


def capability_options() -> list[dict]:
    """Legacy role-change options for old API clients.

    These labels are retained for backward compatibility/migration only. The
    current Chat UI deliberately does not render them as a rights selector;
    owner-connected Gmail/Calendar authority is independent of this table.
    """
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


# ── Legacy calendar-kind detection (migration) ────────────────────────
LEGACY_CALENDAR_KINDS: dict[str, str] = {
    _serve.CALENDAR_READER_PRESET_ID: "can read a calendar",
    _serve.CALENDAR_WRITER_PRESET_ID: "can create calendar events",
    _serve.GLOFOX_READER_PRESET_ID: "can read the Glofox schedule",
    _serve.LEVEL6_WEEKLY_PRESET_ID: "can run the pinned Level 6 weekly capture",
    _serve.LEVEL6_CALENDAR_PRESET_ID: "can read the Level 6 workout week",
}


def legacy_calendar_kind(policy) -> str | None:
    """The legacy calendar-family kind *policy* matches, or ``None``."""
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


# ── Shared connected tools + Jev routing bootstrap ────────────────────
# chat_api imports chat_service + dev_bot before importing this module. Install
# owner-scoped connected-tool authority FIRST so Jev sees the same tool model
# Kyrex will actually execute. Both installers are idempotent.
def _install_shared_connected_tools() -> None:
    if "chat_api" not in sys.modules:
        return
    try:
        import chat_service as _chat_service
        import dev_bot as _dev_bot
        import shared_connected_tools as _shared
        _shared.install(_chat_service, _dev_bot)
    except Exception:
        # Startup remains fail-closed: without the shim, the older narrower
        # preset gates remain in force rather than widening authority.
        pass


def _install_jev_routing() -> None:
    if "chat_api" not in sys.modules:
        return
    try:
        import chat_service as _chat_service
        import dev_bot as _dev_bot
        import jev_stream_router as _jev_stream_router
        _jev_stream_router.install(_chat_service, _dev_bot)
        # After Jev is installed, strip legacy policy grants from its candidate
        # persona metadata. The Bot's safe name/id become the routing identity;
        # owner-connected tools remain in Jev's separate shared_tools state.
        import routing_identity as _routing_identity
        _routing_identity.install(
            _jev_stream_router, _chat_service.delegation, _serve)
    except Exception:
        pass


_install_shared_connected_tools()
_install_jev_routing()
