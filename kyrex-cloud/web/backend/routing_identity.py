"""Routing identity independent of legacy per-Bot permission presets.

The old UI could leave an Email-named Bot carrying a Calendar preset. That
stored policy remains valid for backward compatibility, but it must not tell
Jev that the Bot's PERSONA is Calendar or that connected services are exclusive
capabilities. This shim keeps safe routing metadata to identity/specialization
signals and leaves owner-connected tool availability in Jev's separate
``shared_tools`` state.
"""
from __future__ import annotations

import re

_installed = False


def _routing_role(serve, bot: dict) -> str:
    bot = bot or {}
    # Coordinator is a real routing role because bot:delegate determines which
    # Bot may orchestrate peers; it is not a connected-service permission.
    try:
        if serve.coordinator_granted(bot):
            return "chief-of-staff"
    except Exception:
        pass

    text = " ".join((
        str(bot.get("id") or ""),
        str(bot.get("name") or ""),
    )).lower()
    if re.search(r"\b(?:email|gmail|mailbox|mail)\b", text):
        return "email"
    if re.search(r"\b(?:calendar|schedule|scheduling)\b", text):
        return "calendar"
    if re.search(r"\b(?:browser|web)\b", text):
        return "browser"
    if re.search(r"\b(?:developer|dev|code|coding)\b", text):
        return "developer"
    return "custom"


def install(jev_stream_router, delegation, serve) -> None:
    global _installed
    if _installed:
        return

    # visible_targets/safe_bot_metadata call these functions dynamically. The
    # old policy capability list is deliberately empty for ROUTING metadata:
    # owner-connected tools live in Jev state.shared_tools, and repo/browser
    # resource requirements are enforced when Kyrex actually executes.
    delegation.capability_labels = lambda policy: []
    delegation.role_label = lambda bot: _routing_role(serve, bot)

    # For the currently-bound coordinator `_candidate` has no visible-targets
    # metadata. Do not fall back to a policy-derived Calendar/Developer persona;
    # its safe NAME remains available, and peer candidates receive the routing
    # role above. This prevents stale legacy presets from steering Jev.
    jev_stream_router._role_view = lambda policy: {}

    # Extend Jev's separate shared-tools state with the newly owner-scoped
    # Calendar surfaces. This is DESCRIPTIVE routing metadata only; execution
    # re-checks lifecycle, connector scope, exact intent, and approvals.
    original_shared_tools = jev_stream_router._shared_tools

    def shared_tools(dev_bot, bot):
        tools = list(original_shared_tools(dev_bot, bot) or [])
        try:
            if dev_bot.calendar_route_ready(bot) and "calendar_read" not in tools:
                tools.append("calendar_read")
        except Exception:
            pass
        try:
            if (dev_bot.calendar_editor_route_ready(bot)
                    and "calendar_delete" not in tools):
                tools.append("calendar_delete")
        except Exception:
            pass
        return tools

    jev_stream_router._shared_tools = shared_tools
    _installed = True
