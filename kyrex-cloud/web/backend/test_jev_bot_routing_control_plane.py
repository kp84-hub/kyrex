"""Regression coverage for Jev-as-router / Kyrex-as-reasoner Chat control plane."""

from __future__ import annotations

import json
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

import jev_routing  # noqa: E402
import jev_stream_router  # noqa: E402
import serve  # noqa: E402


class FakeBotClient:
    def __init__(self, target="email-bot", confidence=0.96):
        self.target = target
        self.confidence = confidence
        self.calls = []

    def decide(self, state, questions):
        self.calls.append((state, questions))
        return {
            "model": "jev-test",
            "answers": {
                "target": {
                    "type": "choice",
                    "choice": self.target,
                    "probabilities": {self.target: 1.0},
                    "confidence": self.confidence,
                },
            },
            "usage": {},
        }


def _candidates():
    return [
        {
            "id": "chief",
            "name": "Chief of Staff",
            "role": "chief-of-staff",
            "description": "Coordinates other Bots.",
        },
        {
            "id": "email-bot",
            "name": "Email Bot",
            "role": "custom",
            "description": "Handles email information lookups.",
        },
    ]


def test_bot_decision_is_routing_only_and_shared_tools_are_not_permissions(
        tmp_path, monkeypatch):
    log = tmp_path / "jev.jsonl"
    monkeypatch.setenv("KYREX_JEV_ROUTING_LOG", str(log))
    request = "Find the details for the 4th grade field trip in October"
    client = FakeBotClient()

    result = jev_routing.decide_bot_target(
        request,
        _candidates(),
        "chief",
        shared_tools=["gmail_read", "calendar_write"],
        client=client,
        enabled=True,
    )

    assert result["selected_bot_id"] == "email-bot"
    assert result["source"] == "jev"
    state, questions = client.calls[0]
    assert state["routing_only"] is True
    assert state["shared_tools"] == ["calendar_write", "gmail_read"]
    assert state["available_bot_ids"] == ["chief", "email-bot"]
    instructions = questions["target"]["instructions"]
    assert "ROUTING ONLY" in instructions
    assert "NOT a permission boundary" in instructions
    stored = log.read_text(encoding="utf-8")
    assert request not in stored
    rec = json.loads(stored)
    assert rec["kind"] == "bot_route"
    assert rec["selected_bot_id"] == "email-bot"


