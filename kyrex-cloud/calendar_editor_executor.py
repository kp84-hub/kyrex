#!/usr/bin/env python3
"""calendar_editor_executor.py — the Calendar EDITOR executor (exact-target delete and notes/location update).

Deletes ONE event from the OWNER's PRIMARY calendar, by EXACT Google Calendar
event id, through the owner's own event-write authorization, and NOTHING else.
Distinct from the read-only Calendar Reader (cal_executor.py) and the create-only
Calendar Writer (calendar_writer_executor.py).

Flow (each step fails closed):

  1. Resolve the event to delete from ``--task`` (an already-disambiguated
     event payload, or the owner's exact-ID request text) via cal_editor.
  2. Require an owner-scoped Bot identity (``KYREX_BOT_OWNER``).
  3. Announce ``cal.delete`` so the host classifies the operation and consume
     the host's decision.
  4. MANDATORY T2 APPROVAL GATE: show the user-visible PREVIEW (the exact
     event id/title/when and the Level 6 evidence, or its absence) and block on
     the owner's explicit approval. A deny (or timeout) means NOTHING is
     deleted and NO provider call is made.
  5. Delete via the owner's Calendar EDITOR connector (missing/expired write
     authorization fails closed).
  6. Emit exactly one ``KYREX_RESULT_JSON`` with a safe, readable receipt.

Protocol: ``KYREX_PROGRESS:`` / ``KYREX_OPERATION:`` / ``KYREX_APPROVAL:`` lines
during work and exactly one ``KYREX_RESULT_JSON:`` line at the end on stdout.
"""
import argparse
import json
import os
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import cal_editor  # noqa: E402


def _emit(kind: str, payload: dict) -> None:
    print(f"{kind}:{json.dumps(payload)}", flush=True)


def _read_decision() -> str:
    return sys.stdin.readline().strip()


def _fail(reason: str) -> int:
    _emit("KYREX_RESULT_JSON", {"status": "error", "final_response": "",
                                "errors": [reason]})
    return 0


def run_update(obj):
    try:
        intent = cal_editor.load_update_task(obj)
    except (ValueError, cal_editor.CalendarEditorError) as exc:
        return _fail(str(exc))
    owner = str(os.environ.get("KYREX_BOT_OWNER") or "").strip()
    if not owner:
        return _fail("calendar updates require an owner-scoped Bot identity")
    _emit("KYREX_OPERATION", {"op":"cal.update", "target":intent["event_id"],
                              "summary":"update event notes/location"})
    if _read_decision() not in ("ALLOW", "APPROVE"):
        return _fail("calendar update denied by host policy")
    try:
        import connectors
        editor = connectors.default_store().calendar_editor(owner)
        event = editor.get_event_for_update(intent["event_id"])
        patch = cal_editor.build_update_patch(event, intent)
    except Exception as exc:
        return _fail(f"calendar update preflight failed: {exc}")
    _emit("KYREX_APPROVAL", {"tier":1,
        "summary":f"Update {event.get('summary') or intent['event_id']}",
        "detail":json.dumps({"event_id":intent["event_id"],
                             "title":event.get("summary"), "when":event.get("start"),
                             "changes":patch}, ensure_ascii=False)})
    if _read_decision() != "APPROVED":
        return _fail("calendar update not approved — nothing was changed")
    try:
        updated = editor.update_event(intent["event_id"], patch, etag=event["etag"])
    except Exception as exc:
        return _fail(f"calendar update failed: {exc}. Reload the event and approve a new preview if it changed.")
    _emit("KYREX_RESULT_JSON", {"status":"ok", "event_id":intent["event_id"],
        "final_response":f"Updated notes/location for “{updated.get('summary') or intent['event_id']}” (ref: {intent['event_id']}).",
        "errors":[]})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Kyrex Cloud Calendar Editor Executor")
    ap.add_argument("--task", required=True,
                    help="an event payload (JSON) or the owner's request text")
    ap.add_argument("--repo-url", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--base", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    try:
        obj = json.loads(args.task)
    except ValueError:
        obj = {}
    if isinstance(obj, dict) and obj.get("op") == "clarify_update":
        draft = obj.get("draft")
        try:
            cal_editor.validate_intent({k:draft.get(k) for k in ("event_id","title")})
        except (AttributeError, cal_editor.CalendarEditorError) as exc:
            return _fail(str(exc))
        _emit("KYREX_RESULT_JSON", {"status":"needs_details", "final_response":obj.get("question") or "What address or note should I add?",
                                    "calendar_edit_draft":draft, "errors":[]})
        return 0
    if isinstance(obj, dict) and obj.get("op") == "update":
        return run_update(obj)

    # 1. Normalise the target (fail closed, no provider call).
    try:
        event = cal_editor.intent_from_task(args.task)
    except cal_editor.CalendarEditorError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"could not read the delete request: {type(exc).__name__}: {exc}")

    preview = cal_editor.build_preview(event)

    # 2. Owner-scoped identity.
    owner = str(os.environ.get("KYREX_BOT_OWNER") or "").strip()
    if not owner:
        return _fail("calendar deletes require an owner-scoped Bot identity")

    # 3. Announce the operation; the host classifies it as cal.delete (tier 2).
    _emit("KYREX_PROGRESS", {"cal_delete": preview["event_id"]})
    _emit("KYREX_OPERATION", {
        "op": "cal.delete",
        "target": preview["event_id"],
        "summary": cal_editor.summary_line(event),
    })
    verdict = _read_decision()
    if verdict not in ("ALLOW", "APPROVE"):
        return _fail("calendar delete denied by host policy")

    # 4. Mandatory T2 approval gate -- the user-visible preview, then explicit
    #    owner approval, BEFORE any external call.
    _emit("KYREX_APPROVAL", {
        "tier": cal_editor.APPROVAL_TIER,
        "summary": cal_editor.summary_line(event),
        "detail": cal_editor.preview_display(preview),
    })
    decision = _read_decision()
    if decision != "APPROVED":
        return _fail("calendar delete not approved \u2014 nothing was deleted")

    # 5. The SOLE Google delete call, via the owner's editor connector.
    try:
        import connectors
        store = connectors.default_store()
        store.calendar_editor(owner).delete_event(preview["event_id"])
    except Exception as exc:  # noqa: BLE001
        return _fail(f"calendar delete failed: {type(exc).__name__}: {exc}")

    # 6. One safe receipt.
    receipt = cal_editor.format_receipt(event)
    _emit("KYREX_RESULT_JSON", {"status": "ok", "final_response": receipt,
                                "errors": []})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
