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
        return dict(chat_service.delegation.public_view(rec or submitted))
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
        outcome = original_submit(chat, dev_bot, session, frame, hint)
        if outcome is None:
            return None
        try:
            ok, payload = outcome
        except Exception:
            return outcome
        if not ok or not isinstance(payload, dict):
            return outcome

        terminal = _terminal_public_view(chat_service, session, payload)
        if terminal is None:
            return outcome
        return True, terminal

    jev_stream_router._submit_routed_gmail = submit_routed_gmail
    jev_stream_router._gmail_inline_result_bridge_installed = True
    _installed = True
