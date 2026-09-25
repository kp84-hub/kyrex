"""Owner-scoped connected tools shared across every running Bot.

Bot identity/persona is a ROUTING specialization, not the permission boundary
for services the owner connected to Kyrex.  Gmail already follows this model.
This module applies the same boundary to Google Calendar without weakening the
operation-level safety model:

* calendar reads require the owner's live Calendar read scope;
* event creates/deletes require the owner's live Calendar write scope;
* creates still use the existing exact-payload T1 approval;
* deletes still use the existing exact-target T2 approval;
* connector executors re-check OAuth scope at the provider boundary;
* repo/browser work keeps its existing Rift/provider/allowlist/host gates.

The installer is deliberately a compatibility shim.  Existing preset policies
remain stored/recognised for old Bots and migration, but are no longer the
AUTHORITY for an owner-connected Calendar operation.  The task-local execution
context receives only the single Calendar operation needed by that connected
executor; the Bot's persisted policy is never mutated.
"""
from __future__ import annotations

import contextvars
import json
import re
from typing import Any

_installed = False
_request_text: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kyrex_shared_connected_request", default="")

_CREATE_VERB_RE = re.compile(
    r"^\s*(?:create|add|schedule|book|reserve|make|set\s+up|put)\b",
    re.IGNORECASE,
)
_CREATE_STRONG_RE = re.compile(
    r"^\s*(?:schedule|book|reserve)\b", re.IGNORECASE)
_CALENDAR_CUE_RE = re.compile(
    r"\b(?:calendar|event|meeting|appointment|reminder)\b", re.IGNORECASE)
_DELETE_RE = re.compile(
    r"^\s*(?:remove|delete|cancel|drop|clear|erase)\b", re.IGNORECASE)


def _owner(bot: dict) -> str:
    return str((bot or {}).get("owner") or "").strip()


def _running(dev_bot, bot: dict) -> bool:
    try:
        return bool(dev_bot._bots.is_running(bot or {}))
    except Exception:
        return False


def _calendar_read_available(owner: str) -> bool:
    owner = str(owner or "").strip()
    if not owner:
        return False
    try:
        import connectors
        store = connectors.default_store()
        helper = getattr(store, "calendar_read_available", None)
        if callable(helper):
            return bool(helper(owner))
        return bool(store.scope_granted(
            owner, connectors.GOOGLE_CALENDAR_READ_SCOPE, "google"))
    except Exception:
        return False


def _calendar_write_available(owner: str) -> bool:
    owner = str(owner or "").strip()
    if not owner:
        return False
    try:
        import connectors
        return bool(connectors.default_store().calendar_write_available(owner))
    except Exception:
        return False


def _shared_read_ready(dev_bot, bot: dict) -> bool:
    return _running(dev_bot, bot) and _calendar_read_available(_owner(bot))


def _shared_write_ready(dev_bot, bot: dict) -> bool:
    return _running(dev_bot, bot) and _calendar_write_available(_owner(bot))


def _calendar_create_request(text: str) -> bool:
    """Conservative generic-Bot Calendar-create classifier.

    Dedicated legacy Calendar/Writer roles retain their old route.  For every
    other Bot, a create/add/make/put request needs a Calendar semantic cue;
    schedule/book/reserve is itself a strong scheduling cue.  This prevents a
    Developer Bot's ordinary "create a file" request from being stolen by the
    Calendar connector merely because the owner connected Google.
    """
    text = str(text or "").strip()
    if not text or not _CREATE_VERB_RE.match(text):
        return False
    return bool(_CREATE_STRONG_RE.match(text) or _CALENDAR_CUE_RE.search(text))


def _calendar_delete_request(text: str) -> bool:
    """Conservative generic-Bot Calendar-delete classifier.

    Requiring an event/calendar cue keeps "delete this file" on the repository
    path while allowing natural requests such as "remove this from my calendar"
    and "cancel the dentist appointment".
    """
    text = str(text or "").strip()
    return bool(text and _DELETE_RE.match(text) and _CALENDAR_CUE_RE.search(text))