def test_low_confidence_bot_decision_falls_back_to_current_bot(
        tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_JEV_ROUTING_LOG", str(tmp_path / "jev.jsonl"))
    result = jev_routing.decide_bot_target(
        "find a school email",
        _candidates(),
        "chief",
        shared_tools=["gmail_read"],
        client=FakeBotClient(confidence=0.20),
        enabled=True,
    )
    assert result["selected_bot_id"] == "chief"
    assert result["source"] == "deterministic"
    assert result["reason"] == "low_confidence"
    assert result["jev_bot_id"] == "email-bot"


def test_running_shared_tool_bot_remains_a_jev_candidate_without_rift():
    chief = {
        "id": "chief", "name": "Chief of Staff", "owner": "alice",
        "status": "running", "policy": {"bot:delegate": 0},
    }
    email_bot = {
        "id": "email-bot", "name": "Email Bot", "owner": "alice",
        "status": "running", "policy": {}, "rift": "",
    }
    # The legacy delegation roster reports available=False when the repo Rift
    # does not resolve. That MUST NOT remove the Bot from Jev's routing choices
    # because owner-scoped Gmail/Calendar work does not need a repo workspace.
    delegation = SimpleNamespace(
        visible_targets=lambda owner, exclude_bot_id=None: [{
            "id": "email-bot",
            "name": "Email Bot",
            "status": "running",
            "role": "worker",
            "capabilities": [],
            "model": "",
            "available": False,
        }],
    )
    bots_module = SimpleNamespace(
        load_bots=lambda: {"chief": chief, "email-bot": email_bot},
        is_running=lambda bot: bot.get("status") == "running",
    )
    chat_service = SimpleNamespace(delegation=delegation, bots=bots_module)

    candidates = jev_stream_router._routing_candidates(
        chat_service, "alice", chief)

    assert [c["id"] for c in candidates] == ["chief", "email-bot"]


def test_routed_email_request_still_uses_kyrex_deterministic_gmail_parser():
    chat_service = SimpleNamespace(serve=serve)
    original = "Find the details for the 4th grade field trip in October"
    hint = {
        "selected_bot_id": "email-bot",
        "selected_bot_name": "Email Bot",
        "selected_bot_description": "Handles email information lookups.",
    }

    command = jev_stream_router._gmail_command_for_routed_turn(
        chat_service,
        original,
        original,
        hint=hint,
    )

    assert command is not None
    assert command.startswith("gmail: read ")
    assert "4th" in command.lower()
    assert "field" in command.lower()
    assert "trip" in command.lower()


class FakeStore:
    def __init__(self):
        self.created = []
        self.statuses = []
        self.records = {}
        self.tasks = {}

    def create_delegation(self, **kwargs):
        did = "d1"
        rec = {"delegation_id": did, **kwargs, "task_id": None}
        self.created.append(dict(kwargs))
        self.records[did] = rec
        return did

    def set_delegation_status(self, delegation_id, status, **kwargs):
        self.statuses.append((delegation_id, status, dict(kwargs)))
        rec = self.records.setdefault(delegation_id, {"delegation_id": delegation_id})
        rec["status"] = status
        rec.update(kwargs)

    def get_delegation(self, delegation_id):
        rec = self.records.get(delegation_id)
        return dict(rec) if rec else None

    def get(self, task_id):
        return self.tasks.get(task_id)


def test_jev_routed_mail_delegation_uses_shared_gmail_executor_without_rift():
    store = FakeStore()
    target = {
        "id": "email-bot",
        "owner": "alice",
        "status": "running",
        "rift": "",
        "policy": {},
    }
    delegated = SimpleNamespace(
        public_view=lambda rec: dict(rec),
    )
    bots_module = SimpleNamespace(
        load_bots=lambda: {"email-bot": target},
        is_running=lambda bot: bot.get("status") == "running",
    )
    chat_service = SimpleNamespace(
        serve=serve,
        delegation=delegated,
        bots=bots_module,
        _task_store=lambda: store,
    )
    submissions = []

    def submit_gmail_task(user, bot, task_text, store=None,
                          conversation_id=None):
        submissions.append({
            "user": user,
            "bot": bot["id"],
            "task_text": task_text,
            "executor_prefix": "gmail",
            "conversation_id": conversation_id,
        })
        return "t1"

    dev_bot = SimpleNamespace(
        gmail_route_ready=lambda bot: True,
        submit_gmail_task=submit_gmail_task,
    )
    session = SimpleNamespace(delegation_ctx={
        "owner": "alice",
        "bot": {"id": "chief"},
        "conversation_id": "c1",
    })
    hint = {
        "selected_bot_id": "email-bot",
        "selected_bot_name": "Email Bot",
        "request_text": "Find the details for the 4th grade field trip in October",
    }

    ok, result = jev_stream_router._submit_routed_gmail(
        chat_service,
        dev_bot,
        session,
        {"target_bot_id": "email-bot",
         "task": "Find the details for the 4th grade field trip in October"},
        hint,
    )

    assert ok is True
    assert result["delegation_id"] == "d1"
    assert store.created[0]["executor_prefix"] == "gmail"
    assert store.created[0]["task_text"].startswith("gmail: read ")
    assert submissions[0]["executor_prefix"] == "gmail"
    assert submissions[0]["bot"] == "email-bot"
    assert submissions[0]["task_text"] == store.created[0]["task_text"]
    assert store.created[0]["executor_prefix"] != "repo"


def test_first_delegation_target_survives_engine_thread_boundary_and_task_is_unchanged(
        monkeypatch):
    monkeypatch.setattr(jev_stream_router, "_installed", False)
    original_calls = []

    class FakeEngineSession:
        delegation_ctx = None

        def _handle_delegation(self, frame):
            original_calls.append(dict(frame))
            return True, {"target_bot_id": frame.get("target_bot_id"),
                          "task": frame.get("task")}

    target = {"id": "email-bot", "owner": "alice", "status": "running"}
    delegation = SimpleNamespace(
        resolve_delegation_target=lambda owner, bot_id: target,
    )
    serve_stub = SimpleNamespace(
        DEVELOPER_PRESET={"fs:write": 1},
        is_browser_bot_policy=lambda policy: False,
        coordinator_granted=lambda bot: True,
    )

    async def original_stream(*args, **kwargs):
        yield {"type": "status", "status": "complete", "content": "ok"}

    def original_get_engine_session(*args, **kwargs):
        return FakeEngineSession()

    chat_service = SimpleNamespace(
        stream_chat=original_stream,
        get_conversation=lambda user, cid: {"bot_id": "chief"},
        resolve_bot_for_user=lambda user, bid: {
            "id": "chief", "owner": "alice", "policy": {}},
        serve=serve_stub,
        delegation=delegation,
        EngineSession=FakeEngineSession,
        _get_engine_session=original_get_engine_session,
        _task_store=lambda: FakeStore(),
    )
    dev_bot = SimpleNamespace(
        is_writable_bot_policy=lambda policy: False,
        browser_route_ready=lambda bot: False,
        gmail_route_ready=lambda bot: False,
        email_calendar_route_ready=lambda bot: False,
    )

    jev_stream_router.install(chat_service, dev_bot)
    hint = {
        "coordinator_bot_id": "chief",
        "selected_bot_id": "email-bot",
        "shared_tools": [],
        "request_text": "find the field trip",
        "consumed": False,
    }

    token = jev_stream_router._bot_target_hint.set(hint)
    try:
        sess = chat_service._get_engine_session("alice", "c1")
    finally:
        jev_stream_router._bot_target_hint.reset(token)

    assert jev_stream_router._bot_target_hint.get() is None
    assert sess._jev_bot_target_hint["selected_bot_id"] == "email-bot"
    sess.delegation_ctx = {
        "owner": "alice", "bot": {"id": "chief"},
        "conversation_id": "c1",
    }
    task = "Please investigate the fourth grade field trip and report back."
    ok, result = sess._handle_delegation({
        "target_bot_id": "calendar",
        "task": task,
    })

    assert ok is True
    assert original_calls[0]["target_bot_id"] == "email-bot"
    assert original_calls[0]["task"] == task
    assert result["task"] == task
    assert sess._jev_bot_target_hint["consumed"] is True


def test_completed_delegated_gmail_result_restores_parent_selected_email_state():
    store = FakeStore()
    store.records["d1"] = {
        "delegation_id": "d1",
        "owner": "alice",
        "executor_prefix": "gmail",
        "task_id": "t1",
    }
    store.tasks["t1"] = {
        "task_id": "t1",
        "result": json.dumps({
            "status": "no_changes",
            "mode": "read",
            "query": "4th grade field trip Oct",
            "message_ids": ["m1"],
            "selected": {
                "id": "m1",
                "headers": {"Subject": "Bulldog Bulletin"},
                "enriched_facts": {
                    "title": "4th Grade Field Trip",
                    "date": "2026-10-02",
                },
            },
        }),
    }
    remembered = []
    delegated = SimpleNamespace(
        fetch_delegation=lambda owner, did, store=None: store.records.get(did),
    )
    chat_service = SimpleNamespace(
        delegation=delegated,
        _task_store=lambda: store,
        _remember_gmail_page=lambda user, cid, result: remembered.append(
            (user, cid, result)),
    )

    jev_stream_router._persist_delegated_gmail_results(
        chat_service,
        "alice",
        "c1",
        {"delegations": [{"delegation_id": "d1", "status": "done"}]},
    )

    assert len(remembered) == 1
    user, cid, result = remembered[0]
    assert (user, cid) == ("alice", "c1")
    assert result["selected"]["id"] == "m1"
    assert result["selected"]["enriched_facts"]["date"] == "2026-10-02"
