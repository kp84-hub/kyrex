"""Return fast routed Gmail results to the coordinator in the SAME turn.

Generic Bot delegation is intentionally asynchronous: long-running repo/browser
work should not hold a Chat turn open. Routed Gmail reads are different. They
are bounded, read-only owner-connected operations and Kyrex often needs the
result immediately to finish the user's request (for example: search -> choose
result #5 -> read that message).

Without this bridge, ``delegate_task`` receives only ``status=queued``. The
coordinator therefore stops and asks the user to come back later even when the
Gmail read finishes a moment later. This bridge wraps ONLY the routed Gmail
submitter. After submission it waits for a short bounded window, mirrors the
task's terminal state onto the delegation, persists the same bounded Gmail
conversation state used by direct Chat, and returns the safe terminal public
view to the engine's existing confirmation result.

The model still never receives connector credentials, provider payloads, Gmail
message ids other than those already allowed by the existing canonical result
surface, or approval secrets. A timeout preserves the historical queued result.
All non-Gmail delegation remains asynchronous.
"""
from __future__ import annotations

import json
import os
import re
import time

_installed = False

INLINE_WAIT_SECONDS = max(
    0.0,
    min(60.0, float(os.environ.get("KYREX_CHAT_GMAIL_INLINE_WAIT_SECONDS", "15"))),
)
INLINE_POLL_SECONDS = max(
    0.02,
    min(0.5, float(os.environ.get("KYREX_CHAT_GMAIL_INLINE_POLL_SECONDS", "0.10"))),
)
MAX_CHECKS_PER_TURN = 12

_TERMINAL = frozenset({"done", "failed", "cancelled"})


def _result_dict(task: dict | None) -> dict:
    result = (task or {}).get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return {}
    return result if isinstance(result, dict) else {}


def _identity(session):
    ctx = getattr(session, "delegation_ctx", None) or {}
    return (
        str(ctx.get("owner") or "").strip(),
        str(ctx.get("conversation_id") or "").strip(),
    )


def _terminal_public_view(chat_service, session, submitted: dict):
    """Wait briefly for one already-submitted routed Gmail task.

    Returns ``None`` when the bounded wait expires so the caller can preserve
    the original queued result exactly. Otherwise returns a reconciled safe
    delegation public view. ``awaiting_approval`` is also returned immediately
    if ever observed (Gmail reads should never need approval), with no token.
    """
    task_id = str((submitted or {}).get("task_id") or "").strip()
    delegation_id = str((submitted or {}).get("delegation_id") or "").strip()
    if not task_id or not delegation_id:
        return None

    try:
        store = chat_service._task_store()
    except Exception:
        return None

    deadline = time.monotonic() + INLINE_WAIT_SECONDS
    task = None
    while True:
        try:
            task = store.get(task_id)
        except Exception:
            task = None
        if task is None:
            return None
        status = str(task.get("status") or "")
        if status in _TERMINAL or status == "awaiting_approval":
            break
        if time.monotonic() >= deadline:
            return None
        time.sleep(INLINE_POLL_SECONDS)

    owner, conversation_id = _identity(session)

    # Persist the same bounded Gmail page/selection state as the direct Chat
    # path BEFORE returning to the coordinator. That makes the very next
    # delegate_task("read number 5") resolve against this exact result page.
    if status == "done" and owner and conversation_id:
        remember = getattr(chat_service, "_remember_gmail_page", None)
        result = _result_dict(task)
        if callable(remember) and result:
            try:
                remember(owner, conversation_id, result)
            except Exception:
                pass

    try:
        rec = store.get_delegation(delegation_id) or {}
    except Exception:
        rec = {}
    reconcile = getattr(chat_service, "_reconcile_delegation", None)
    if rec and callable(reconcile):
        try:
            rec = reconcile(store, rec) or rec
        except Exception:
            pass

    # If this terminal result is being delivered directly back to the
    # coordinator model, suppress the later poll-driven assistant notice so the
    # user gets one coherent Kyrex answer instead of a second [Delegated ...]
    # message. The Delegated Work card remains durable and visible.
    if status in _TERMINAL:
        try:
            store.mark_delegation_relayed(delegation_id)
            if rec:
                rec = store.get_delegation(delegation_id) or rec
        except Exception:
            pass

    try:
        view = dict(chat_service.delegation.public_view(rec or submitted))
        # The UI summary is capped at 4,000 characters and can omit facts late
        # in a newsletter. The coordinator needs the connector's bounded body
        # as untrusted evidence, not an arbitrary/raw task result projection.
        selected = _result_dict(task).get("selected")
        if status == "done" and isinstance(selected, dict):
            headers = selected.get("headers") or {}
            view["email_evidence"] = {
                "headers": {k: str(headers.get(k) or "")[:500]
                            for k in ("Subject", "From", "Date")},
                "body": str(selected.get("body") or "")[:20000],
                "body_read_status": str(selected.get("body_read_status") or "")[:30],
                "body_available": bool(str(selected.get("body") or "").strip()),
                "body_truncated": bool(selected.get("truncated")) or len(str(selected.get("body") or "")) > 20000,
                "untrusted_data": True,
            }
        return view
    except Exception:
        return dict(submitted)


