"""Owner-scoped Bot rights for Kyrex Chat.

Bots are routing specializations, not permission containers.  Every Bot owned by
an authenticated user receives the same host operation policy at execution
time.  Operation tiers / approvals remain owned by :mod:`serve`; connector
scopes, repository workspaces, Browser Host bindings and allowlists remain
resource/readiness gates.

This module is deliberately an installation shim while the legacy preset fields
remain in the registry for backwards compatibility.  Stored preset policies are
therefore no longer authoritative for owner-connected operations or delegation,
but old records do not need a destructive migration.
"""
from __future__ import annotations

import contextvars
import re
from typing import Any

_installed = False
_request_text: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kyrex_owner_rights_request_text", default="")


def owner_operation_policy(serve) -> dict:
    """One runtime policy for every owned Bot, at host-defined tiers."""
    return dict(serve.OPERATION_TIERS)


def _running_owned(bots, bot: dict) -> bool:
    bot = bot or {}
    try:
        return bool(str(bot.get("owner") or "").strip() and bots.is_running(bot))
    except Exception:
        return False


def _same_owner(user, bot: dict) -> bool:
    return bool(str((bot or {}).get("owner") or "").strip() == str(user or "").strip())


def _calendar_read_available(owner: str) -> bool:
    owner = str(owner or "").strip()
    if not owner:
        return False
    try:
        import connectors
        store = connectors.default_store()
        return bool(
            store.scope_granted(owner, connectors.GOOGLE_CALENDAR_READ_SCOPE)
            or store.calendar_write_available(owner)
        )
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


def _browser_ready(bots, bot: dict) -> bool:
    """Browser is a resource readiness check, never a Bot-rights check."""
    if not _running_owned(bots, bot):
        return False
    allowlist = (bot or {}).get("browser_allowlist")
    if not isinstance(allowlist, list) or not any(
            isinstance(host, str) and host.strip() for host in allowlist):
        return False
    try:
        import browser_hosts
        return bool(browser_hosts.binding_for(bot.get("owner"), bot.get("id")))
    except Exception:
        return False


def _create_shaped(text: str, cal_writer) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    # A selected-email pronoun handoff has its own higher-priority route.
    if re.match(r"^\s*add\s+that\s+to\s+(?:my\s+)?calendar\b", raw, re.I):
        return False
    try:
        cal_writer.parse_create_request(raw)
        return True
    except Exception:
        # Preserve the useful deterministic usage response for malformed create
        # requests, but never make an ordinary conversational turn a writer turn.
        return bool(re.match(
            r"^\s*(?:create|add|schedule|book|reserve|make|set\s+up|put)\b",
            raw, re.I))


def _safe_reset(var, token) -> None:
    try:
        var.reset(token)
    except ValueError as exc:
        # Async-generator finalization may happen in a sibling Context (same
        # condition already handled by jev_stream_router).
        if "different Context" not in str(exc):
            raise


def _clone_with_policy(bot: dict, policy: dict) -> dict:
    out = dict(bot or {})
    out["policy"] = dict(policy)
    return out


