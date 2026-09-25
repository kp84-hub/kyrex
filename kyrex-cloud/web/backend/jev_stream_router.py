"""Install Jev as Kyrex Chat's bounded routing control plane.

Jev decides WHERE a turn should go; Kyrex still decides WHAT the task means,
which tools/operations are legal, how to execute them, how to recover, and what
to tell the user.

Two independent hints are installed:

* execution-route hint — preserves the original bounded Developer/Browser
  de-escalation (repo/browser -> engine only);
* Bot-target hint — on coordinator turns, Jev chooses one host-supplied Bot id
  from the owner's eligible safe roster. The decision is injected into the
  coordinator context as ROUTING ONLY, and the first delegate_task target is
  host-enforced to that id. Kyrex still writes the delegated task text.

Owner-scoped connected tools remain separate from Bot identity. A Jev-routed
Gmail delegation is therefore sent through the EXISTING shared Gmail bridge
instead of accidentally falling through to the repo/Rift executor. All Gmail
scope/body/result bounds are still enforced by the existing route + connector.

The execution hint is a ContextVar because it is consumed synchronously during
route selection. The Bot-target hint is also a ContextVar on the request thread
for coordinator-context rendering, but before Chat starts its manually-created
engine worker thread the hint is copied onto that EngineSession. The host-side
delegation handler reads the session copy, so the route cannot disappear at the
thread boundary and concurrent turns still remain isolated.
"""

from __future__ import annotations

import contextvars
import functools
import json
import sys

import jev_routing


_route_hint: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kyrex_jev_route_hint", default=None)
_bot_target_hint: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "kyrex_jev_bot_target_hint", default=None)
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


def _is_coordinator(chat_service, bot) -> bool:
    try:
        return bool(chat_service.serve.coordinator_granted(bot or {}))
    except Exception:
        return False


def _role_view(policy) -> dict:
    """Read the already-loaded safe user-facing role view, never a Bot prompt."""
    try:
        roles = sys.modules.get("bot_roles")
        if roles is not None and hasattr(roles, "role_view"):
            view = roles.role_view(policy)
            return dict(view or {})
    except Exception:
        pass
    return {}


def _candidate(chat_service, bot: dict, *, metadata: dict | None = None) -> dict:
    """Safe Jev candidate metadata; no Rift/policy rules/prompt/provider secret."""
    bot = bot or {}
    metadata = metadata or {}
    view = _role_view(bot.get("policy"))
    role = str(metadata.get("role") or view.get("id") or "custom")
    description = str(view.get("description") or "").strip()
    caps = metadata.get("capabilities") or []
    if caps:
        cap_text = ", ".join(str(c) for c in caps[:20])
        description = (
            (description + " ") if description else ""
        ) + f"Host-reported role capabilities: {cap_text}."
    description = (
        description + " Owner-scoped connected tools are independent of this "
        "role/persona and are enforced separately by Kyrex."
    ).strip()
    return {
        "id": str(bot.get("id") or metadata.get("id") or "").strip(),
        "name": str(bot.get("name") or metadata.get("name") or "").strip(),
        "role": role,
        "description": description,
    }


def _routing_candidates(chat_service, user: str, coordinator: dict) -> list[dict]:
    """Current coordinator + SAME-owner eligible peer Bots, safe metadata only."""
    current = _candidate(chat_service, coordinator)
    out = [current] if current["id"] else []
    owner = str((coordinator or {}).get("owner") or "").strip()
    coordinator_id = str((coordinator or {}).get("id") or "").strip()
    if not owner:
        return out
    try:
        peers = chat_service.delegation.visible_targets(
            owner, exclude_bot_id=coordinator_id)
    except Exception:
        return out

    for meta in peers:
        try:
            if str(meta.get("status") or "") != "running":
                continue
            if not bool(meta.get("available")):
                continue
            peer_id = str(meta.get("id") or "").strip()
            if not peer_id:
                continue
            # Use the same target resolver delegation itself uses: ownership,
            # lifecycle, Rift and provider configuration must all be valid
            # BEFORE Jev is allowed to see this Bot as a route.
            peer = chat_service.delegation.resolve_delegation_target(
                owner, peer_id)
            out.append(_candidate(chat_service, peer, metadata=meta))
        except Exception:
            continue
    return out