def install(chat_service, jev_stream_router) -> None:
    """Wrap ONLY the final routed Gmail submitter with a short inline wait."""
    global _installed
    if (_installed or getattr(
            jev_stream_router, "_gmail_inline_result_bridge_installed", False)):
        _installed = True
        return

    original_submit = getattr(jev_stream_router, "_submit_routed_gmail", None)
    if not callable(original_submit):
        return

    def submit_routed_gmail(chat, dev_bot, session, frame, hint):
        cache = getattr(session, "_gmail_inline_cache", None)
        task_text = str(frame.get("task") or "").strip()
        page = ()
        if not task_text.lower().startswith("gmail: search"):
            # "read number 1" on a new search page names a DIFFERENT message.
            try:
                owner, cid = _identity(session)
                conv = chat.get_conversation(owner, cid) or {}
                page = tuple(conv.get("gmail_results") or [])
            except Exception:
                pass
        # Compile the same state-derived command as the routed submitter.
        # Cache reads by message identity, not by its number/current page: the
        # same announcement may appear in many different search result sets.
        canonical = ""
        resolver = getattr(jev_stream_router, "_gmail_command_for_routed_turn", None)
        if callable(resolver):
            import gmail_continuation_bridge
            resolved = gmail_continuation_bridge.resolve_continuation(chat, session, task_text)
            if resolved is not None and resolved[0] == "command":
                canonical = resolved[1]
            elif resolved is None:
                routed_hint = dict(hint or {})
                owner, cid = _identity(session)
                routed_hint.setdefault("owner", owner)
                routed_hint.setdefault("conversation_id", cid)
                canonical = resolver(chat, task_text, routed_hint.get("request_text") or "", hint=routed_hint)
        read = re.match(r"^gmail: read id (\S+)", canonical or "")
        key = (("read", read.group(1)) if read else
               (str(frame.get("target_bot_id") or ""), canonical or task_text, page))
        if isinstance(cache, dict) and key in cache:
            ok, saved = cache[key]
            if ok and isinstance(saved, dict):
                # Refresh the durable result, never submit another Gmail job.
                # Search results restore their own page; single-message reads
                # restore selection while preserving the CURRENT search page.
                saved = _terminal_public_view(chat_service, session, saved) or saved
                return ok, dict(saved, already_checked=True,
                                follow_up="This email/query was already checked. Use its evidence; do not submit it again.")
            return ok, saved
        if canonical and isinstance(cache, dict) and len(cache) >= MAX_CHECKS_PER_TURN:
            return False, {"error": "Email lookup reached its per-turn limit.",
                           "lookup_limit_reached": True,
                           "follow_up": "Stop searching now. Give one brief answer from verified evidence with its source, and state any missing detail. Do not guess or resubmit."}
        outcome = original_submit(chat, dev_bot, session, frame, hint)
        if outcome is None:
            return None
        try:
            ok, payload = outcome
        except Exception:
            return outcome
        if not ok or not isinstance(payload, dict):
            if canonical and isinstance(cache, dict):
                cache[key] = outcome
            return outcome

        terminal = _terminal_public_view(chat_service, session, payload)
        if terminal is None:
            if isinstance(cache, dict):
                cache[key] = outcome
            return outcome
        if isinstance(cache, dict):
            cache[key] = (True, terminal)
        return True, terminal

    jev_stream_router._submit_routed_gmail = submit_routed_gmail
    jev_stream_router._gmail_inline_result_bridge_installed = True
    _installed = True
