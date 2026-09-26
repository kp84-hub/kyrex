"""Keep Kyrex mail-specialist routing on the owner Gmail connector.

Jev decides WHERE a coordinator turn should go. Kyrex still decides HOW to
execute it. Four gaps remained after shared connected-tool routing landed:

* if Jev kept the turn on Chief and Kyrex later reasoned that an Email Bot was
  the right subtask target, the normal delegation path could use the model's
  rephrased task text and lose the original mail lookup intent;
* Gmail query derivation treated any trailing ``for ...`` as the whole query,
  so a request such as ``Wake Christian ... field trip for 4th grade`` became
  merely ``4th grade``;
* a completed delegated Gmail read was copied into the parent conversation only
  when the Delegated Work UI happened to sync, so an immediate ``add that to my
  calendar`` could race ahead of ``gmail_selected``; and
* a single-date event with no time at all forced a redundant time question even
  though Calendar already has an approval preview where an all-day proposal can
  be reviewed safely.

This compatibility shim is installed after Jev. It does not give Jev any new
reasoning authority and it grants no connector scope. It only:

* reuses the original user request when Kyrex later delegates a connected-tool
  subtask after Jev kept Chief;
* permits noun-less Gmail coercion only for read/lookup-shaped requests and a
  host-visible mail-specialist target;
* derives the bounded Gmail fallback query from all meaningful request terms
  rather than truncating at a grammatical ``for``;
* reconciles already-completed delegated work before each new turn, so selected
  Gmail state is durable conversation state rather than a UI-poll race;
* keeps only the newest completed Gmail delegation authoritative for current
  selection/continuation state; and
* treats a title+single-date email event with NO time evidence as an all-day
  Calendar proposal. Partial or conflicting time evidence still fails closed.

Gmail mutations still fail closed, connector readiness is rechecked by the
existing Gmail bridge, and Calendar create/delete approvals remain unchanged.
Non-mail delegation still uses the existing Rift/provider path.
"""
from __future__ import annotations

import functools
import re

_installed = False

_LOOKUP_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+|could\s+you\s+|would\s+you\s+)?"
    r"(?:find|search|look\s+up|look\s+for|lookup|locate|fetch|retrieve|"
    r"investigate|read|open|show|view|check|get|tell\s+me|what|when|where|who)\b",
    re.IGNORECASE,
)

# Natural prose around a topic is not useful as a Gmail search term. These are
# deliberately tiny additions to serve.py's existing stopword set.
_QUERY_NOISE = frozenset({
    "mention", "mentions", "mentioned", "mentioning",
    "detail", "details", "information", "info",
})


def _mail_specialist(hint: dict | None) -> bool:
    hint = hint or {}
    haystack = " ".join((
        str(hint.get("selected_bot_id") or ""),
        str(hint.get("selected_bot_name") or ""),
        str(hint.get("selected_bot_role") or ""),
        str(hint.get("selected_bot_description") or ""),
    )).lower()
    return bool(re.search(r"\b(?:email|gmail|mailbox|mail)\b", haystack))


def _grounded_search_command(serve, task: str, request: str) -> str | None:
    """Accept Kyrex's compact search plan only when it stays on the user topic.

    A canonical search may select a narrower or broader Gmail query after a
    no-match result. Multi-sentence instructions never become provider queries.
    The original request still gates the mail route and supplies the fallback.
    """
    prefix = "gmail: search "
    if not task.startswith(prefix) or "\n" in task or "\r" in task:
        return None
    canonical = serve.canonical_gmail_task(task)
    if not canonical:
        return None
    query = canonical[len(prefix):]
    if (not query or len(query) > 80 or len(query.split()) > 8
            or re.search(r"[.!?;,]\s", query)):
        return None
    # "including its location/deadline" names desired answer fields, not the
    # topic that grounded the user's mail lookup.
    topic = re.split(r"\bincluding\b", request, maxsplit=1, flags=re.IGNORECASE)[0]
    tokens = lambda text: {
        t.lower() for t in re.findall(r"[A-Za-z0-9]+", text)
        if len(t) >= 3 and t.lower() not in serve._GMAIL_QUERY_STOPWORDS
        and t.lower() not in _QUERY_NOISE
    }
    if not tokens(query).intersection(tokens(topic)):
        return None
    return canonical


