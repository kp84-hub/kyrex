"""Host jobs reject cross-source results before storage or Chat delivery."""
import asyncio
import json
import sqlite3

import pytest
import serve
import delegation
import device_messages
from job_contracts import (
    JobContractError, contract_for_task, validate_request_route, validate_result,
)
from task_store import CloudTaskStore, TaskWorker


@pytest.fixture
def store(tmp_path):
    value = CloudTaskStore(db_path=tmp_path / "tasks.db")
    yield value
    value.close()


def preview():
    return {
        "status": "no_changes", "mode": "level6_preview", "count": 6,
        "lines": [f"Day {i}: workout — trainer: Coach {i}" for i in range(6)],
        "final_response": "Preview only — nothing sent.\n#L6Workout\nSix workouts",
    }


def email(mode="search"):
    return {"status": "no_changes", "mode": mode, "count": 1,
            "query": "homecoming", "message_ids": ["email-1"],
            "final_response": "PRIVATE EMAIL CONTENT"}


def queue(store, text=serve.LEVEL6_MESSAGE_PREVIEW_REQUEST, prefix="level6"):
    return store.submit("calendar", text, executor_prefix=prefix,
                        resolve_bot=False, chat_id="alice")


def execute(store, executor, sent=None):
    task = store.claim_next("contract-test")
    worker = TaskWorker(store, executor=executor,
                        send=lambda cid, text: sent.append(text) if sent is not None else None)
    worker.execute_task(task)
    return store.get(task["task_id"])


@pytest.mark.parametrize("prompt", [
    "Prepare the #L6Workout message for this week and preview it for the Level 6 group chat. Don’t send yet.",
    "Draft the Level 6 workout message for the group chat",
    "Can you preview this week's Level 6 workout message?",
    "Show me a preview of the #L6Workout message",
    "#L6Workout preview",
])
def test_preview_intent_rejects_email_and_delivery_before_submission(store, prompt):
    for prefix, command in [
        ("gmail", "gmail: search Level 6"),
        ("level6", serve.LEVEL6_MESSAGE_REQUEST),
        ("level6", serve.LEVEL6_CALENDAR_BATCH_REQUEST),
        ("repo", prompt),
    ]:
        with pytest.raises(JobContractError):
            store.submit("calendar", command, executor_prefix=prefix,
                         request_text=prompt, resolve_bot=False)
    assert store.list_tasks() == []


def test_contract_survives_store_restart_and_contains_no_send_authority(tmp_path):
    path = tmp_path / "durable.db"
    store = CloudTaskStore(db_path=path)
    task_id = queue(store)
    expected = store.get(task_id)["job_contract"]
    store.close()
    with_store = CloudTaskStore(db_path=path)
    try:
        assert with_store.get(task_id)["job_contract"] == expected
        assert expected["operation"] == "preview"
        assert expected["result_kind"] == "workout_preview"
        assert not any("send" in action or "write" in action for action in expected["allowed_actions"])
        assert len(expected["request_sha256"]) == 64
    finally:
        with_store.close()


@pytest.mark.parametrize("bad_result", [
    email(),
    dict(preview(), lines=["one"], count=1),
    dict(preview(), mode="level6_delivery"),
    dict(preview(), final_response="Sent to the group"),
    dict(preview(), count=True),
])
def test_worker_rejects_bad_results_even_if_callback_error_is_swallowed(store, bad_result):
    task_id = queue(store)
    sent = []
    def executor(**kw):
        kw["send"]("alice", "UNVALIDATED PRIVATE RESPONSE")
        kw["edit"]("alice", "id", "UNVALIDATED EDIT")
        try:
            kw["on_result"](bad_result)
        except JobContractError:
            pass
        kw["send"]("alice", "PRIVATE RESPONSE AFTER CALLBACK FAILURE")
    task = execute(store, executor, sent)
    assert task["status"] == "failed"
    assert task["result"] is None
    assert "PRIVATE" not in task["error"]
    assert sent == []
    assert not any(e["type"] in {"result", "message", "edit"} for e in store.get_events(task_id))


def test_valid_preview_publishes_only_validated_response_and_copies_result(store):
    task_id = queue(store)
    result = preview()
    sent = []
    def executor(**kw):
        kw["send"]("alice", "UNRELATED EMAIL BEFORE RESULT")
        kw["on_result"](result)
        result["lines"].clear()  # no mutable reference survives validation
        kw["send"]("alice", "UNRELATED EMAIL AFTER RESULT")
    task = execute(store, executor, sent)
    assert task["status"] == "done"
    assert task["result"]["count"] == len(task["result"]["lines"]) == 6
    assert sent == [task["result"]["final_response"]]
    assert "UNRELATED" not in json.dumps(store.get_events(task_id))


