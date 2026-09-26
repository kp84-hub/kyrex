"""Install Jev as Kyrex Chat's bounded routing control plane.

Jev decides WHERE a turn should go; Kyrex still decides WHAT the task means,
which tools/operations are legal, how to execute them, how to recover, and what
to tell the user.

Two independent hints are installed:

* execution-route hint — preserves the original bounded Developer/Browser
  de-escalation (repo/browser -> engine only);
* Bot-target hint — on coordinator turns, Jev chooses one host-supplied Bot id
  from the owner's safe, running roster. The decision is injected into the
  coordinator context as ROUTING ONLY, and the first delegate_task target is
  host-enforced to that id. Kyrex still writes the delegated task text.

Owner-scoped connected tools remain separate from Bot identity and from repo
workspace eligibility. A running same-owner Bot may receive shared connected-
tool work even when it has no usable Rift/provider for repo-style delegation.
Repo work keeps the existing Rift/provider gates; Gmail reads and the existing
email -> Calendar handoff remain on their owner-scoped bridges. Their connector
scope, result bounds, and Calendar approval gate remain authoritative.

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


def _reset_context_token(var, token) -> bool:
    """Reset *token* without crashing async-generator cross-context cleanup.

    Starlette/asyncio may finalize an abandoned async generator from an
    ``async_generator_athrow`` task whose Context is not the request Context
    that created the token. ``ContextVar.reset`` rejects that with ``ValueError:
    ... was created in a different Context``. The originating request task is
    already being torn down in that case, so there is no live Context to mutate
    or leak into another request. Suppress ONLY that exact cross-context reset;
    every other reset error remains visible.
    """
    try:
        var.reset(token)
        return True
    except ValueError as exc:
        if "different Context" in str(exc):
            return False
        raise


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


def _owned_running_bot(chat_service, owner: str, bot_id: str):
    """Resolve a same-owner RUNNING Bot without imposing repo/Rift eligibility.

    Shared connected tools (Gmail/Calendar) are owner-scoped and do not require
    a repository workspace or a Bot provider. Repo-style delegation continues
    to use delegation.resolve_delegation_target and its stronger Rift/provider
    gates. This helper never grants a tool; the tool-specific readiness check
    still runs after it.
    """
    owner = str(owner or "").strip()
    bot_id = str(bot_id or "").strip()
    if not owner or not bot_id:
        return None
    try:
        registry = chat_service.bots.load_bots()
        bot = registry.get(bot_id)
    except Exception:
        return None
    if not isinstance(bot, dict):
        return None
    if str(bot.get("owner") or "").strip() != owner:
        return None
    try:
        if not chat_service.bots.is_running(bot):
            return None
    except Exception:
        if str(bot.get("status") or "").strip() != "running":
            return None
    return bot


def _routing_candidates(chat_service, user: str, coordinator: dict) -> list[dict]:
    """Current coordinator + same-owner RUNNING peers, safe metadata only.

    The old delegation roster's ``available`` flag means "repo Rift resolves".
    That is intentionally NOT a prerequisite here: Jev is choosing a Bot route,
    and owner-scoped connected tools may legitimately run on a Bot with no Rift.
    If Kyrex later chooses repo-style work, the existing delegation resolver
    re-applies the Rift/provider requirements before that task can be created.
    """
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
            peer_id = str(meta.get("id") or "").strip()
            if not peer_id:
                continue
            peer = _owned_running_bot(chat_service, owner, peer_id)
            if peer is None:
                continue
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
    # Unlike email_calendar_route_ready (which proves only same-owner/running
    # identity), this Jev metadata must say calendar_write ONLY when the owner's
    # connection actually carries the write scope. Execution re-checks it too.
    try:
        owner = str((bot or {}).get("owner") or "").strip()
        write_ready = getattr(dev_bot, "_calendar_write_available", None)
        if (owner and dev_bot.email_calendar_route_ready(bot)
                and callable(write_ready) and write_ready(owner)):
            tools.append("calendar_write")
    except Exception:
        pass
    return tools


def _selected_bot_is_mail_specialist(hint: dict) -> bool:
    """True only when host-visible safe metadata clearly names a mail specialist.

    This is a ROUTING hint, never a permission grant. It exists so a natural
    request like "find the 4th grade field trip" may be interpreted as a Gmail
    read after Jev deliberately chose ``email-bot`` even though the raw request
    did not contain the noun "email". Arbitrary peer selections are never
    coerced into Gmail merely because Gmail is owner-connected.
    """
    hint = hint or {}
    haystack = " ".join((
        str(hint.get("selected_bot_id") or ""),
        str(hint.get("selected_bot_name") or ""),
        str(hint.get("selected_bot_role") or ""),
        str(hint.get("selected_bot_description") or ""),
    )).lower()
    return any(word in haystack for word in ("email", "gmail", "mailbox"))


def _gmail_command_for_routed_turn(chat_service, task_text: str,
                                   original_request: str,
                                   *, hint: dict | None = None) -> str | None:
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
    natural_request = serve.natural_gmail_command(request)
    if natural_request:
        return natural_request

    # A request that does not explicitly name mail may be interpreted as mail
    # ONLY when Jev selected a host-visible mail specialist. Jev still supplies
    # no query/tool args: Kyrex's existing deterministic parser derives those
    # from the untouched request after adding only the mail-object cue it needs.
    # Mail mutations remain refused and never become a read by prefixing text.
    if not request or not _selected_bot_is_mail_specialist(hint or {}):
        return None
    mutate_re = getattr(serve, "_GMAIL_MUTATE_RE", None)
    try:
        if mutate_re is not None and mutate_re.match(request):
            return None
    except Exception:
        return None
    return serve.natural_gmail_command(f"Read my email and {request}")


def _submit_routed_gmail(chat_service, dev_bot, session, frame: dict,
                         hint: dict):
    """Submit ONE Jev-routed Gmail delegation, or None when it is not Gmail.

    Once Kyrex's existing deterministic Gmail parser positively identifies the
    turn as a Gmail read, this function NEVER returns ``None`` merely because
    the routed Bot/connector is unavailable. ``None`` means only "this is not a
    Gmail request". A recognized Gmail request returns a connected-tool error
    instead of falling through to generic repo/Rift delegation.
    """
    canonical = _gmail_command_for_routed_turn(
        chat_service, frame.get("task"),
        hint.get("request_text") or "", hint=hint)
    if not canonical:
        return None

    ctx = getattr(session, "delegation_ctx", None) or {}
    owner = str(ctx.get("owner") or "").strip()
    coordinator = ctx.get("bot") or {}
    coordinator_id = str(coordinator.get("id") or "").strip()
    target_id = str(hint.get("selected_bot_id") or "").strip()
    if not owner or not coordinator_id or not target_id:
        return False, {"error": "Gmail routing is missing its delegation context."}

    target = _owned_running_bot(chat_service, owner, target_id)
    if target is None:
        return False, {"error": (
            "The routed Bot is not available for Gmail connected-tool work.")}
    try:
        if not dev_bot.gmail_route_ready(target):
            return False, {"error": (
                "Gmail read is unavailable for this account or routed Bot. "
                "The request was not sent to the repo executor.")}
    except Exception:
        return False, {"error": (
            "Gmail read readiness could not be verified. The request was not "
            "sent to the repo executor.")}

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


def _submit_routed_email_calendar(chat_service, dev_bot, session, frame: dict,
                                  hint: dict):
    """Route the selected-email Calendar handoff through its existing safe path.

    Triggered ONLY by the existing bounded pronoun recognizer ("add that to my
    calendar" etc.). The selected Gmail facts are read from the PARENT
    conversation, reduced by the existing deterministic email_event helper,
    validated by cal_writer, then submitted through ``email_calendar``. That
    executor still owns the mandatory exact-payload approval and the owner's
    Calendar-write connector scope. Bot role/Rift/provider are not permissions.
    """
    request = str((hint or {}).get("request_text") or "").strip()
    email_event = getattr(chat_service, "email_event", None)
    cal_writer = getattr(chat_service, "cal_writer", None)
    if email_event is None or cal_writer is None:
        return None
    try:
        if not email_event.is_add_to_calendar_request(request):
            return None
    except Exception:
        return None

    ctx = getattr(session, "delegation_ctx", None) or {}
    owner = str(ctx.get("owner") or "").strip()
    coordinator_id = str((ctx.get("bot") or {}).get("id") or "").strip()
    conversation_id = str(ctx.get("conversation_id") or "").strip()
    target_id = str((hint or {}).get("selected_bot_id") or "").strip()
    if not owner or not coordinator_id or not target_id or not conversation_id:
        return False, {"error": "Calendar handoff is missing its conversation context."}

    target = _owned_running_bot(chat_service, owner, target_id)
    if target is None:
        return False, {"error": "The routed Bot is not available for connected-tool work."}
    try:
        if not dev_bot.email_calendar_route_ready(target):
            return False, {"error": "The routed Bot cannot receive the Calendar handoff."}
    except Exception:
        return False, {"error": "The Calendar handoff is unavailable."}

    try:
        conv = chat_service.get_conversation(owner, conversation_id) or {}
        selected = conv.get("gmail_selected")
    except Exception:
        selected = None
    if not isinstance(selected, dict) or not selected:
        return False, {"error": (
            "I don't have a selected email to add. Read the email first, then "
            "ask me to add that to your calendar.")}
    facts = selected.get("facts")
    if not isinstance(facts, dict) or not facts:
        return False, {"error": "The selected email has no validated event facts yet."}
    try:
        if email_event.required_needs(facts):
            return False, {"error": (
                email_event.need_prompt(facts) + "\n\n"
                + email_event.render_details(facts))[:2000]}
    except Exception:
        return False, {"error": "The selected email does not have enough event detail yet."}

    write_ready = getattr(dev_bot, "_calendar_write_available", None)
    try:
        if not callable(write_ready) or not write_ready(owner):
            return False, {"error": (
                "Google Calendar write access isn't enabled for your account. "
                "Enable it in Settings and try again.")}
    except Exception:
        return False, {"error": "Google Calendar write access is unavailable."}

    try:
        title, date, start, end, all_day = email_event.event_intent_args(facts)
        intent = cal_writer.build_intent(
            title, date, start, end, all_day=all_day)
    except Exception as exc:
        return False, {"error": str(exc)[:1000] or "The event could not be validated."}

    intent_text = json.dumps(intent)
    store = chat_service._task_store()
    visible_text = str((frame or {}).get("task") or request).strip()[:2000]
    delegation_id = store.create_delegation(
        owner=owner,
        coordinator_bot_id=coordinator_id,
        target_bot_id=target_id,
        task_text=visible_text or request,
        parent_conversation_id=conversation_id,
        parent_task_id=ctx.get("parent_task_id"),
        parent_delegation_id=None,
        depth=1,
        executor_prefix="email_calendar",
        status="queued",
    )
    try:
        task_id = dev_bot.submit_email_calendar_task(
            owner, target, intent_text, store=store,
            conversation_id=conversation_id)
    except Exception as exc:
        error = f"could not create target Calendar task: {exc}"
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


def _fallback_hint_for_frame(chat_service, session, frame: dict,
                             hint: dict) -> dict:
    """Kyrex-chosen retry target after Jev's initial route was consumed.

    Jev is NOT asked again. Kyrex may reason from a failed/incomplete delegated
    result and choose one alternate Bot. We preserve the original request and
    attach only safe target metadata so the same owner-scoped connected-tool
    adapters can be reused once; generic repo delegation remains unchanged.
    """
    retry = dict(hint or {})
    target_id = str((frame or {}).get("target_bot_id") or "").strip()
    retry["selected_bot_id"] = target_id
    ctx = getattr(session, "delegation_ctx", None) or {}
    owner = str(ctx.get("owner") or "").strip()
    target = _owned_running_bot(chat_service, owner, target_id)
    if target is not None:
        safe = _candidate(chat_service, target)
        retry["selected_bot_name"] = safe.get("name") or ""
        retry["selected_bot_role"] = safe.get("role") or ""
        retry["selected_bot_description"] = safe.get("description") or ""
    return retry


def _gmail_detail_followup_guidance(hint: dict | None) -> str:
    """Tell Kyrex how to finish email-detail lookups using bounded Gmail state."""
    if "gmail_read" not in set((hint or {}).get("shared_tools") or []):
        return ""
    return (
        "\nFor an email/Gmail request asking for details, do not treat a subject "
        "or search snippet as the completed answer. Search using a short topic "
        "grounded in the user's request, not the full question or answer fields "
        "such as location or deadline. For a new search, delegate task text "
        "exactly `gmail: search <topic>`, choosing the topic yourself. If no "
        "messages match, try a different shorter relevant query; do not repeat "
        "the same failed search. Compare the returned search "
        "results with the user's request and choose the most relevant message. "
        "When its body is needed, continue in this same turn by calling "
        "delegate_task for the selected Email/Gmail Bot with task text exactly "
        "`read number N`, replacing N with that message's displayed result number. "
        "Do not assume a fixed result position, invent a message id, or ask the "
        "user to request the read. The host resolves this bounded continuation "
        "against the stored Gmail result page. If a message only points to a "
        "form/link or does not answer the requested detail, do not stop there: "
        "reason over the remaining plausible hits on that same result page and "
        "read them one at a time, in the same turn, until the requested fact is "
        "found or the relevant candidates are exhausted. Choose each displayed "
        "result number from the current page; do not hardcode positions or "
        "repeat a read/search that already failed. Answer from the message "
        "bodies, and say clearly what remains unknown only after those bounded "
        "candidates are exhausted."
    )


def _calendar_read_guidance(hint: dict | None) -> str:
    """Keep Calendar lookups off write-only routes."""
    shared = set((hint or {}).get("shared_tools") or [])
    if "calendar_read" in shared:
        return ""
    return (
        "\nFor this turn, the host has not proven a calendar_read capability. "
        "Do not delegate Calendar lookup/search/read requests to a write-only "
        "Calendar Bot or try to express a read as a create command. "
        "calendar_write, when listed, supports event creation only. "
        "Use a host-proven read source such as Gmail when appropriate, or state "
        "that Calendar data is unavailable."
    )


def install(chat_service, dev_bot) -> None:
    """Install the active Jev routing shim exactly once."""
    global _installed
    if _installed or getattr(chat_service, "_jev_routing_installed", False):
        _installed = True
        return

    original_stream_chat = chat_service.stream_chat
    original_writable = dev_bot.is_writable_bot_policy
    original_browser_ready = dev_bot.browser_route_ready
    original_gmail_ready = dev_bot.gmail_route_ready
    original_email_calendar_ready = dev_bot.email_calendar_route_ready
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

    def _jev_selected_peer_for(bot) -> bool:
        hint = _bot_target_hint.get()
        if not hint:
            return False
        current = str((bot or {}).get("id") or "").strip()
        coordinator = str(hint.get("coordinator_bot_id") or "").strip()
        selected = str(hint.get("selected_bot_id") or "").strip()
        return bool(current and current == coordinator and selected
                    and selected != current)

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

    @functools.wraps(original_gmail_ready)
    def gmail_ready(bot):
        # When Jev routed this coordinator turn to a peer, do not let the
        # coordinator's direct shared Gmail shortcut intercept it before Kyrex
        # can delegate. The peer task still re-runs original Gmail readiness.
        if _jev_selected_peer_for(bot):
            return False
        return original_gmail_ready(bot)

    @functools.wraps(original_email_calendar_ready)
    def email_calendar_ready(bot):
        # Same rule for the selected-email Calendar handoff: Jev selected the
        # peer; Kyrex reasons/delegates; the host then uses the existing shared
        # email_calendar path under that peer identity.
        if _jev_selected_peer_for(bot):
            return False
        return original_email_calendar_ready(bot)

    dev_bot.is_writable_bot_policy = writable_policy
    dev_bot.browser_route_ready = browser_ready
    dev_bot.gmail_route_ready = gmail_ready
    dev_bot.email_calendar_route_ready = email_calendar_ready

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
            gmail_detail_guidance = _gmail_detail_followup_guidance(hint)
            calendar_read_guidance = _calendar_read_guidance(hint)
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
                    "Owner-scoped connected tools are independent of Bot role "
                    "and DO NOT require that target's repo Rift/provider."
                )
            return (
                str(base or "").rstrip()
                + "\n\n"
                + directive
                + f"\nHost-proven shared connected tools: {tools}."
                + "\nA roster 'available' flag describes repo/Rift execution; "
                  "it does not block owner-scoped connected-tool work."
                + gmail_detail_guidance
                + calendar_read_guidance
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
            if not hint:
                return original_handle_delegation(self, frame)

            # After Jev's initial target has been tried, Kyrex may reason from
            # the result and choose ONE alternate Bot. Jev is not asked again.
            # The retry may still use the same owner-scoped connected-tool
            # adapters; generic repo delegation stays on the existing path.
            if hint.get("consumed"):
                if int(hint.get("connected_fallbacks") or 0) >= 1:
                    return original_handle_delegation(self, frame)
                retry_hint = _fallback_hint_for_frame(
                    chat_service, self, frame, hint)
                routed_calendar = _submit_routed_email_calendar(
                    chat_service, dev_bot, self, frame, retry_hint)
                if routed_calendar is not None:
                    hint["connected_fallbacks"] = 1
                    return routed_calendar
                routed_gmail = _submit_routed_gmail(
                    chat_service, dev_bot, self, frame, retry_hint)
                if routed_gmail is not None:
                    hint["connected_fallbacks"] = 1
                    return routed_gmail
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

            # Connected-tool specializations reuse EXISTING owner-scoped paths.
            # Calendar handoff is checked first because its strict pronoun shape
            # is an action, not a Gmail read. Gmail then handles mail lookup.
            routed_calendar = _submit_routed_email_calendar(
                chat_service, dev_bot, self, routed_frame, hint)
            if routed_calendar is not None:
                return routed_calendar
            routed_gmail = _submit_routed_gmail(
                chat_service, dev_bot, self, routed_frame, hint)
            if routed_gmail is not None:
                return routed_gmail
            # Anything else keeps existing delegation semantics, including the
            # repo Rift/provider eligibility checks and their recoverable error.
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
                selected_id = (bot_decision.get("selected_bot_id")
                               or current_id)
                selected_meta = next(
                    (c for c in candidates if c.get("id") == selected_id), {})
                bot_target_hint = {
                    "coordinator_bot_id": current_id,
                    "selected_bot_id": selected_id,
                    "selected_bot_name": selected_meta.get("name") or "",
                    "selected_bot_role": selected_meta.get("role") or "",
                    "selected_bot_description": (
                        selected_meta.get("description") or ""),
                    "source": bot_decision.get("source"),
                    "reason": bot_decision.get("reason"),
                    "confidence": bot_decision.get("confidence"),
                    "shared_tools": shared,
                    # Kept ONLY in this per-turn metadata so the host can run
                    # existing deterministic connected-tool parsers. It is
                    # never written to Jev routing telemetry.
                    "request_text": str(user_content or "")[:4000],
                    "consumed": False,
                    "connected_fallbacks": 0,
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
                    route_reset = _reset_context_token(_route_hint, route_token)
                yield frame
        finally:
            if not route_reset:
                _reset_context_token(_route_hint, route_token)
            # Bot routing is needed while original_stream_chat resolves the
            # engine session and builds the coordinator context. The worker has
            # its own session copy before it starts. On client disconnect,
            # async-generator finalization may run in another Context; the
            # narrow reset helper suppresses only that teardown-only mismatch.
            _reset_context_token(_bot_target_hint, bot_token)

    chat_service.stream_chat = routed_stream_chat
    chat_service._jev_routing_installed = True
    _installed = True