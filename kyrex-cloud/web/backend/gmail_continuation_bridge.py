"""Preserve bounded Gmail continuations across delegated Chat work.

The routed Gmail boundary deliberately treats the untouched user request as the
source of truth for the INITIAL search. That prevents a model-authored delegate
instruction from becoming a provider query. After a search returns a numbered
page, however, Kyrex may legitimately choose a bounded continuation such as
``read number 5`` or ``show 5 more`` while still handling the SAME user turn.

Those continuations are not new queries. They resolve only against the parent
conversation's already-redacted Gmail page, using the same deterministic
helpers as direct Chat. This bridge reconciles the latest completed Gmail
work, compiles the continuation to an exact canonical command, then hands that
command back to the existing routed Gmail submitter. The model never supplies
an id, page token, or replacement search query.
"""
from __future__ import annotations

import functools
import re

_installed = False


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


def install(chat_service, jev_stream_router) -> None:
    """Wrap the existing routed Gmail submitter with bounded continuation state."""
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
            return original_submit(
                chat, dev_bot, session, routed_frame, routed_hint)

        return original_submit(chat, dev_bot, session, frame, hint)

    jev_stream_router._submit_routed_gmail = submit_routed_gmail
    jev_stream_router._gmail_continuation_bridge_installed = True
    _installed = True