def _submit_connected(dev_bot, user, bot, task_text, executor_prefix, *,
                      store=None, conversation_id=None):
    """Submit one owner-scoped connected task with no Rift/provider authority."""
    bot = bot or {}
    bot_id = str(bot.get("id") or "").strip()
    owner = _owner(bot)
    text = str(task_text or "").strip()
    if not bot_id or not owner:
        raise dev_bot.DevBotError("bot id and owner are required")
    if owner != str(user or "").strip():
        raise dev_bot.DevBotError(
            f"bot {bot_id!r} belongs to another owner -- fail closed")
    if not _running(dev_bot, bot):
        raise dev_bot.DevBotError(
            f"bot {bot_id!r} is {bot.get('status') or dev_bot._bots.STATUS_STOPPED} -- "
            "start it before submitting tasks")
    if not text:
        raise dev_bot.DevBotError("a connected-tool request is required")

    if executor_prefix == "calendar":
        if text not in dev_bot.CALENDAR_COMMANDS:
            raise dev_bot.DevBotError(
                f"unsupported calendar request {text!r}; the only accepted requests "
                "are calendar: today, calendar: tomorrow, calendar: week")
        if not _calendar_read_available(owner):
            raise dev_bot.DevBotError(
                "Google Calendar read access is not connected for this owner")
    elif executor_prefix in ("cal_write", "cal_edit"):
        if not _calendar_write_available(owner):
            raise dev_bot.DevBotError(
                "Google Calendar write access is not connected for this owner")
    else:
        raise dev_bot.DevBotError(
            f"unsupported shared connected executor {executor_prefix!r}")

    if store is None:
        from task_store import CloudTaskStore
        store = CloudTaskStore()
    return store.submit(
        session_key=bot_id,
        task_text=text,
        repo_url=None,
        executor_prefix=executor_prefix,
        bot_id=bot_id,
        rift=str(bot.get("rift") or "").strip(),
        chat_id=str(user or ""),
        resolve_bot=True,
        conversation_id=(str(conversation_id).strip() or None
                         if conversation_id else None),
    )


def _delegated_calendar_payload(owner: str, target: dict, text: str):
    """Return ``(prefix, task_text)`` for a shared Calendar delegation or None."""
    import serve

    stripped = str(text or "").strip()
    canonical = (stripped if stripped in serve.CALENDAR_TASK_TEXTS
                 else serve.natural_calendar_command(stripped))
    if canonical and _calendar_read_available(owner):
        return "calendar", canonical

    if _calendar_create_request(stripped) and _calendar_write_available(owner):
        # The existing writer remains authoritative for grammar/intent and
        # approval.  Reject a non-calendar-looking false positive here rather
        # than silently falling back to repo execution.
        try:
            import cal_writer
            intent = cal_writer.intent_from_task(stripped)
        except Exception:
            return None
        return "cal_write", json.dumps(intent, sort_keys=True)

    if _calendar_delete_request(stripped) and _calendar_write_available(owner):
        try:
            import cal_editor
            import cal_delete_preflight
            intent = cal_editor.normalize_delete_request(stripped)
            if intent.get("event_id"):
                payload = {"id": intent["event_id"]}
            else:
                event = cal_delete_preflight.resolve_owner_event(owner, intent, target)
                payload = {"event": event}
            return "cal_edit", json.dumps(payload, sort_keys=True)
        except Exception:
            return None
    return None


