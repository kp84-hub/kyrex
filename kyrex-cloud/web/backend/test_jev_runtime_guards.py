"""Runtime regressions found by the live Jev routing smoke test."""

from __future__ import annotations

import contextvars
import sys
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_CLOUD = _HERE.parents[1]
_REPO = _HERE.parents[2]
_ENGINE = _REPO / "kyrex_engine"
for _path in (str(_HERE), str(_CLOUD), str(_ENGINE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import jev_stream_router  # noqa: E402
import serve  # noqa: E402


def test_contextvar_reset_from_different_context_is_teardown_safe():
    """Match Starlette's async-generator finalizer ContextVar failure exactly."""
    var = contextvars.ContextVar("jev-test", default=None)
    token = var.set("request-value")

    # A token can only be reset in the Context that created it. Starlette may
    # close an abandoned async generator from async_generator_athrow in another
    # Context; the routing wrapper must not turn that teardown into an
    # unhandled Task exception.
    other = contextvars.Context()
    assert other.run(jev_stream_router._reset_context_token, var, token) is False

    # The originating Context is untouched by the failed foreign reset and can
    # still clean itself normally.
    assert var.get() == "request-value"
    assert jev_stream_router._reset_context_token(var, token) is True
    assert var.get() is None


def test_recognized_gmail_route_never_falls_through_to_repo_when_unready():
    """A positive Gmail parse + failed readiness is a tool error, never repo."""
    target = {
        "id": "email-bot",
        "name": "Email Bot",
        "owner": "alice",
        "status": "running",
        "rift": "",
        "policy": {},
    }
    bots_module = SimpleNamespace(
        load_bots=lambda: {"email-bot": target},
        is_running=lambda bot: bot.get("status") == "running",
    )
    chat_service = SimpleNamespace(
        serve=serve,
        bots=bots_module,
        # These must never be touched: readiness fails before a task/delegation
        # row, and the caller must receive a connected-tool error instead of
        # falling into generic repo delegation.
        _task_store=lambda: (_ for _ in ()).throw(
            AssertionError("must not create a task when Gmail is unready")),
    )
    dev_bot = SimpleNamespace(gmail_route_ready=lambda bot: False)
    session = SimpleNamespace(delegation_ctx={
        "owner": "alice",
        "bot": {"id": "chief"},
        "conversation_id": "c1",
    })
    hint = {
        "coordinator_bot_id": "chief",
        "selected_bot_id": "email-bot",
        "selected_bot_name": "Email Bot",
        "selected_bot_role": "custom",
        "selected_bot_description": "Handles email information lookups.",
        "request_text": "Find the details for the 4th grade field trip in October 2",
    }

    result = jev_stream_router._submit_routed_gmail(
        chat_service,
        dev_bot,
        session,
        {
            "target_bot_id": "email-bot",
            "task": "Find the details for the 4th grade field trip in October 2",
        },
        hint,
    )

    assert result is not None
    ok, payload = result
    assert ok is False
    assert "Gmail read is unavailable" in payload["error"]
    assert "repo executor" in payload["error"]


def test_non_mail_peer_request_still_returns_none_for_normal_delegation():
    """The guard must not steal arbitrary Bot-to-Bot/repo work."""
    chat_service = SimpleNamespace(serve=serve)
    hint = {
        "selected_bot_id": "qa-bot",
        "selected_bot_name": "QA Bot",
        "selected_bot_role": "qa",
        "selected_bot_description": "Reviews application behavior.",
        "request_text": "Review the parser tests",
    }

    command = jev_stream_router._gmail_command_for_routed_turn(
        chat_service,
        "Review the parser tests",
        hint["request_text"],
        hint=hint,
    )

    assert command is None
