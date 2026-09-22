#!/usr/bin/env python3
"""cal_editor.py — bounded, deterministic Calendar EDITOR core (delete only).

Pure, dependency-free heart of the Calendar Editor capability: it turns the
OWNER's own text into ONE safe DELETE intent that targets an EXACT Google
Calendar event id (or a title that must be DISAMBIGUATED), renders the
user-visible PREVIEW shown at the explicit T2 approval gate, and formats the
safe receipt. Nothing here touches the network or a model.

Boundary (deliberately narrow): DELETE only, on the owner's PRIMARY calendar.
Creating, updating, moving, inviting, or reading are rejected. A title that
matches MORE THAN ONE event is never guessed -- it fails closed and lists the
candidates so the owner disambiguates by id.

Evidence rule: an event is NEVER described as a "Level 6" event unless its OWN
title carries the Level 6 workout evidence (``Level 6 Workout:``). A bare
mention of Level 6 in a request is NOT evidence.
"""
from __future__ import annotations

import json
import re

CALENDAR_ID = "primary"
#: The mandatory confirmation tier for a DELETION (destructive): T2.
APPROVAL_TIER = 2
MAX_TITLE_CHARS = 200

#: The Level 6 workout event-title evidence. Only a title with this exact
#: prefix may be presented as Level 6-sourced.
LEVEL6_TITLE_PREFIX = "Level 6 Workout:"

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_@.+-]{5,1024}$")
_ID_INTENT_RE = re.compile(
    r"^\s*(?:remove|delete|cancel|drop|clear|erase)\s+"
    r"(?:the\s+)?(?:calendar\s+)?event\s+(?:with\s+)?id\s+"
    r"[\"']?(?P<id>[A-Za-z0-9_@.+-]{5,1024})[\"']?\s*$",
    re.IGNORECASE)
_TITLE_INTENT_RE = re.compile(
    r"^\s*(?:remove|delete|cancel|drop|clear|erase)\s+"
    r"(?:(?:this|it)\s+)?"
    r"(?:from\s+(?:the\s+|my\s+)?calendar\s+)?"
    r"(?:the\s+)?"
    r"(?P<title>.+?)\s*$",
    re.IGNORECASE)