def bounded_gmail_command(chat_service, task_text: str,
                          original_request: str,
                          *, hint: dict | None = None) -> str | None:
    """Return one canonical Gmail read for a routed mail turn, or ``None``.

    The original request authorizes the Gmail lookup. Kyrex may choose a short,
    topic-grounded canonical search for HOW to find it; verbose instructions
    and unrelated searches fall back to the original request. A request with
    no mail noun needs a lookup shape AND a mail-specialist route.
    """
    serve = chat_service.serve
    task = str(task_text or "").strip()
    request = str(original_request or "").strip()

    if request:
        canonical_request = serve.canonical_gmail_task(request)
        if canonical_request:
            return canonical_request
        natural_request = serve.natural_gmail_command(request)
        mail_lookup = bool(_LOOKUP_RE.match(request) and
                           (natural_request or _mail_specialist(hint)))
        if not mail_lookup:
            return natural_request
        mutate_re = getattr(serve, "_GMAIL_MUTATE_RE", None)
        try:
            if mutate_re is not None and mutate_re.match(request):
                return None
        except Exception:
            return None

        # An exact user-selected message or newest-message read is authoritative.
        if natural_request and natural_request.startswith((
                "gmail: read id ", "gmail: message ", "gmail: latest")):
            return natural_request
        chosen_search = _grounded_search_command(serve, task, request)
        if chosen_search:
            return chosen_search
        if natural_request:
            return natural_request

        # A nounless request is permitted only on the mail-specialist route.
        # A trailing "including its ..." clause asks for answer fields; the
        # topical lookup before it is the bounded search fallback.
        detail_clause = re.search(
            r"\bincluding\s+(?:its|their|the)\b", request, re.IGNORECASE)
        topic_request = (request[:detail_clause.start()].rstrip(" ,;")
                         if detail_clause else request)
        return serve.natural_gmail_command(f"Read my email and {topic_request}")

    # Compatibility for callers that genuinely have no original request.
    canonical = serve.canonical_gmail_task(task)
    if canonical:
        return canonical
    return serve.natural_gmail_command(task)


def full_gmail_query(serve, text: str) -> str:
    """Derive the existing bounded query without the lossy generic ``for`` cut.

    Sender/topic clauses retain the original specialized behavior. Otherwise
    all meaningful tokens survive the existing stopword filter. Therefore
    ``search my email for Tesla`` still becomes ``Tesla``, while
    ``Wake Christian ... field trip for 4th grade`` keeps both the school/topic
    context and ``4th grade`` instead of collapsing to the trailing phrase.
    """
    text = str(text or "")
    sender = serve._gmail_sender_phrase(text)
    topic = serve._gmail_topic_phrase(text)

    if sender and topic:
        combined = f"{serve._gmail_from_term(sender)} {serve._gmail_quote_phrase(topic)}"
        if len(combined) > serve._GMAIL_QUERY_MAX:
            combined = combined[:serve._GMAIL_QUERY_MAX].strip()
        return combined
    if sender:
        return serve._bound_gmail_query(serve._gmail_from_term(sender))
    if topic:
        return serve._bound_gmail_query(topic)

    tokens = re.findall(r"[A-Za-z0-9_.@\-]+", text)
    stop = set(serve._GMAIL_QUERY_STOPWORDS) | set(_QUERY_NOISE)
    kept = [token for token in tokens if token.lower() not in stop]
    return serve._bound_gmail_query(" ".join(kept))


def calendar_handoff_facts(facts: dict | None) -> dict:
    """Return facts with the one safe date-only -> all-day default applied.

    A Calendar proposal may default to all-day only when the email has a title
    and one unambiguous event date and contains NO start/end time evidence. A
    partial time, conflicting time, or ambiguous date is never rewritten.
    This is a proposal default, not a provider write: the existing Calendar
    approval gate still previews the exact all-day event before creation.
    """
    out = dict(facts or {})
    if not out.get("title") or not out.get("date"):
        return out
    if out.get("date_ambiguous") or out.get("time_ambiguous"):
        return out
    if out.get("all_day") or out.get("start") or out.get("end"):
        return out

    out["all_day"] = True
    for key in ("missing", "ambiguous", "needs"):
        values = out.get(key)
        if isinstance(values, (list, tuple)):
            out[key] = [value for value in values if value != "time"]
    return out


def _install_calendar_handoff_defaults(chat_service) -> None:
    """Apply the date-only all-day default to both direct and routed handoffs."""
    email_event = getattr(chat_service, "email_event", None)
    if email_event is None or getattr(
            email_event, "_kyrex_date_only_all_day_installed", False):
        return
    original_required = getattr(email_event, "required_needs", None)
    original_args = getattr(email_event, "event_intent_args", None)
    if not callable(original_required) or not callable(original_args):
        return

    @functools.wraps(original_required)
    def required_needs(facts):
        return original_required(calendar_handoff_facts(facts))

    @functools.wraps(original_args)
    def event_intent_args(facts):
        return original_args(calendar_handoff_facts(facts))

    email_event.required_needs = required_needs
    email_event.event_intent_args = event_intent_args
    email_event._kyrex_date_only_all_day_installed = True


def latest_completed_gmail_sync(sync_result: dict | None) -> dict | None:
    """Narrow a newest-first delegation sync to its newest completed Gmail row.

    ``CloudTaskStore.list_delegations`` is newest-first. Selection state is a
    current-conversation pointer, so replaying every historical Gmail result
    would let an older row overwrite a newer read. The first completed Gmail
    row is therefore the only one allowed to update ``gmail_selected`` and the
    Gmail continuation state.
    """
    result = sync_result if isinstance(sync_result, dict) else {}
    for view in result.get("delegations") or []:
        if (str(view.get("status") or "") == "done"
                and str(view.get("executor_prefix") or "") == "gmail"):
            narrowed = dict(result)
            narrowed["delegations"] = [view]
            return narrowed
    return None