def _submit_delegation_connected(original, delegation, dev_bot, owner,
                                  coordinator_bot, target_bot_id, text, *,
                                  store=None, parent_conversation_id=None,
                                  parent_task_id=None,
                                  parent_delegation_id=None,
                                  executor_prefix="repo", depth=1):
    """Connected-tool-first wrapper for the existing durable delegation path."""
    # Existing explicit non-repo prefixes keep their existing implementation;
    # this wrapper only intercepts an ordinary Kyrex delegation whose text is
    # deterministically a shared Calendar operation.
    if str(executor_prefix or "repo").strip().lower() != "repo":
        return original(
            owner, coordinator_bot, target_bot_id, text, store=store,
            parent_conversation_id=parent_conversation_id,
            parent_task_id=parent_task_id,
            parent_delegation_id=parent_delegation_id,
            executor_prefix=executor_prefix, depth=depth)

    owner = str(owner or "").strip()
    try:
        target = delegation.resolve_connected_tool_target(owner, target_bot_id)
        routed = _delegated_calendar_payload(owner, target, text)
    except Exception:
        routed = None
    if not routed:
        return original(
            owner, coordinator_bot, target_bot_id, text, store=store,
            parent_conversation_id=parent_conversation_id,
            parent_task_id=parent_task_id,
            parent_delegation_id=parent_delegation_id,
            executor_prefix=executor_prefix, depth=depth)

    prefix, task_text = routed
    coordinator_bot = coordinator_bot or {}
    coordinator_id = str(coordinator_bot.get("id") or "").strip()
    target_id = str(target.get("id") or target_bot_id).strip()
    if not owner or not coordinator_id:
        raise delegation.DelegationError("delegation requires an owner and coordinator Bot")
    if str(coordinator_bot.get("owner") or "").strip() != owner:
        raise delegation.DelegationError("the coordinator Bot is not owned by you")
    if not dev_bot._serve.coordinator_granted(coordinator_bot):
        raise delegation.DelegationError(
            f"Bot {coordinator_id!r} is not coordinator-capable")
    if parent_delegation_id:
        raise delegation.DelegationError(
            "recursive delegation is not permitted (one level only)")
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        raise delegation.DelegationError("delegation depth must be an integer")
    if depth != delegation.MAX_DEPTH:
        raise delegation.DelegationError(
            f"delegation depth must be {delegation.MAX_DEPTH} (one level only)")
    if target_id == coordinator_id:
        raise delegation.DelegationError("a coordinator cannot delegate to itself")

    if store is None:
        from task_store import CloudTaskStore
        store = CloudTaskStore()
    delegation_id = store.create_delegation(
        owner=owner,
        coordinator_bot_id=coordinator_id,
        target_bot_id=target_id,
        task_text=task_text,
        parent_conversation_id=parent_conversation_id,
        parent_task_id=parent_task_id,
        parent_delegation_id=None,
        depth=depth,
        executor_prefix=prefix,
        status=delegation.STATUS_QUEUED,
    )
    try:
        task_id = store.submit(
            session_key=target_id,
            task_text=task_text,
            repo_url=None,
            executor_prefix=prefix,
            bot_id=target_id,
            rift=str(target.get("rift") or ""),
            chat_id=owner,
            resolve_bot=True,
            conversation_id=parent_conversation_id,
            parent_delegation_id=delegation_id,
        )
    except Exception as exc:
        store.set_delegation_status(
            delegation_id, delegation.STATUS_DELEGATION_REJECTED,
            error=f"could not create target task: {exc}")
        raise delegation.DelegationError(f"could not create target task: {exc}")
    store.set_delegation_status(
        delegation_id, delegation.STATUS_QUEUED, task_id=task_id)
    return delegation.public_view(store.get_delegation(delegation_id) or {})