def install(chat_service, dev_bot, bot_capabilities, delegation, serve,
            jev_stream_router=None) -> None:
    """Install the owner-rights compatibility layer once."""
    global _installed
    if _installed:
        return

    bots = chat_service.bots
    cal_writer = chat_service.cal_writer
    shared_policy = owner_operation_policy(serve)

    # ── Execution authority: same host operation policy for every owned Bot.
    original_build_context = serve.build_context

    def build_context(session_key, executor_prefix="repo", allow_bot_resolution=True):
        ctx = original_build_context(
            session_key, executor_prefix,
            allow_bot_resolution=allow_bot_resolution)
        if str(getattr(ctx, "bot_owner", "") or "").strip():
            ctx.policy = dict(shared_policy)
        return ctx

    serve.build_context = build_context

    # Strict Level-6 preset predicates are legacy identity checks.  The fixed
    # Level-6 operations may also execute under the shared owner policy; the
    # pinned command/resource checks remain unchanged.
    original_l6_weekly = serve.level6_weekly_granted
    original_l6_calendar = serve.level6_calendar_granted
    original_l6_authorized = serve.level6_calendar_read_authorized

    def _shared(policy) -> bool:
        return isinstance(policy, dict) and policy == shared_policy

    serve.level6_weekly_granted = (
        lambda policy: _shared(policy) or original_l6_weekly(policy))
    serve.level6_calendar_granted = (
        lambda policy: _shared(policy) or original_l6_calendar(policy))
    serve.level6_calendar_read_authorized = (
        lambda policy: _shared(policy) or original_l6_authorized(policy))

    # ── Chat coordination is a host surface available to every owned Bot.
    original_derive = bot_capabilities.derive_bot_capabilities

    def derive_bot_capabilities(policy):
        result = original_derive(policy)
        tools = set(result.get("tools") or [])
        tools.update(("delegate_task", "delegation_status"))
        result["tools"] = sorted(tools)
        decisions = dict(result.get("decisions") or {})
        for tool in ("delegate_task", "delegation_status"):
            decisions[tool] = {
                "operation": "bot:delegate",
                "derived_tier": serve.OPERATION_TIERS["bot:delegate"],
                "effective_tier": 0,
                "matched_rule": "owner-scoped-host-grant",
                "reason": "owned Bots share Kyrex coordination rights",
                "allowed": True,
            }
        result["decisions"] = decisions
        result["host_granted"] = sorted(
            set(result.get("host_granted") or [])
            | {"delegate_task", "delegation_status"})
        return result

    bot_capabilities.derive_bot_capabilities = derive_bot_capabilities

    # Host-side delegation authority: ownership + running lifecycle, not a role
    # preset. Target ownership, depth, executor, connector and approval gates are
    # still enforced by delegation.py / the selected executor.
    def coordinator_granted(bot) -> bool:
        return _running_owned(bots, bot or {})

    serve.coordinator_granted = coordinator_granted

    # ── Per-turn text for deterministic direct-route readiness.
    original_stream_chat = chat_service.stream_chat

    async def stream_chat(*args, **kwargs):
        text = kwargs.get("user_content")
        if text is None and len(args) >= 3:
            text = args[2]
        token = _request_text.set(str(text or ""))
        try:
            async for frame in original_stream_chat(*args, **kwargs):
                yield frame
        finally:
            _safe_reset(_request_text, token)

    chat_service.stream_chat = stream_chat

    # ── Owner-connected Calendar + fixed schedule routes.
    def calendar_route_ready(bot) -> bool:
        return (_running_owned(bots, bot)
                and _calendar_read_available(bot.get("owner")))

    def calendar_writer_route_ready(bot) -> bool:
        return (_running_owned(bots, bot)
                and _calendar_write_available(bot.get("owner"))
                and _create_shaped(_request_text.get(""), cal_writer))

    def calendar_editor_route_ready(bot) -> bool:
        return (_running_owned(bots, bot)
                and _calendar_write_available(bot.get("owner")))

    def glofox_route_ready(bot) -> bool:
        return _running_owned(bots, bot)

    def level6_route_ready(bot) -> bool:
        return _running_owned(bots, bot)

    def level6_calendar_route_ready(bot) -> bool:
        return (_running_owned(bots, bot)
                and _calendar_read_available(bot.get("owner")))

    def level6_message_route_ready(bot) -> bool:
        return (_running_owned(bots, bot)
                and _calendar_read_available(bot.get("owner")))

    # The old unified Calendar role becomes irrelevant to routing. General
    # calendar/read/create/delete predicates above now apply to every owned Bot.
    dev_bot.calendar_bot_route_ready = lambda _bot: False
    dev_bot.calendar_route_ready = calendar_route_ready
    dev_bot.calendar_writer_route_ready = calendar_writer_route_ready
    dev_bot.calendar_editor_route_ready = calendar_editor_route_ready
    dev_bot.glofox_route_ready = glofox_route_ready
    dev_bot.level6_route_ready = level6_route_ready
    dev_bot.level6_calendar_route_ready = level6_calendar_route_ready
    dev_bot.level6_message_route_ready = level6_message_route_ready

    # Browser rights are shared too; actual use still requires this Bot's
    # allowlist + explicit Browser Host binding.
    dev_bot.browser_route_ready = lambda bot: _browser_ready(bots, bot)
    dev_bot.browser_bot_ready = lambda bot: _browser_ready(bots, bot)

    # Reuse all existing canonical parsing/submission code by presenting the
    # legacy executor-specific policy only at its old preflight seam.  The task
    # itself executes with the shared owner policy via serve.build_context.
    original_calendar_submit = dev_bot.submit_calendar_task
    original_writer_submit = dev_bot.submit_calendar_writer_task
    original_editor_submit = dev_bot.submit_calendar_editor_task
    original_glofox_submit = dev_bot.submit_glofox_task
    original_browser_submit = dev_bot.submit_browser_task
    original_l6_submit = dev_bot.submit_level6_task
    original_l6_calendar_submit = dev_bot.submit_level6_calendar_task
    original_l6_message_submit = getattr(dev_bot, "submit_level6_message_task", None)

    def _require_owner(user, bot):
        if not _same_owner(user, bot):
            raise dev_bot.DevBotError("Bot belongs to another owner — fail closed")
        if not _running_owned(bots, bot):
            raise dev_bot.DevBotError(
                f"bot {str((bot or {}).get('id') or '')!r} is not running")

    def submit_calendar_task(user, bot, task_text, **kwargs):
        _require_owner(user, bot)
        if not _calendar_read_available(bot.get("owner")):
            raise dev_bot.DevBotError("Google Calendar read is not connected for this owner")
        return original_calendar_submit(
            user, _clone_with_policy(bot, serve.calendar_reader_preset_policy()),
            task_text, **kwargs)

    def submit_calendar_writer_task(user, bot, task_text, **kwargs):
        _require_owner(user, bot)
        if not _calendar_write_available(bot.get("owner")):
            raise dev_bot.DevBotError("Google Calendar write is not connected for this owner")
        return original_writer_submit(
            user, _clone_with_policy(bot, serve.calendar_writer_preset_policy()),
            task_text, **kwargs)

    def submit_calendar_editor_task(user, bot, task_text, **kwargs):
        _require_owner(user, bot)
        if not _calendar_write_available(bot.get("owner")):
            raise dev_bot.DevBotError("Google Calendar write is not connected for this owner")
        return original_editor_submit(
            user, _clone_with_policy(bot, serve.calendar_editor_preset_policy()),
            task_text, **kwargs)

    def submit_glofox_task(user, bot, task_text, **kwargs):
        _require_owner(user, bot)
        return original_glofox_submit(
            user, _clone_with_policy(bot, serve.glofox_reader_preset_policy()),
            task_text, **kwargs)

    def submit_browser_task(user, bot, steps, **kwargs):
        _require_owner(user, bot)
        return original_browser_submit(
            user, _clone_with_policy(bot, serve.browser_preset_policy()),
            steps, **kwargs)

    def submit_level6_task(user, bot, task_text, **kwargs):
        _require_owner(user, bot)
        return original_l6_submit(
            user, _clone_with_policy(bot, serve.level6_weekly_preset_policy()),
            task_text, **kwargs)

    def submit_level6_calendar_task(user, bot, task_text, **kwargs):
        _require_owner(user, bot)
        return original_l6_calendar_submit(
            user, _clone_with_policy(bot, serve.level6_calendar_preset_policy()),
            task_text, **kwargs)

    dev_bot.submit_calendar_task = submit_calendar_task
    dev_bot.submit_calendar_writer_task = submit_calendar_writer_task
    dev_bot.submit_calendar_editor_task = submit_calendar_editor_task
    dev_bot.submit_glofox_task = submit_glofox_task
    dev_bot.submit_browser_task = submit_browser_task
    dev_bot.submit_level6_task = submit_level6_task
    dev_bot.submit_level6_calendar_task = submit_level6_calendar_task

    if callable(original_l6_message_submit):
        def submit_level6_message_task(user, bot, task_text, **kwargs):
            _require_owner(user, bot)
            # The legacy message route is part of the unified Calendar preset;
            # use that only for its old preflight. Runtime policy is shared.
            return original_l6_message_submit(
                user, _clone_with_policy(bot, serve.calendar_preset_policy()),
                task_text, **kwargs)
        dev_bot.submit_level6_message_task = submit_level6_message_task

    # ── Jev routing metadata is specialization, never stored policy.
    if jev_stream_router is not None:
        def candidate(_chat_service, bot: dict, *, metadata=None) -> dict:
            bot = bot or {}
            name = str(bot.get("name") or (metadata or {}).get("name") or "").strip()
            bot_id = str(bot.get("id") or (metadata or {}).get("id") or "").strip()
            return {
                "id": bot_id,
                "name": name,
                "role": "specialist",
                "description": (
                    f"{name or bot_id} is a user-named Kyrex routing specialization. "
                    "Connected tools and operation rights are owner-scoped and "
                    "independent of Bot specialization."
                ),
            }

        jev_stream_router._candidate = candidate
        # Initial cross-Bot routing remains a Chief-of-Staff specialization,
        # even though every Bot can perform bounded fallback delegation.
        jev_stream_router._is_coordinator = lambda _cs, bot: bool(re.search(
            r"\b(?:chief(?:\s+of\s+staff)?|coordinator)\b",
            " ".join((str((bot or {}).get("id") or ""),
                      str((bot or {}).get("name") or ""))).lower()))

    _installed = True
