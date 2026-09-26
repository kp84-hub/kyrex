"""Preserve bounded Gmail continuations across delegated Chat work.

The routed Gmail boundary deliberately treats the untouched user request as the
source of truth for the INITIAL search. That prevents a model-authored delegate
instruction from becoming a provider query. After a search returns a numbered
page, however, Kyrex may legitimately choose a bounded continuation such as
``read number 5`` or ``show 5 more`` while still handling the SAME user turn.

Those continuations are not new queries. They resolve only against the parent
conversation's already-redacted Gmail page, using the same deterministic
helpers as direct Chat. This bridge also follows ONLY routed, read-only Gmail
connected-tool tasks for a short bounded window so their safe result can return
to Kyrex inside the same engine turn. Generic Bot delegations remain asynchronous
and approval-owning targets remain untouched.

The model never supplies an id, page token, or replacement search query.
"""
from __future__ import annotations

import functools
import json
import os
import re
import time

_installed = False

# Gmail is an owner-scoped, read-only connected tool and normally completes
# quickly. Following it briefly lets the coordinator reason over the result in
# the SAME user turn without changing the deliberately asynchronous lifecycle
# of repo/browser/write/approval-bearing delegations.
_GMAIL_FOLLOW_MAX_SECONDS = float(
    os.environ.get("KYREX_CHAT_GMAIL_FOLLOW_MAX_SECONDS", "12")
)
_GMAIL_FOLLOW_POLL_SECONDS = 0.05
_TERMINAL = frozenset({"done", "failed", "cancelled"})


def _turn_identity(session):
    ctx = getattr(session, "delegation_ctx", None) or {}
    owner = str(ctx.get("owner") or "").strip()
    conversation_id = str(ctx.get("conversation_id") or "").strip()
    return owner, conversation_id


def _continuation_intent(serve, task_text: str):
    """Return ``("select", n)`` / ``("more", None)`` for bounded continuations.

    A leading ``gmail:`` is tolerated because model-authored delegation text may
    preserve the tool namespace. The remainder still has to satisfy serve.py's
    existing strict continuation grammar; arbitrary prose is never accepted.
    """
    task = str(task_text or "").strip()
    if not task:
        return None
    task = re.sub(r"^gmail:\s*", "", task, count=1, flags=re.IGNORECASE)

    try:
        index = serve.natural_gmail_select(task)
    except Exception:
        index = None
    if index is not None:
        return "select", index

    try:
        if serve.natural_gmail_more(task):
            return "more", None
    except Exception:
        pass
    return None


def resolve_continuation(chat_service, session, task_text: str):
    """Compile a routed continuation against durable parent-conversation state.

    Returns:
      * ``None`` when *task_text* is not a bounded continuation;
      * ``("command", canonical)`` when it resolves safely; or
      * ``("error", message)`` when it IS a continuation but no safe stored
        page exists. A recognized continuation never falls back to re-running
        the original Gmail search.
    """
    serve = getattr(chat_service, "serve", None)
    if serve is None:
        return None
    intent = _continuation_intent(serve, task_text)
    if intent is None:
        return None

    owner, conversation_id = _turn_identity(session)
    if not owner or not conversation_id:
        return "error", "Gmail continuation is missing its conversation context."

    # The previous Gmail task may have completed during THIS same Kyrex turn.
    # Reconcile it now so its bounded hit ids/page token become parent state;
    # this removes any dependency on the frontend Delegated Work poll.
    sync = getattr(chat_service, "sync_delegated_work", None)
    if callable(sync):
        try:
            sync(owner, conversation_id)
        except Exception:
            # Fail closed below if the page is still unavailable.
            pass

    try:
        conv = chat_service.get_conversation(owner, conversation_id) or {}
    except Exception:
        conv = {}

    kind, value = intent
    try:
        if kind == "select":
            compile_select = getattr(chat_service, "_gmail_select_command", None)
            command = compile_select(conv, value) if callable(compile_select) else None
        else:
            compile_more = getattr(chat_service, "_gmail_continuation_command", None)
            command = compile_more(conv) if callable(compile_more) else None
    except Exception:
        command = None

    if not command:
        return "error", (
            "I don't have a Gmail result page to continue. Run the mail search "
            "first, then choose a numbered result or ask for more.")
    return "command", str(command)


def _task_result(task: dict | None) -> dict:
    """Return one durable task result as a dict, never raising."""
    raw = (task or {}).get("result")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
    return {}


