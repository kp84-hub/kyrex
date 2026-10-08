#!/usr/bin/env python3
"""Bounded Calendar Editor: exact-target deletion and notes/location updates.

Deletes retain the destructive T2 gate. Updates accept only location and notes,
append notes by default, and require a T1 preview of the exact patch. Titles
must resolve to one event; ambiguous matches are never guessed. Executors
accept exact IDs only. The connector preserves other fields through PATCH
and rejects concurrent changes using the approved event's ETag.
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
    title = _CALENDAR_SUFFIX_RE.sub("", m.group("title")).strip()
    title = title.strip("\"'“”").strip()
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
            f"choose an exact id. Candidates: {listed}")
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


class CalendarEditClarification(CalendarEditorError):
    def __init__(self, question, draft):
        super().__init__(question)
        self.draft = draft


def update_shaped(text):
    raw = str(text or '').strip()
    return bool(re.match(r'^(?:update|edit|change|set|replace)\b.*\b(?:location|address|notes?|description)\b|^(?:(?:can|could) you\s+)?add\b.*\b(?:location|address|notes?)\b|^here is the address\b', raw, re.I))


def edit_reply(text, draft):
    if not isinstance(draft, dict):
        return False
    return not re.match(r"^(?:show|read|list|check|create|schedule|book|delete|cancel|never mind|nevermind)\b", str(text or "").strip(), re.I)


def validate_update_intent(intent):
    if not isinstance(intent, dict) or set(intent) - {'op', 'event_id', 'title', 'location', 'notes', 'replace_notes'}:
        raise CalendarEditorError('An update accepts only a target, location, and notes.')
    target = validate_intent({k:intent.get(k) for k in ('event_id', 'title')})
    output = {'op':'update', **target}
    for field in ('location', 'notes'):
        if field in intent:
            value = intent[field]
            if not isinstance(value, str) or len(value) > 8000 or '\x00' in value:
                raise CalendarEditorError(f'{field} must be text of at most 8000 characters')
            output[field] = value
    if not {'location','notes'} & output.keys():
        raise CalendarEditClarification('What address or note should I add?', output)
    if type(intent.get('replace_notes', False)) is not bool:
        raise CalendarEditorError('replace_notes must be a boolean')
    output['replace_notes'] = intent.get('replace_notes', False)
    return output


def normalize_update_request(text, *, context=None, pending=None):
    raw = str(text or '').strip()
    if raw.startswith('{'):
        try:
            return validate_update_intent(json.loads(raw))
        except ValueError:
            raise CalendarEditorError('The update intent is not valid JSON') from None
    if pending:
        field = pending.get('field', 'notes')
        value = re.sub(r'^here is the address\s*[:]?\s*', '', raw, flags=re.I)
        return validate_update_intent({k:v for k,v in {**pending, field:value}.items() if k != 'field'})
    raw = re.sub(r'^(?:can|could) you\s+', '', raw, flags=re.I).rstrip('?')
    raw = re.sub(r'\.\s*like the address.*$', '', raw, flags=re.I)
    raw = raw.rstrip('.')
    # Canonical exact-target form supports deterministic delegations.
    match = re.fullmatch(r'(?:update|edit|change|set|replace)\s+(?:calendar\s+)?event\s+id\s+(\S+)\s+(location|address|notes?|description)\s+(?:to\s+)?(.+)', raw, re.I)
    if match:
        eid, field, value = match.groups()
        return validate_update_intent({'event_id':eid, 'location' if field.lower() in {'location','address'} else 'notes':value.strip('"'), 'replace_notes':raw.lower().startswith('replace ')})
    reverse = re.fullmatch(r'(set|change|update|replace)\s+(?:(?:the|a)\s+)?(notes?|address|location)\s+(?:of|for|on)\s+(.+?)\s+to\s+(.+)', raw, re.I)
    if reverse:
        verb, field, target, value = reverse.groups()
        raw = f'{verb} {field} {value} to {target}'
    match = re.fullmatch(r'(add|replace|set|change|update)\s+(?:(?:the|a)\s+)?(notes?|address|location)\s*(.*?)\s+(?:to|on|for|of)\s+(.+)', raw, re.I)
    if not match:
        raise CalendarEditorError('Which event should I update, and what address or note should I use?')
    verb, field, value, target = match.groups()
    target = target.strip('"“”')
    if target.lower() in {'it','that','that event','this event','the appointment'}:
        if not isinstance(context, dict) or not context.get('event_id'):
            raise CalendarEditorError('Which appointment should I update? Give its title or exact event id.')
        intent = {'event_id':context['event_id']}
    else:
        exact = re.fullmatch(r'(?:calendar\s+)?event\s+id\s+(\S+)', target, re.I)
        intent = {'event_id':exact[1]} if exact else {'title':target}
    field = 'location' if field.lower() in {'address','location'} else 'notes'
    intent.update(op='update', replace_notes=verb.lower() == 'replace')
    if not value.strip() or value.strip().lower() in {'to it','like the address'}:
        raise CalendarEditClarification('What address or note should I add?', {**intent,'field':field})
    intent[field] = re.sub(r'^(?:to|as)\s+', '', value.strip(), flags=re.I).strip('"“”')
    return validate_update_intent(intent)


def validate_patch(patch):
    if not isinstance(patch, dict) or not patch or set(patch) - {'location','description'}:
        raise CalendarEditorError('Only location and notes may be patched')
    if any(not isinstance(v, str) or len(v) > 16000 or '\x00' in v for v in patch.values()):
        raise CalendarEditorError('Invalid update field text')
    return dict(patch)


def build_update_patch(event, intent):
    intent = validate_update_intent(intent)
    if event.get('id') != intent.get('event_id'):
        raise CalendarEditorError('The update target differs from the selected event')
    patch = {}
    if 'location' in intent:
        patch['location'] = intent['location']
    if 'notes' in intent:
        existing = event.get('description') or ''
        if not isinstance(existing, str):
            raise CalendarEditorError('Could not read the existing notes safely')
        patch['description'] = intent['notes'] if intent['replace_notes'] or not existing else existing + '\n' + intent['notes']
    return validate_patch(patch)


def load_update_task(raw):
    obj = raw if isinstance(raw, dict) else json.loads(raw)
    intent = validate_update_intent(obj)
    if not intent['event_id']:
        raise CalendarEditorError('Resolve the event to an exact id before updating')
    return intent