def install(chat_service, dev_bot) -> None:
    """Install shared Calendar authority once, before the Jev stream wrapper."""
    global _installed
    if _installed:
        return

    import delegation
    import serve

    original_stream = chat_service.stream_chat
    original_build_context = serve.build_context
    original_writer_ready = dev_bot.calendar_writer_route_ready
    original_submit_delegation = delegation.submit_delegation

    async def connected_stream_chat(*args, **kwargs):
        # user_content is the third positional argument on the stable Chat API;
        # accept the named form too so tests/helpers keep working.
        text = kwargs.get("user_content")
        if text is None and len(args) >= 3:
            text = args[2]
        token = _request_text.set(str(text or ""))
        try:
            async for frame in original_stream(*args, **kwargs):
                yield frame
        finally:
            # Async generators may be finalized from a different Context (the
            # exact SSE teardown case fixed in Jev #221). Never turn cleanup
            # into a user-visible failure.
            try:
                _request_text.reset(token)
            except ValueError:
                _request_text.set("")

    def connected_build_context(session_key, executor_prefix="repo",
                                allow_bot_resolution=True):
        ctx = original_build_context(
            session_key, executor_prefix,
            allow_bot_resolution=allow_bot_resolution)
        owner = str(getattr(ctx, "bot_owner", "") or "").strip()
        policy = dict(getattr(ctx, "policy", {}) or {})
        if executor_prefix == "calendar" and _calendar_read_available(owner):
            policy["cal:list"] = 0
        elif executor_prefix == "cal_write" and _calendar_write_available(owner):
            policy["cal:create"] = 0
        elif executor_prefix == "cal_edit" and _calendar_write_available(owner):
            policy["cal:delete"] = 2
        ctx.policy = policy
        return ctx

    def calendar_route_ready(bot):
        return _shared_read_ready(dev_bot, bot)

    def calendar_writer_route_ready(bot):
        # Preserve legacy dedicated Calendar/Writer behavior.  Other Bots gain
        # the owner-connected writer only for an actual scheduling-shaped turn,
        # so Developer work such as "create a parser file" stays on repo.
        try:
            if original_writer_ready(bot):
                return True
        except Exception:
            pass
        return (_shared_write_ready(dev_bot, bot)
                and _calendar_create_request(_request_text.get()))

    def calendar_editor_route_ready(bot):
        return _shared_write_ready(dev_bot, bot)

    def calendar_editor_route_for(bot, text):
        # Existing Editor-role behavior stays compatible; the newly shared path
        # requires an explicit Calendar/event cue to avoid stealing "delete file".
        try:
            old_editor = dev_bot._serve.calendar_editor_granted(
                (bot or {}).get("policy"))
        except Exception:
            old_editor = False
        if old_editor:
            return calendar_editor_route_ready(bot) and dev_bot.calendar_editor_request(text)
        return calendar_editor_route_ready(bot) and _calendar_delete_request(text)

    def submit_calendar_task(user, bot, task_text, store=None,
                             conversation_id=None):
        return _submit_connected(
            dev_bot, user, bot, task_text, "calendar", store=store,
            conversation_id=conversation_id)

    def submit_calendar_writer_task(user, bot, task_text, store=None,
                                    conversation_id=None):
        return _submit_connected(
            dev_bot, user, bot, task_text, "cal_write", store=store,
            conversation_id=conversation_id)

    def submit_calendar_editor_task(user, bot, task_text, store=None,
                                    conversation_id=None):
        return _submit_connected(
            dev_bot, user, bot, task_text, "cal_edit", store=store,
            conversation_id=conversation_id)

    def connected_submit_delegation(owner, coordinator_bot, target_bot_id, text,
                                    *, store=None, parent_conversation_id=None,
                                    parent_task_id=None,
                                    parent_delegation_id=None,
                                    executor_prefix="repo", depth=1):
        return _submit_delegation_connected(
            original_submit_delegation, delegation, dev_bot,
            owner, coordinator_bot, target_bot_id, text, store=store,
            parent_conversation_id=parent_conversation_id,
            parent_task_id=parent_task_id,
            parent_delegation_id=parent_delegation_id,
            executor_prefix=executor_prefix, depth=depth)

    # Expose the same owner-scope predicates Jev/diagnostics may consult.
    dev_bot._calendar_read_available = _calendar_read_available
    dev_bot._calendar_write_available = _calendar_write_available
    dev_bot.calendar_route_ready = calendar_route_ready
    dev_bot.calendar_writer_route_ready = calendar_writer_route_ready
    dev_bot.calendar_editor_route_ready = calendar_editor_route_ready
    dev_bot.calendar_editor_route_for = calendar_editor_route_for
    dev_bot.submit_calendar_task = submit_calendar_task
    dev_bot.submit_calendar_writer_task = submit_calendar_writer_task
    dev_bot.submit_calendar_editor_task = submit_calendar_editor_task
    delegation.submit_delegation = connected_submit_delegation
    serve.build_context = connected_build_context
    chat_service.stream_chat = connected_stream_chat
    _installed = True