@pytest.mark.parametrize("corruption", ["command", "executor", "contract"])
def test_altered_queued_job_never_runs_executor(store, corruption):
    task_id = queue(store)
    if corruption == "command":
        store._conn.execute("UPDATE tasks SET task_text=? WHERE task_id=?",
                            (serve.LEVEL6_MESSAGE_REQUEST, task_id))
    elif corruption == "executor":
        store._conn.execute("UPDATE tasks SET executor_prefix='repo' WHERE task_id=?", (task_id,))
    else:
        store._conn.execute("UPDATE tasks SET job_contract='broken json' WHERE task_id=?", (task_id,))
    store._conn.commit()
    task = execute(store, lambda **kw: pytest.fail("Corrupted job executed"))
    assert task["status"] == "failed"
    assert task["result"] is None


def test_read_job_cannot_enter_approval_flow(store):
    task_id = queue(store)
    def executor(**kw):
        try:
            kw["on_approval"]("id", 1, "token", "Send text", {})
        except JobContractError:
            pass
        kw["on_result"](preview())
    assert execute(store, executor)["status"] == "failed"
    assert store.pending_approvals_for_task(task_id) == []


def test_complete_guard_rejects_bypass_and_cancel_still_wins(store):
    task_id = queue(store)
    store.claim_next("test")
    assert store.complete(task_id, email()) == "failed"
    assert store.get(task_id)["result"] is None
    next_id = queue(store)
    store.claim_next("test")
    store.request_cancel(next_id)
    assert store.complete(next_id, email()) == "cancelled"


def test_cancellation_wins_after_worker_captures_result(store):
    task_id = queue(store)
    sent = []
    def executor(**kw):
        kw["on_result"](preview())
        store.request_cancel(task_id)
    assert execute(store, executor, sent)["status"] == "cancelled"
    assert sent == []


def test_finalization_rechecks_cancellation_before_any_result_event(store, monkeypatch):
    task_id = queue(store)
    sent = []
    complete = store.complete
    def cancel_then_complete(task_id, result, **kwargs):
        store.request_cancel(task_id)
        return complete(task_id, result, **kwargs)
    monkeypatch.setattr(store, "complete", cancel_then_complete)
    task = execute(store, lambda **kw: kw["on_result"](preview()), sent)
    assert task["status"] == "cancelled"
    assert sent == []
    assert not any(e["type"] == "result" for e in store.get_events(task_id))


def test_finalization_rechecks_route_after_executor_returns(store):
    task_id = queue(store)
    sent = []
    def executor(**kw):
        kw["on_result"](preview())
        store._conn.execute("UPDATE tasks SET task_text=? WHERE task_id=?",
                            (serve.LEVEL6_MESSAGE_REQUEST, task_id))
        store._conn.commit()
    task = execute(store, executor, sent)
    assert task["status"] == "failed"
    assert task["result"] is None
    assert sent == []
    assert not any(e["type"] == "result" for e in store.get_events(task_id))


@pytest.mark.parametrize("prefix,command,result", [
    ("gmail", "gmail: search homecoming", email()),
    ("gmail", "gmail: read id email-1", email("read")),
    ("calendar", "calendar: week", {"status": "no_changes", "mode": "calendar",
                                    "count": 0, "window": "week", "final_response": "No events"}),
    ("level6", "calendar", dict(preview(), mode="level6_calendar")),
    ("level6", "weekly", dict(preview(), mode="level6_weekly")),
])
def test_each_supported_read_keeps_its_own_result_kind(store, prefix, command, result):
    queue(store, command, prefix)
    task = execute(store, lambda **kw: kw["on_result"](result))
    assert task["status"] == "done"
    assert task["result"] == result


@pytest.mark.parametrize("prefix,command,mode", [
    ("gmail", "gmail: search", "search"),
    ("calendar", "calendar: week", "calendar"),
    ("level6", "calendar", "level6_calendar"),
])
def test_unavailable_connector_is_explicit_and_cannot_contain_success_items(prefix, command, mode):
    contract = contract_for_task(prefix, command)
    result = {"status": "no_changes", "mode": mode, "count": 0,
              "outcome": "unavailable", "final_response": "Connect the source in Settings."}
    validate_result(contract, result)
    with pytest.raises(JobContractError):
        validate_result(contract, dict(result, count=1))


def test_email_search_cannot_satisfy_full_read_or_calendar_job():
    with pytest.raises(JobContractError):
        validate_request_route("gmail: read id email-1", "gmail", "gmail: search homecoming")
    with pytest.raises(JobContractError):
        validate_result(contract_for_task("calendar", "calendar: week"), email())



def test_explicit_email_selection_is_not_replaced_by_another_message():
    with pytest.raises(JobContractError):
        validate_request_route("gmail: read id email-1", "gmail", "gmail: read id email-2")


