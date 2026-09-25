"""Task-local policy overlay for owner-connected Calendar executors.

The production task worker is a separate process from Kyrex Chat.  It imports
``serve`` directly, so web-process routing shims cannot be the only place that
translates owner connector scopes into executor authority.

This module installs a narrow wrapper around ``serve.build_context``.  It never
mutates a Bot's stored policy.  For exactly one connected Calendar executor it
adds exactly one operation when the OWNER's live Google scope supports it:

* ``calendar``  -> ``cal:list`` tier 0
* ``cal_write`` -> ``cal:create`` tier 0 (writer still requires T1 approval)
* ``cal_edit``  -> ``cal:delete`` tier 2 (editor still requires T2 approval)

Repo/browser/default contexts are byte-for-byte untouched.  Connector methods
re-check OAuth scope at the provider call as the final authority.
"""
from __future__ import annotations

_installed = False


def calendar_read_available(owner: str) -> bool:
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


def calendar_write_available(owner: str) -> bool:
    owner = str(owner or "").strip()
    if not owner:
        return False
    try:
        import connectors
        return bool(connectors.default_store().calendar_write_available(owner))
    except Exception:
        return False


def install(serve) -> None:
    global _installed
    # A process can import this helper more than once; never stack wrappers.
    if _installed or getattr(serve, "_owner_connected_context_installed", False):
        _installed = True
        return

    original = serve.build_context

    def connected_build_context(session_key, executor_prefix="repo",
                                allow_bot_resolution=True):
        ctx = original(
            session_key, executor_prefix,
            allow_bot_resolution=allow_bot_resolution)
        owner = str(getattr(ctx, "bot_owner", "") or "").strip()
        policy = dict(getattr(ctx, "policy", {}) or {})
        if executor_prefix == "calendar" and calendar_read_available(owner):
            policy["cal:list"] = 0
        elif executor_prefix == "cal_write" and calendar_write_available(owner):
            policy["cal:create"] = 0
        elif executor_prefix == "cal_edit" and calendar_write_available(owner):
            policy["cal:delete"] = 2
        ctx.policy = policy
        return ctx

    serve.build_context = connected_build_context
    serve._owner_connected_context_installed = True
    _installed = True