def _reasoned_connected_delegate(chat_service, dev_bot, jev_stream_router,
                                 session, frame: dict):
    """Try connected adapters after Kyrex chooses a peer while Jev kept Chief.

    The existing Jev wrapper already handles the initial peer Jev chose. This
    function covers the other legitimate architecture path: Jev leaves the turn
    on Chief, Kyrex reasons, and Kyrex later emits a delegation to a specialist.
    The original request remains in the per-turn hint, so a model paraphrase
    cannot erase the source intent before deterministic connected-tool parsing.
    """
    hint = getattr(session, "_jev_bot_target_hint", None)
    if not isinstance(hint, dict) or not hint:
        return None

    selected = str(hint.get("selected_bot_id") or "").strip()
    coordinator = str(hint.get("coordinator_bot_id") or "").strip()
    # A peer selected up front is already handled by Jev's installed wrapper.
    if selected and selected != coordinator:
        return None

    retry = jev_stream_router._fallback_hint_for_frame(
        chat_service, session, frame, hint)

    # Keep the same precedence as the existing Jev connected-tool fallback:
    # strict selected-email Calendar handoff first, then Gmail read.
    routed_calendar = jev_stream_router._submit_routed_email_calendar(
        chat_service, dev_bot, session, frame, retry)
    if routed_calendar is not None:
        return routed_calendar

    routed_gmail = jev_stream_router._submit_routed_gmail(
        chat_service, dev_bot, session, frame, retry)
    if routed_gmail is not None:
        return routed_gmail
    return None


def _turn_identity(args, kwargs):
    """Return ``(user, conversation_id)`` from the stable stream_chat shape."""
    user = kwargs.get("user")
    conversation_id = kwargs.get("conversation_id")
    if user is None and len(args) >= 1:
        user = args[0]
    if conversation_id is None and len(args) >= 2:
        conversation_id = args[1]
    return str(user or "").strip(), str(conversation_id or "").strip()


def install(chat_service, dev_bot, jev_stream_router, serve) -> None:
    """Install the bounded mail handoff after Jev's routing wrapper."""
    global _installed
    if _installed or getattr(chat_service, "_mail_routing_bridge_installed", False):
        _installed = True
        return

    engine_cls = getattr(chat_service, "EngineSession", None)
    current_handle = getattr(engine_cls, "_handle_delegation", None) if engine_cls else None
    current_stream = getattr(chat_service, "stream_chat", None)
    if not callable(current_handle) or not callable(current_stream):
        return
    required = (
        "_fallback_hint_for_frame", "_submit_routed_email_calendar",
        "_submit_routed_gmail",
    )
    if not all(callable(getattr(jev_stream_router, name, None)) for name in required):
        return

    # Tighten the same helper the Jev wrapper already calls; no second Gmail
    # grammar or route table is introduced.
    jev_stream_router._gmail_command_for_routed_turn = bounded_gmail_command

    # The Jev sync wrapper resolves this module global at CALL time. Narrow its
    # state-copy helper to the newest completed Gmail row so pre-turn sync cannot
    # resurrect an older selection.
    original_persist = getattr(
        jev_stream_router, "_persist_delegated_gmail_results", None)
    if callable(original_persist):
        @functools.wraps(original_persist)
        def persist_latest(chat, user, conversation_id, sync_result):
            narrowed = latest_completed_gmail_sync(sync_result)
            if narrowed is not None:
                return original_persist(chat, user, conversation_id, narrowed)
            return None

        jev_stream_router._persist_delegated_gmail_results = persist_latest

    # natural_gmail_command resolves _gmail_query_from dynamically from the
    # serve module, so one narrow replacement fixes direct and delegated reads.
    serve._gmail_query_from = lambda text: full_gmail_query(serve, text)

    # Direct Chat and Jev's routed email->Calendar adapter share the same
    # email_event module object, so installing the proposal default once keeps
    # both paths identical without weakening Calendar's approval executor.
    _install_calendar_handoff_defaults(chat_service)

    @functools.wraps(current_handle)
    def handle_delegation(self, frame):
        routed = _reasoned_connected_delegate(
            chat_service, dev_bot, jev_stream_router, self, frame or {})
        if routed is not None:
            return routed
        return current_handle(self, frame)

    @functools.wraps(current_stream)
    async def stream_chat(*args, **kwargs):
        # Completed delegated Gmail results are authoritative durable state, not
        # a frontend concern. Reconcile them BEFORE routing the next turn so an
        # immediate "add that to my calendar" cannot beat the UI's Delegated
        # Work poll and lose the selected email. The sync is owner-scoped,
        # idempotent, and fail-soft; the current turn remains available if the
        # status view itself is temporarily unavailable.
        user, conversation_id = _turn_identity(args, kwargs)
        if user and conversation_id:
            try:
                chat_service.sync_delegated_work(user, conversation_id)
            except Exception:
                pass
        async for frame in current_stream(*args, **kwargs):
            yield frame

    engine_cls._handle_delegation = handle_delegation
    chat_service.stream_chat = stream_chat
    chat_service._mail_routing_bridge_installed = True
    _installed = True