def _shared_tools(dev_bot, bot: dict) -> list[str]:
    """Owner-scoped connected tools already proven available by host predicates."""
    tools = []
    try:
        if dev_bot.gmail_route_ready(bot):
            tools.append("gmail_read")
    except Exception:
        pass
    try:
        if dev_bot.email_calendar_route_ready(bot):
            tools.append("calendar_write")
    except Exception:
        pass
    return tools


def _gmail_command_for_routed_turn(chat_service, task_text: str,
                                   original_request: str) -> str | None:
    """Reuse the EXISTING deterministic Gmail grammar for a routed mail turn."""
    serve = chat_service.serve
    task = str(task_text or "").strip()
    request = str(original_request or "").strip()

    canonical = serve.canonical_gmail_task(task)
    if canonical:
        return canonical
    natural = serve.natural_gmail_command(task)
    if natural:
        return natural

    # Jev has already made the ROUTING decision that this turn belongs on the
    # selected specialist path. We still do NOT let Jev write a query or tool
    # argument: Kyrex's existing deterministic parser derives it from the
    # untouched user request by adding only the explicit mail-object cue that
    # the parser requires. Mutating/ambiguous requests still fail closed.
    if request:
        return serve.natural_gmail_command(
            f"Read my email and {request}")
    return None


def _submit_routed_gmail(chat_service, dev_bot, session, frame: dict,
                         hint: dict):
    """Submit ONE Jev-routed Gmail delegation, or None when it is not Gmail.

    The durable delegation row remains the UI/audit surface, while the linked
    task is created through dev_bot.submit_gmail_task -- the SAME owner-scoped,
    read-only bridge direct Bot Chat uses. Nothing here widens Gmail scope,
    fetches attachments, or invents a query.
    """
    ctx = getattr(session, "delegation_ctx", None) or {}
    owner = str(ctx.get("owner") or "").strip()
    coordinator = ctx.get("bot") or {}
    coordinator_id = str(coordinator.get("id") or "").strip()
    target_id = str(hint.get("selected_bot_id") or "").strip()
    if not owner or not coordinator_id or not target_id:
        return None

    try:
        target = chat_service.delegation.resolve_delegation_target(
            owner, target_id)
        if not dev_bot.gmail_route_ready(target):
            return None
    except Exception:
        return None

    canonical = _gmail_command_for_routed_turn(
        chat_service, frame.get("task"),
        hint.get("request_text") or "")
    if not canonical:
        return None

    store = chat_service._task_store()
    delegation_id = store.create_delegation(
        owner=owner,
        coordinator_bot_id=coordinator_id,
        target_bot_id=target_id,
        task_text=canonical,
        parent_conversation_id=ctx.get("conversation_id"),
        parent_task_id=ctx.get("parent_task_id"),
        parent_delegation_id=None,
        depth=1,
        executor_prefix="gmail",
        status="queued",
    )
    try:
        task_id = dev_bot.submit_gmail_task(
            owner, target, canonical, store=store,
            conversation_id=ctx.get("conversation_id"))
    except Exception as exc:
        error = f"could not create target Gmail task: {exc}"
        try:
            store.set_delegation_status(
                delegation_id, "rejected", error=error)
        except Exception:
            pass
        return False, {"error": error}

    store.set_delegation_status(
        delegation_id, "queued", task_id=task_id)
    rec = store.get_delegation(delegation_id) or {}
    return True, dict(chat_service.delegation.public_view(rec))


