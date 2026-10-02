"""Browser source verification must not be rewritten onto a mail target."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent
for path in (_BACKEND, _BACKEND.parents[1], _BACKEND.parents[2] / "kyrex_engine"):
    sys.path.insert(0, str(path))

import dev_bot
import jev_stream_router as jev
import mail_routing_bridge as mail
import serve


TASK = json.dumps({"actions": [
    {"action": "navigate", "url": "https://agroecologyeducationfarm.wordpress.ncsu.edu/"},
    {"action": "read"},
]})


def _chat(browser):
    return SimpleNamespace(
        serve=SimpleNamespace(is_browser_bot_policy=serve.is_browser_bot_policy),
        bots=SimpleNamespace(
            load_bots=lambda: {"browser": browser},
            is_running=lambda bot: bot.get("status") == "running",
        ),
    )


def _browser(**overrides):
    return {"id": "browser", "owner": "alice", "status": "running",
            "policy": serve.browser_preset_policy(), **overrides}


@pytest.mark.parametrize("selected", ["chief", "email-bot", "calendar-bot"])
@pytest.mark.parametrize("consumed", [False, True])
def test_browser_subtask_survives_both_wrappers(selected, consumed, monkeypatch):
    calls = []

    class Engine:
        def _handle_delegation(self, frame):
            calls.append(dict(frame))
            return True, dict(frame)

    async def stream(*args, **kwargs):
        yield {}

    chat = _chat(_browser())
    chat.stream_chat = stream
    chat.EngineSession = Engine
    chat.serve._gmail_query_from = lambda text: text
    service = SimpleNamespace(
        is_writable_bot_policy=lambda policy: False,
        browser_route_ready=lambda bot: False,
        gmail_route_ready=lambda bot: False,
        email_calendar_route_ready=lambda bot: False,
        validate_browser_steps=dev_bot.validate_browser_steps,
    )

    def wrong_route(*args, **kwargs):
        raise AssertionError("Browser subtask reached the Gmail/Calendar adapter")

    monkeypatch.setattr(jev, "_installed", False)
    monkeypatch.setattr(mail, "_installed", False)
    monkeypatch.setattr(jev, "_submit_routed_gmail", wrong_route)
    monkeypatch.setattr(jev, "_submit_routed_email_calendar", wrong_route)
    monkeypatch.setattr(jev, "_gmail_command_for_routed_turn", lambda *a, **k: None)
    # install() wraps this module global too; restore it between test cases.
    monkeypatch.setattr(jev, "_persist_delegated_gmail_results",
                        jev._persist_delegated_gmail_results)
    jev.install(chat, service)
    mail.install(chat, service, jev, chat.serve)
    session = Engine()
    session.delegation_ctx = {"owner": "alice", "conversation_id": "c1"}
    session._jev_bot_target_hint = {
        "coordinator_bot_id": "chief", "selected_bot_id": selected,
        "request_text": "Find an address for this NCSU Agroecology Farm",
        "consumed": consumed,
    }
    frame = {"target_bot_id": "browser", "task": TASK}
    assert session._handle_delegation(frame) == (True, frame)
    assert calls == [frame]
    assert session._jev_bot_target_hint["consumed"] is True


@pytest.mark.parametrize("overrides,task", [
    ({"owner": "bob"}, TASK),
    ({"status": "stopped"}, TASK),
    ({"policy": {"fs:read": 0}}, TASK),
    ({}, "Find an address"),
    ({}, '{"actions":[{"action":"click","selector":"button"}]}'),
    ({}, '{"actions":[{"action":"navigate","url":"file:///etc/passwd"}]}'),
    ({}, '{"actions":[]}'),
])
def test_browser_exception_requires_owned_running_read_only_plan(overrides, task):
    session = SimpleNamespace(delegation_ctx={"owner": "alice"})
    assert not jev._is_explicit_browser_subtask(
        _chat(_browser(**overrides)), dev_bot, session,
        {"target_bot_id": "browser", "task": task})
