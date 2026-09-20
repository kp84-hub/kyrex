"""bot_roles.py — the server-side user-facing Bot role model (Kyrex Chat).

A Bot's user-facing NAME and DESCRIPTION come deterministically from this
module — never from free-text the bot owner typed, never from an LLM, and
never from a client-supplied label. The Chief of Staff calendar/roster
context (``chat_service._bot_roster_lines`` / ``build_coordinator_context``)
and the Chat roster read the SAME role view, so a report of a Bot's
capabilities is exactly what the server will enforce.

Roles:

  * PRIMARY user-facing roles (offered in the one Change capability control):
      - ``chief-of-staff``  (Coordinator preset — delegates, never writes)
      - ``calendar``        (unified Calendar preset — cal:list + cal:create
                             with exact-payload approval + glofox:read)
      - ``developer``       (Developer preset — file writes + PRs)
      - ``browser``         (Browser preset — read-only browsing)
  * OPTIONAL INTERNAL specialists (never offered in the primary UI; a Bot
    whose policy matches nothing is reported as ``custom`` with a truthful
    description derived from its actual effective permissions):
      - ``qa`` / ``security`` are recognised labels for internal reporting;
        no preset grants are invented for them here.

No second policy engine: role detection reuses the EXACT preset predicates in
serve.py (``is_calendar_bot_policy``, ``coordinator_granted``,
``is_writable_bot_policy``, ``is_browser_bot_policy`` …), so a role is only
ever claimed when the policy is byte-for-byte the server-defined grant.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent            # web/backend/
_CLOUD_DIR = _SCRIPT_DIR.parent.parent                   # kyrex-cloud/
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

import serve as _serve  # noqa: E402


# ── The capability table (single source of truth) ─────────────────────
# id -> {label, description, preset (or None), primary, internal}
ROLES: dict[str, dict] = {
    "chief-of-staff": {
        "label": "Chief of Staff",
        "preset": _serve.COORDINATOR_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Coordinates your other Bots: it can delegate work to them and "
            "read their status. It never writes files, browses, touches your "
            "calendar, or runs commands itself."
        ),
    },
    "calendar": {
        "label": "Calendar Bot",
        "preset": _serve.CALENDAR_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Reads your Google calendar (calendar: today / tomorrow / week), "
            "creates events from natural language — every create waits for "
            "your explicit approval of the exact event first — and reads the "
            "pinned Glofox schedule and Level 6 workout week on the same "
            "connected account."
        ),
    },
    "developer": {
        "label": "Developer Bot",
        "preset": _serve.DEVELOPER_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Works on a real repository: reads and writes files and opens "
            "pull requests. It runs only against its own workspace."
        ),
    },
    "browser": {
        "label": "Browser Bot",
        "preset": _serve.BROWSER_PRESET_ID,
        "primary": True,
        "internal": False,
        "description": (
            "Reads pages on the domains you allowlisted, through a Browser "
            "Host you explicitly bind. It never clicks, types, submits, or "
            "downloads."
        ),
    },
}

# Optional internal specialist labels (never offered in the primary control).
INTERNAL_SPECIALISTS: dict[str, dict] = {
    "qa": {
        "label": "QA Specialist",
        "preset": None,
        "primary": False,
        "internal": True,
        "description": (
            "Optional internal QA specialist role. Its capabilities are "
            "reported from its actual policy."
        ),
    },
    "security": {
        "label": "Security Specialist",
        "preset": None,
        "primary": False,
        "internal": True,
        "description": (
            "Optional internal security specialist role. Its capabilities "
            "are reported from its actual policy."
        ),
    },
}

#: Every role id the server can report (primary + internal).
ALL_ROLE_IDS: tuple[str, ...] = tuple(
    list(ROLES) + list(INTERNAL_SPECIALISTS)
)

#: The role ids offered by the one user-facing Change capability control.
PRIMARY_ROLE_IDS: tuple[str, ...] = ("chief-of-staff", "calendar",
                                     "developer", "browser")


def _role_entry(role_id: str) -> dict | None:
    entry = ROLES.get(role_id)
    if entry is None:
        entry = INTERNAL_SPECIALISTS.get(role_id)
    return entry


def role_for_policy(policy) -> str:
    """The deterministic role id for *policy*, or ``"custom"``.

    Fail closed: a role is claimed ONLY when the policy is byte-for-byte one
    of the server-defined presets (exact-match predicates in serve.py). A
    wildcard, an extra op, a malformed policy, or an exact match to a legacy
    preset (Calendar Reader / Writer, Glofox Reader, Level 6) reports
    ``custom`` — never a borrowed primary label.
    """
    try:
        if _serve.is_calendar_bot_policy(policy):
            return "calendar"
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
    """True iff *policy* is shape-compatible with the Developer preset.

    ``is_writable_bot_policy`` proves fs:write is granted; this adds the
    exact-preset shape check (fs:read + repo:read + fs:write + repo:pr, and
    no other granted op) so a policy that merely grants fs:write (e.g. a
    hand-written write grant) is reported as ``custom`` with a truthful
    permissions-based description rather than labelled Developer.
    """
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
    """The deterministic, non-secret role view a Bot reports.

    Always carries id/label/description; ``primary`` and ``internal`` come
    from the capability table. A ``custom`` role gets a truthful description
    built from the policy's actual effective permissions (the SAME engine the
    executor enforces) — never the policy rules themselves.
    """
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
    granted = []
    try:
        perms = _serve.effective_permissions(policy)
    except Exception:
        perms = {}
    for op in sorted(perms):
        if isinstance(perms.get(op), int) and perms[op] == 0:
            granted.append(op)
    if granted:
        description = "Custom role. Grants: " + ", ".join(
            sorted(granted)) + "."
    else:
        description = "Custom role with no auto-granted capabilities."
    return {
        "id": "custom",
        "label": "Custom Bot",
        "description": description,
        "primary": False,
        "internal": True,
        "preset": None,
    }


def capability_options() -> list[dict]:
    """The PRIMARY capability choices for the one Change capability control."""
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
# The migration surface consolidates the LEGACY calendar-family presets into
# one unified Calendar Bot. Detection reuses the same exact-match predicates
# so a bot that merely resembles a legacy preset is never touched.

LEGACY_CALENDAR_KINDS: dict[str, str] = {
    _serve.CALENDAR_READER_PRESET_ID: "can read a calendar",
    _serve.CALENDAR_WRITER_PRESET_ID: "can create calendar events",
    _serve.GLOFOX_READER_PRESET_ID: "can read the Glofox schedule",
    _serve.LEVEL6_WEEKLY_PRESET_ID: "can run the pinned Level 6 weekly capture",
    _serve.LEVEL6_CALENDAR_PRESET_ID: "can read the Level 6 workout week",
}


def legacy_calendar_kind(policy) -> str | None:
    """The legacy calendar-family kind *policy* matches, or ``None``.

    Returns the LEGACY preset id (calendar-reader / calendar-writer /
    glofox-reader / level6-weekly / level6-calendar) when the policy is
    EXACTLY that preset. The unified Calendar preset and everything else
    return ``None``.
    """
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