def _persist_delegated_gmail_results(chat_service, user: str,
                                     conversation_id: str,
                                     sync_result: dict) -> None:
    """Carry a completed delegated Gmail selection back to parent Chat state.

    This makes a later owner-scoped "add that to my calendar" consume the SAME
    selected/enriched email facts even though Gmail ran under a routed peer Bot.
    Repeating the sync is idempotent: _remember_gmail_page overwrites the same
    bounded conversation keys with the same durable result.
    """
    remember = getattr(chat_service, "_remember_gmail_page", None)
    if not callable(remember):
        return
    store = chat_service._task_store()
    for view in (sync_result or {}).get("delegations") or []:
        if str(view.get("status") or "") != "done":
            continue
        delegation_id = str(view.get("delegation_id") or "").strip()
        if not delegation_id:
            continue
        try:
            rec = chat_service.delegation.fetch_delegation(
                user, delegation_id, store=store)
        except Exception:
            rec = None
        if not rec or str(rec.get("executor_prefix") or "") != "gmail":
            continue
        task_id = rec.get("task_id")
        task = store.get(task_id) if task_id else None
        result = (task or {}).get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, json.JSONDecodeError):
                result = None
        if isinstance(result, dict) and result:
            try:
                remember(user, conversation_id, result)
            except Exception:
                # Conversation-state enrichment is fail-soft; the durable Gmail
                # task/result remains authoritative and visible on the card.
                pass