def test_installed_mail_adapter_preserves_full_body_read_when_model_suggests_search():
    from types import SimpleNamespace
    import mail_routing_bridge
    command = mail_routing_bridge.bounded_gmail_command(
        SimpleNamespace(serve=serve), "gmail: search homecoming",
        "Read my email about homecoming in full",
        hint={"selected_bot_id": "email", "selected_bot_name": "Email Bot"})
    assert command.startswith("gmail: read ")


def test_delegation_keeps_original_preview_intent_if_router_regresses(store, monkeypatch):
    chief = {"id": "chief", "owner": "alice", "status": "running", "policy": serve.COORDINATOR_PRESET}
    target = {"id": "calendar", "owner": "alice", "status": "running", "policy": serve.CALENDAR_PRESET}
    monkeypatch.setattr(delegation, "resolve_connected_tool_target", lambda *a: target)
    monkeypatch.setattr(delegation, "_delegated_level6_message", lambda *a: None)
    monkeypatch.setattr(delegation, "_delegated_gmail_command", lambda *a: "gmail: search Level 6")
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", chief, "calendar", "#L6Workout preview", store=store)
    assert store.list_delegations(owner="alice") == []



def test_installed_calendar_adapter_retains_original_preview_intent(store, monkeypatch):
    import shared_connected_tools as connected
    import dev_bot
    chief = {"id": "chief", "owner": "alice", "status": "running", "policy": serve.COORDINATOR_PRESET}
    target = {"id": "calendar", "owner": "alice", "status": "running", "policy": serve.CALENDAR_PRESET}
    monkeypatch.setattr(delegation, "resolve_connected_tool_target", lambda *a: target)
    monkeypatch.setattr(connected, "_delegated_calendar_payload", lambda *a, **kw: ("calendar", "calendar: week"))
    with pytest.raises(delegation.DelegationError):
        connected._submit_delegation_connected(
            lambda *a, **kw: pytest.fail("Wrong route fell back"),
            delegation, dev_bot, "alice", chief, "calendar", "#L6Workout preview", store=store)
    assert store.list_delegations(owner="alice") == []


def test_installed_mail_adapter_cannot_claim_workout_preview():
    from types import SimpleNamespace
    import mail_routing_bridge
    chat = SimpleNamespace(serve=serve)
    hint = {"selected_bot_id": "email", "selected_bot_name": "Email Bot"}
    assert mail_routing_bridge.bounded_gmail_command(
        chat, "gmail: search workouts", "#L6Workout preview", hint=hint) is None


def test_routed_mail_submission_rechecks_original_intent_before_creating_records(store, monkeypatch):
    from types import SimpleNamespace
    import jev_stream_router
    import dev_bot
    monkeypatch.setattr(jev_stream_router, "_gmail_command_for_routed_turn",
                        lambda *a, **kw: "gmail: search workouts")
    chat = SimpleNamespace(_task_store=lambda: store, serve=serve, delegation=delegation)
    session = SimpleNamespace(delegation_ctx={"owner": "alice", "bot": {"id": "chief"}})
    ok, result = jev_stream_router._submit_routed_gmail(
        chat, dev_bot, session, {"task": "gmail: search workouts"},
        {"request_text": "#L6Workout preview", "selected_bot_id": "email"})
    assert not ok and "error" in result
    assert store.list_delegations(owner="alice") == []


def test_direct_chat_cannot_discard_preview_intent_in_a_fixed_calendar_mode(store, monkeypatch):
    import chat_service as chat
    monkeypatch.setattr(chat, "_task_store", lambda: store)
    monkeypatch.setattr(chat.dev_bot, "submit_level6_calendar_task",
                        lambda *a, **kw: pytest.fail("Wrong route submitted"))
    async def run():
        return [frame async for frame in chat._stream_writable_bot_task(
            "alice", {"messages": []}, {}, "#L6Workout preview", "conv", asyncio.Event(),
            mode="level6_calendar")]
    with pytest.raises(chat.ChatUnavailable):
        asyncio.run(run())


@pytest.mark.parametrize("phrase", [
    "Show my recent texts with Ethan The Neighbor.",
    "Show my recent texts from Ethan The Neighbor.",
    "Could you show me my recent text messages with Ethan The Neighbor?",
    "Please read my texts from Ethan The Neighbor",
])
def test_contact_read_wording_has_same_route(phrase):
    assert device_messages.read_command(phrase) == "Ethan The Neighbor"


def test_migration_preserves_existing_task_and_derives_legacy_contract(tmp_path):
    path = tmp_path / "legacy.db"
    original = CloudTaskStore(db_path=path)
    task_id = queue(original)
    original.close()
    # Simulate the schema immediately before this change.
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE tasks DROP COLUMN job_contract")
    migrated = CloudTaskStore(db_path=path)
    try:
        assert migrated.get(task_id)["job_contract"] is None
        assert execute(migrated, lambda **kw: kw["on_result"](preview()))["status"] == "done"
    finally:
        migrated.close()