_CALENDAR_SUFFIX_RE = re.compile(
    r"\s+from\s+(?:the\s+|my\s+)?calendar\s*[.!?]*\s*$",
    re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class CalendarEditorError(Exception):
    """A delete request, target, or intent is refused (fail closed)."""


def is_level6_workout_title(title) -> bool:
    """True only when *title* carries the Level 6 workout evidence."""
    return str(title or "").strip().startswith(LEVEL6_TITLE_PREFIX)


# ── Intent ──────────────────────────────────────────────────────────────

def normalize_delete_request(text) -> dict:
    """Normalise the OWNER'S text into ONE safe delete intent.

    Two accepted shapes (anything else fails closed):
      * "delete calendar event id <event-id>"  -- exact-ID targeting;
      * "remove this from calendar <title>"    -- a title to disambiguate.
    """
    raw = str(text or "").strip()
    if not raw:
        raise CalendarEditorError("the request is empty")
    m = _ID_INTENT_RE.match(raw)
    if m:
        event_id = m.group("id").strip()
        if not _EVENT_ID_RE.match(event_id):
            raise CalendarEditorError(f"invalid event id {event_id!r}")
        return {"event_id": event_id, "title": None}
    m = _TITLE_INTENT_RE.match(raw)
    if not m:
        raise CalendarEditorError(
            "I could not read that as a calendar delete request. Use one of:\n"
            "  delete calendar event id <event-id>\n"
            "  remove this from calendar <event title>")
    title = m.group("title").strip().strip("\"'\u201c\u201d").strip()
    if not title:
        raise CalendarEditorError("the event title is empty")
    if len(title) > MAX_TITLE_CHARS:
        raise CalendarEditorError(
            f"the title is too long (max {MAX_TITLE_CHARS} characters)")
    if _CONTROL_RE.search(title):
        raise CalendarEditorError("the title contains control characters")
    return {"event_id": None, "title": title}


def validate_intent(intent) -> dict:
    if not isinstance(intent, dict):
        raise CalendarEditorError("a delete intent must be an object")
    unknown = sorted(set(intent) - {"event_id", "title"})
    if unknown:
        raise CalendarEditorError(
            f"unsupported field(s): {', '.join(unknown)} -- this slice supports "
            "only event_id and title")
    event_id = intent.get("event_id")
    if event_id is not None:
        event_id = str(event_id).strip()
        if not _EVENT_ID_RE.match(event_id):
            raise CalendarEditorError(f"invalid event id {event_id!r}")
        return {"event_id": event_id, "title": None}
    title = intent.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CalendarEditorError(
            "a delete intent needs an exact event_id or a title")
    title = title.strip()
    if len(title) > MAX_TITLE_CHARS:
        raise CalendarEditorError(
            f"the title is too long (max {MAX_TITLE_CHARS} characters)")
    if _CONTROL_RE.search(title):
        raise CalendarEditorError("the title contains control characters")
    return {"event_id": None, "title": title}


def load_intent(raw) -> dict:
    if isinstance(raw, dict):
        return validate_intent(raw)
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        raise CalendarEditorError("the delete intent is not valid JSON")
    return validate_intent(parsed)


def _norm_title(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def target_event(events, intent) -> dict:
    """Resolve the ONE event the intent names, fail closed.

    Exact event-id targeting wins outright. A title must resolve to EXACTLY
    one event: zero matches AND more than one match both fail closed -- an
    ambiguous title is never guessed (the error lists the candidates so the
    owner can disambiguate by id).
    """
    intent = validate_intent(intent)
    items = [e for e in (events or []) if isinstance(e, dict)]
    if intent["event_id"]:
        wanted = intent["event_id"]
        for e in items:
            if str(e.get("id") or "").strip() == wanted:
                return e
        raise CalendarEditorError(
            f"no event on the primary calendar has id {wanted!r}")
    wanted_title = _norm_title(intent["title"])
    matches = [e for e in items
               if _norm_title(e.get("summary")) == wanted_title]
    if not matches:
        raise CalendarEditorError(
            "no event on the primary calendar is titled "
            f"\u201c{intent['title']}\u201d")
    if len(matches) > 1:
        listed = "; ".join(
            f"{str(e.get('id') or '?')} ({_human_when(e)})" for e in matches[:5])
        raise CalendarEditorError(
            f"{len(matches)} events are titled \u201c{intent['title']}\u201d -- "
            f"delete needs an exact id. Candidates: {listed}")
    return matches[0]


# ── Preview / evidence / receipt ────────────────────────────────────────

def _human_when(event) -> str:
    start = event.get("start") if isinstance(event, dict) else None
    if not isinstance(start, dict):
        return "unknown time"
    if start.get("date"):
        return f"{start['date']} (all day)"
    dt = str(start.get("dateTime") or "")
    return dt.replace("T", " ")[:16] or "unknown time"


def _valid_event(event) -> dict:
    if not isinstance(event, dict):
        raise CalendarEditorError("no event to delete")
    event_id = str(event.get("id") or "").strip()
    if not _EVENT_ID_RE.match(event_id):
        raise CalendarEditorError(f"invalid or missing event id: {event_id!r}")
    summary = str(event.get("summary") or event_id).strip()
    out = {"id": event_id, "summary": summary}
    if isinstance(event.get("start"), dict):
        out["start"] = event["start"]
    return out


def build_preview(event) -> dict:
    """The user-visible PREVIEW shown at the T2 approval gate.

    ``level6`` is True ONLY with the Level 6 workout evidence on the event's
    OWN title -- a bare Level 6 mention is never presented as evidence.
    """
    event = _valid_event(event)
    title = event["summary"]
    level6 = is_level6_workout_title(title)
    return {
        "title": title,
        "event_id": event["id"],
        "when": _human_when(event),
        "calendar": CALENDAR_ID,
        "level6": level6,
        "level6_evidence": LEVEL6_TITLE_PREFIX if level6 else None,
    }


def preview_display(preview) -> str:
    """The human-readable preview text (also the approval gate detail)."""
    return "\n".join([
        f"delete: {preview['title']}",
        f"event id: {preview['event_id']}",
        f"when: {preview['when']}",
        f"calendar: {preview['calendar']}",
        ("source: Level 6 workout (confirmed by the event title)"
         if preview["level6"]
         else "source: not a Level 6 workout (title carries no Level 6 evidence)"),
    ])


def payload_identity(event) -> str:
    """A stable identity for the EXACT event shown/approved at the gate."""
    preview = build_preview(event)
    return f"{preview['event_id']}|{preview['title']}"


def summary_line(event) -> str:
    preview = build_preview(event)
    return (f"delete calendar event \u201c{preview['title']}\u201d "
            f"\u2014 {preview['when']}")


def format_receipt(event) -> str:
    preview = build_preview(event)
    return (f"\u2705 Deleted \u201c{preview['title']}\u201d "
            f"\u2014 {preview['when']} (ref: {preview['event_id']})")


# ── Executor boundary ───────────────────────────────────────────────────

def load_event(raw) -> dict:
    """Load the ALREADY-DISAMBIGUATED event a delete should target.

    Accepts a JSON object ``{"event": {...}}`` or ``{"id":..., "summary":...}``
    (the workflow's resolved event), or a request text whose EXACT-ID form
    names the event directly. A title-only text is REFUSED here: the executor
    deletes by EXACT id only; disambiguation happens in the workflow.
    """
    if isinstance(raw, dict):
        obj = raw.get("event") if isinstance(raw.get("event"), dict) else raw
        return _valid_event(obj)
    text = str(raw or "").strip()
    if not text:
        raise CalendarEditorError("the request is empty")
    if text[:1] in "{[":
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            raise CalendarEditorError("the delete payload is not valid JSON")
        return load_event(parsed)
    if _EVENT_ID_RE.match(text):
        # A bare exact event id is a valid direct target for the executor.
        return _valid_event({"id": text, "summary": text})
    intent = normalize_delete_request(text)
    if not intent["event_id"]:
        raise CalendarEditorError(
            "delete by title must be resolved to an exact event id before the "
            "executor runs; this boundary deletes by exact id only")
    return _valid_event({"id": intent["event_id"], "summary": intent["event_id"]})


def intent_from_task(raw) -> dict:
    """The executor's entry point: the resolved event to delete."""
    return load_event(raw)