def install(chat_service, dev_bot) -> None:
    """Install the active Jev routing shim exactly once."""
    global _installed
    if _installed or getattr(chat_service, "_jev_routing_installed", False):
        _installed = True
        return

    original_stream_chat = chat_service.stream_chat
    original_writable = dev_bot.is_writable_bot_policy
    original_browser_ready = dev_bot.browser_route_ready
    original_coordinator_context = getattr(
        chat_service, "build_coordinator_context", None)
    original_get_engine_session = getattr(
        chat_service, "_get_engine_session", None)
    engine_cls = getattr(chat_service, "EngineSession", None)
    original_handle_delegation = (
        getattr(engine_cls, "_handle_delegation", None)
        if engine_cls is not None else None)
    original_sync_delegated = getattr(
        chat_service, "sync_delegated_work", None)

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

    if callable(original_coordinator_context):
        @functools.wraps(original_coordinator_context)
        def coordinator_context(owner, coordinator_bot):
            base = original_coordinator_context(owner, coordinator_bot)
            hint = _bot_target_hint.get()
            if not hint:
                return base
            coordinator_id = str(
                (coordinator_bot or {}).get("id") or "").strip()
            if coordinator_id != str(hint.get("coordinator_bot_id") or ""):
                return base
            selected = str(hint.get("selected_bot_id") or "").strip()
            if not selected:
                return base
            tools = ", ".join(hint.get("shared_tools") or []) or "none"
            if selected == coordinator_id:
                directive = (
                    "Jev routing decision for THIS turn: keep the turn on this "
                    "coordinator. This is ROUTING ONLY. Kyrex still owns all "
                    "reasoning, tool use, recovery/fallback, and the user-facing "
                    "answer. You may delegate later only when your reasoning "
                    "discovers a genuine subtask."
                )
            else:
                directive = (
                    f"Jev routing decision for THIS turn: target Bot id "
                    f"{selected!r}. This is ROUTING ONLY: do not re-decide the "
                    "initial target. Kyrex still owns understanding the request, "
                    "writing the delegated task, tool use, recovery/fallback, "
                    "and the final user-facing answer. Delegate the user's "
                    "intent plainly; the host enforces the selected Bot id. "
                    "Owner-scoped connected tools are independent of Bot role."
                )
            return (
                str(base or "").rstrip()
                + "\n\n"
                + directive
                + f"\nHost-proven shared connected tools: {tools}."
            )

        chat_service.build_coordinator_context = coordinator_context

    if callable(original_get_engine_session):
        @functools.wraps(original_get_engine_session)
        def get_engine_session(*args, **kwargs):
            session = original_get_engine_session(*args, **kwargs)
            # Chat starts engine turns with threading.Thread, not
            # asyncio.to_thread, so ContextVars do not cross automatically.
            # Copy the bounded per-turn routing metadata onto the session while
            # still on the request thread; the worker then reads this exact
            # snapshot. A reused session is explicitly cleared on a non-routed
            # turn so no old decision can bleed into the next turn.
            hint = _bot_target_hint.get()
            session._jev_bot_target_hint = dict(hint) if hint else None
            return session

        chat_service._get_engine_session = get_engine_session

    if callable(original_handle_delegation):
        @functools.wraps(original_handle_delegation)
        def handle_delegation(self, frame):
            # IMPORTANT: run_turn executes in a manually-created worker thread.
            # Read the copy placed on this EngineSession by get_engine_session,
            # not the request-thread ContextVar.
            hint = getattr(self, "_jev_bot_target_hint", None)
            if not hint or hint.get("consumed"):
                return original_handle_delegation(self, frame)

            selected = str(hint.get("selected_bot_id") or "").strip()
            coordinator_id = str(
                hint.get("coordinator_bot_id") or "").strip()
            if not selected or selected == coordinator_id:
                # Jev kept the turn on Chief; Kyrex remains free to delegate a
                # later reasoned subtask through the normal coordinator path.
                return original_handle_delegation(self, frame)

            routed_frame = dict(frame or {})
            routed_frame["target_bot_id"] = selected
            # Jev owns ONLY the target choice. Kyrex's model-authored task text
            # is left byte-for-byte intact.
            hint["consumed"] = True

            # If the routed task is a Gmail read, use the existing shared
            # owner-scoped Gmail bridge instead of the delegation module's
            # legacy generic repo fallback. Non-Gmail work stays byte-identical
            # to the existing delegation path.
            routed_gmail = _submit_routed_gmail(
                chat_service, dev_bot, self, routed_frame, hint)
            if routed_gmail is not None:
                return routed_gmail
            return original_handle_delegation(self, routed_frame)

        engine_cls._handle_delegation = handle_delegation

    if callable(original_sync_delegated):
        @functools.wraps(original_sync_delegated)
        def sync_delegated_work(user, conversation_id):
            result = original_sync_delegated(user, conversation_id)
            _persist_delegated_gmail_results(
                chat_service, user, conversation_id, result)
            return result

        chat_service.sync_delegated_work = sync_delegated_work

    @functools.wraps(original_stream_chat)
    async def routed_stream_chat(
        user,
        conversation_id,
        user_content,
        cancel_event=None,
        workspace_id=None,
        request_id=None,
    ):
        route_hint = None
        bot_target_hint = None
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
                route_hint = "engine"

            # Coordinator Bot routing is a SEPARATE Jev question. The safe
            # candidate roster is host-supplied; current Chief is the fallback.
            if _is_coordinator(chat_service, bot):
                candidates = _routing_candidates(
                    chat_service, user, bot)
                shared = _shared_tools(dev_bot, bot)
                current_id = str(bot.get("id") or "").strip()
                bot_decision = jev_routing.decide_bot_target(
                    user_content, candidates, current_id,
                    shared_tools=shared)
                bot_target_hint = {
                    "coordinator_bot_id": current_id,
                    "selected_bot_id": bot_decision.get("selected_bot_id")
                    or current_id,
                    "source": bot_decision.get("source"),
                    "reason": bot_decision.get("reason"),
                    "confidence": bot_decision.get("confidence"),
                    "shared_tools": shared,
                    # Kept ONLY in this per-turn metadata so the host can run
                    # the existing deterministic connected-tool parser if the
                    # selected Bot is a mail specialist. It is never logged.
                    "request_text": str(user_content or "")[:4000],
                    "consumed": False,
                }

        route_token = _route_hint.set(route_hint)
        bot_token = _bot_target_hint.set(bot_target_hint)
        route_reset = False
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
                if not route_reset:
                    _route_hint.reset(route_token)
                    route_reset = True
                yield frame
        finally:
            if not route_reset:
                _route_hint.reset(route_token)
            # Bot routing is needed while original_stream_chat resolves the
            # engine session and builds the coordinator context. The worker has
            # its own session copy before it starts, so clearing here cannot
            # erase an in-flight delegation decision.
            _bot_target_hint.reset(bot_token)

    chat_service.stream_chat = routed_stream_chat
    chat_service._jev_routing_installed = True
    _installed = True
