#!/usr/bin/env python3
"""cal_delete_preflight.py — owner-scoped title→event preflight for a DELETE.

The SINGLE source of the owner-scoped, NON-DESTRUCTIVE calendar read used to
disambiguate a calendar DELETE title BEFORE any executor runs. Shared by the
direct Chat ``calendar_delete`` route (chat_service) and by Bot-to-Bot
delegation (delegation), so a delegated delete resolves the SAME way a direct
one does: against the OWNER's PREFERRED calendar, to EXACTLY ONE event, failing
closed on ZERO or MULTIPLE matches (the error lists the candidates so the owner
can disambiguate by id).

It is a HOST-side preflight, not a bot op: it grants no capability, performs no
write, and is reachable only after ``cal_editor.normalize_delete_request`` /
``cal_editor.validate_intent`` has produced a validated intent. The executor it
feeds still deletes by EXACT event id only, behind its mandatory T2 approval.
"""
from __future__ import annotations

import sys
from pathlib import Path

_CLOUD_DIR = Path(__file__).resolve().parent
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

import cal_editor  # noqa: E402


def resolve_owner_event(owner, intent, bot=None) -> dict:
    """Resolve a validated delete intent to ONE of the OWNER's events.

    Reads the owner-scoped calendar (never a foreign one) and returns the ONE
    matching event. An AMBIGUOUS title raises ``CalendarEditorError`` carrying
    the CANDIDATE list -- the caller returns the candidates with NO task and NO
    approval gate; a unique title returns that ONE event, which the executor
    then previews at the T2 gate. A read failure fails closed with usage.
    """
    intent = cal_editor.validate_intent(intent)   # ONLY a normalized intent
    owner = str(owner or "").strip()
    try:
        import connectors
        connector_store = connectors.default_store()
        calendar_id = connector_store.preferred_calendar(owner, "google")
        query = intent.get("title") or None
        events = connector_store.calendar(owner).events(
            max_results=100, calendar_id=calendar_id, query=query)
    except Exception:
        raise cal_editor.CalendarEditorError(
            "I could not read your calendar to resolve that title. Provide an "
            "exact event id instead: delete calendar event id <event-id>")
    # Record the non-destructive delete-preflight read on the audit trail.
    try:
        import audit as _audit
        _audit.log(
            bot_id=str((bot or {}).get("id") or "calendar-editor"),
            operation="cal.delete_preflight",
            tier="tier0",
            decision="auto",
            outcome="owner-scoped non-destructive event lookup for delete "
                    "disambiguation",
        )
    except Exception:
        pass
    return cal_editor.target_event(events, intent)