def _terminal_public_view(chat_service, store, submitted: dict, task: dict) -> dict:
    """Return the reconciled safe delegation view for one terminal Gmail task."""
    delegation_id = str((submitted or {}).get("delegation_id") or "").strip()
    rec = None
    if delegation_id:
        try:
            rec = store.get_delegation(delegation_id)
        except Exception:
            rec = None
        reconcile = getattr(chat_service, "_reconcile_delegation", None)
        if rec is not None and callable(reconcile):
            try:
                rec = reconcile(store, rec) or rec
            except Exception:
                pass
        delegation_module = getattr(chat_service, "delegation", None)
        public_view = getattr(delegation_module, "public_view", None)
        if rec is not None and callable(public_view):
            try:
                return dict(public_view(rec))
            except Exception:
                pass

    # Fail-soft fallback for a test double or a transient delegation-row read:
    # never expose the raw provider result. Only lifecycle + the existing safe
    # submitted view are returned.
    out = dict(submitted or {})
    out["status"] = str((task or {}).get("status") or out.get("status") or "")
    return out


def follow_routed_gmail(chat_service, session, routed):
    """Briefly follow ONE routed Gmail task and return its safe terminal view.

    This is intentionally narrower than generic delegation following:

    * only a successful routed Gmail submission with a durable ``task_id`` is
      eligible;
    * ``awaiting_approval`` returns immediately (Gmail reads should never need
      one, and this function must never absorb an approval-bearing operation);
    * terminal Gmail result state is copied into the parent conversation via
      the EXISTING ``_remember_gmail_page`` helper, but the raw result is never
      returned to the model;
    * timeout returns the original queued/running public view, preserving the
      existing asynchronous behavior rather than pretending completion.
    """
    if not (isinstance(routed, tuple) and len(routed) == 2):
        return routed
    ok, submitted = routed
    if not ok or not isinstance(submitted, dict):
        return routed
    task_id = str(submitted.get("task_id") or "").strip()
    if not task_id:
        return routed

    owner, conversation_id = _turn_identity(session)
    if not owner or not conversation_id:
        return routed
    get_store = getattr(chat_service, "_task_store", None)
    if not callable(get_store):
        return routed
    try:
        store = get_store()
    except Exception:
        return routed

    deadline = time.monotonic() + max(0.0, _GMAIL_FOLLOW_MAX_SECONDS)
    while True:
        try:
            task = store.get(task_id) or {}
        except Exception:
            return routed
        status = str(task.get("status") or "")

        # Never turn this Gmail convenience into an approval waiter. If the
        # executor ever changes unexpectedly, fall back to the normal owner
        # approval lifecycle immediately.
        if status == "awaiting_approval":
            return routed

        if status in _TERMINAL:
            if status == "done":
                result = _task_result(task)
                remember = getattr(chat_service, "_remember_gmail_page", None)
                if result and callable(remember):
                    try:
                        remember(owner, conversation_id, result)
                    except Exception:
                        pass
            return True, _terminal_public_view(
                chat_service, store, submitted, task)

        if time.monotonic() >= deadline:
            return routed
        time.sleep(_GMAIL_FOLLOW_POLL_SECONDS)


def install(chat_service, jev_stream_router) -> None:
    """Wrap routed Gmail with bounded continuation + same-turn read following."""
    global _installed
    if (_installed or getattr(
            jev_stream_router, "_gmail_continuation_bridge_installed", False)):
        _installed = True
        return

    original_submit = getattr(jev_stream_router, "_submit_routed_gmail", None)
    if not callable(original_submit):
        return

    @functools.wraps(original_submit)
    def submit_routed_gmail(chat, dev_bot, session, frame, hint):
        resolved = resolve_continuation(
            chat_service, session, (frame or {}).get("task"))
        if resolved is not None:
            status, payload = resolved
            if status == "error":
                return False, {"error": payload}

            # Feed the exact state-derived command to the EXISTING Gmail route.
            # Clear request_text only for this continuation so #225's original-
            # request authority remains intact for every initial search.
            routed_frame = dict(frame or {})
            routed_frame["task"] = payload
            routed_hint = dict(hint or {})
            routed_hint["request_text"] = ""
            submitted = original_submit(
                chat, dev_bot, session, routed_frame, routed_hint)
            return follow_routed_gmail(chat_service, session, submitted)

        submitted = original_submit(chat, dev_bot, session, frame, hint)
        return follow_routed_gmail(chat_service, session, submitted)

    jev_stream_router._submit_routed_gmail = submit_routed_gmail
    jev_stream_router._gmail_continuation_bridge_installed = True
    _installed = True
