"""Install Jev as a bounded decision layer around Kyrex Chat routing.

This first active slice intentionally has one-way authority: Jev may reduce an
EXACT Developer/Browser primary role to the ordinary read-only engine, but it
cannot invent a capability or promote an otherwise-ineligible route. Custom or
mixed-capability Bots remain entirely on Kyrex's deterministic router.

The installer is idempotent and the routing hint is a ContextVar, so concurrent
Chat turns cannot leak decisions into one another. The hint is cleared as soon
as the wrapped stream produces its first frame; by then stream_chat has already
completed route selection.
"""

from __future__ import annotations

import contextvars
import functools

import jev_routing


_route_hint: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kyrex_jev_route_hint", default=None)
_installed = False


def _exact_developer(chat_service, policy) -> bool:
    """True only for the server-defined Developer preset shape."""
    try:
        return dict(policy or {}) == dict(chat_service.serve.DEVELOPER_PRESET)
    except Exception:
        return False


def _exact_browser(chat_service, policy) -> bool:
    """True only for the server-defined Browser preset shape."""
    try:
        return bool(chat_service.serve.is_browser_bot_policy(policy))
    except Exception:
        return False


def install(chat_service, dev_bot) -> None:
    """Install the active Jev routing shim exactly once."""
    global _installed
    if _installed or getattr(chat_service, "_jev_routing_installed", False):
        _installed = True
        return

    original_stream_chat = chat_service.stream_chat
    original_writable = dev_bot.is_writable_bot_policy
    original_browser_ready = dev_bot.browser_route_ready

    @functools.wraps(original_writable)
    def writable_policy(policy):
        # Active Jev may only DE-escalate an exact Developer turn. Returning
        # False cannot grant a capability; mixed/custom policies never receive
        # a hint and therefore remain byte-identical to deterministic routing.
        if _route_hint.get() == "engine" and _exact_developer(
                chat_service, policy):
            return False
        return original_writable(policy)

    @functools.wraps(original_browser_ready)
    def browser_ready(bot):
        if (_route_hint.get() == "engine"
                and _exact_browser(chat_service, (bot or {}).get("policy"))):
            return False
        return original_browser_ready(bot)

    dev_bot.is_writable_bot_policy = writable_policy
    dev_bot.browser_route_ready = browser_ready

    @functools.wraps(original_stream_chat)
    async def routed_stream_chat(
        user,
        conversation_id,
        user_content,
        cancel_event=None,
        workspace_id=None,
        request_id=None,
    ):
        hint = None
        bot = None
        try:
            conv = chat_service.get_conversation(user, conversation_id)
            bot_id = (conv or {}).get("bot_id") or None
            if bot_id:
                bot = chat_service.resolve_bot_for_user(user, bot_id)
        except Exception:
            # The authoritative stream will surface the real registry/binding
            # error. Jev never masks or rewrites that failure path.
            bot = None

        if bot is not None:
            available = {"engine"}
            fallback = "engine"
            policy = bot.get("policy")
            try:
                if (_exact_developer(chat_service, policy)
                        and original_writable(policy)):
                    available.add("repo")
                    fallback = "repo"
                elif (_exact_browser(chat_service, policy)
                      and original_browser_ready(bot)):
                    available.add("browser")
                    fallback = "browser"
            except Exception:
                # Existing stream_chat predicates fail closed too. Do not let
                # Jev turn a readiness fault into a new route.
                available = {"engine"}
                fallback = "engine"

            decision = jev_routing.decide_route(
                user_content, available, fallback)
            selected = decision.get("selected_route")
            if selected == "engine" and fallback in {"repo", "browser"}:
                hint = "engine"

        token = _route_hint.set(hint)
        reset = False
        try:
            kwargs = {
                "cancel_event": cancel_event,
                "request_id": request_id,
            }
            # Preserve stream_chat's sentinel semantics. The wrapper is used by
            # chat_api, which passes the sentinel explicitly; tests/future
            # callers may omit it, so only forward workspace_id when supplied.
            if workspace_id is not None:
                kwargs["workspace_id"] = workspace_id
            agen = original_stream_chat(
                user, conversation_id, user_content, **kwargs)
            async for frame in agen:
                if not reset:
                    _route_hint.reset(token)
                    reset = True
                yield frame
        finally:
            if not reset:
                _route_hint.reset(token)

    chat_service.stream_chat = routed_stream_chat
    chat_service._jev_routing_installed = True
    _installed = True
