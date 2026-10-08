"""Developer tasks belong to the target chat, with a concise coordinator relay."""
import json

import pytest
import bots
import chat_service
import delegation
import serve
from task_store import CloudTaskStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "tasks.db")
    monkeypatch.setattr(chat_service, "_task_store", lambda: store)
    monkeypatch.setattr(delegation, "CloudTaskStore", lambda: store)
    monkeypatch.setattr(chat_service, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    monkeypatch.setattr(serve, "_bot_llm_config", lambda bot: {"provider": "openai", "api_key": "x", "model": "m"})
    for bot_id, owner, policy in [("chief", "alice", {"fs:read": 0, "bot:delegate": 0}),
                                  ("dev", "alice", {"fs:read": 0, "fs:write": 1}),
                                  ("foreign", "bob", {"fs:read": 0})]:
        rift = tmp_path / bot_id
        rift.mkdir()
        bots.add_bot(bot_id, "Developer Bot" if bot_id == "dev" else bot_id,
                     "test:model", str(rift), owner=owner, policy=policy,
                     status="running", provider_profile_id="p")
    parent = chat_service.create_conversation("alice", bot_id="chief")
    session = object.__new__(chat_service.EngineSession)
    session.delegation_ctx = {"owner": "alice", "bot": bots.get_bot("chief"),
                              "conversation_id": parent["conversation_id"]}
    return store, session, parent


def submit(session, target="dev", text="Inspect the progress flow. Do not edit files."):
    ok, view = session._handle_delegation({"target_bot_id": target, "task": text})
    assert ok, view
    return view


def test_new_target_chat_contains_task_activity_and_full_result(setup):
    store, session, parent = setup
    view = submit(session)
    target_id = view["target_conversation_id"]
    task = store.get(view["task_id"])
    assert target_id != parent["conversation_id"]
    assert task["conversation_id"] == target_id
    assert task["bot_id"] == "dev"
    store.set_status(task["task_id"], "running")
    store.add_event(task["task_id"], "progress", {"stage": "Tracing progress events…", "args": "PRIVATE"})
    conv = chat_service.get_conversation("alice", target_id)
    assert conv["bot_id"] == "dev"
    assert conv["messages"][0]["role"] == "user"
    bubble = conv["messages"][-1]
    assert bubble["task"] == {"taskId": task["task_id"], "status": "running"}
    assert bubble["events"][-1]["payload"]["stage"] == "Tracing progress events…"
    assert "PRIVATE" not in json.dumps(conv)
    roster = chat_service.list_conversations("alice")
    assert next(c for c in roster if c["conversation_id"] == target_id)["activity"]["task_id"] == task["task_id"]
    # Other persistence paths must not save a projected empty live bubble.
    chat_service._write("alice", conv)
    raw = json.loads(chat_service._conv_path("alice", target_id).read_text())
    assert not any(m.get("delegated_active") for m in raw["messages"])
    full = "Recommendation: relay meaningful progress updates.\n\n" + "Full technical evidence. " * 90
    store.complete(task["task_id"], {"mode": "developer", "status": "completed", "final_response": full})
    sync = chat_service.sync_delegated_work("alice", parent["conversation_id"])
    result = chat_service.get_conversation("alice", target_id)["messages"][-1]
    assert result["id"] == bubble["id"]
    assert result["content"] == full.strip()
    assert result["developer_result"] is True
    assert "task" not in result
    notice = chat_service.get_conversation("alice", parent["conversation_id"])["messages"][-1]
    assert "Developer Bot finished" in notice["content"]
    assert "Full technical evidence" not in notice["content"]
    assert notice["delegation_result"]["conversation_id"] == target_id
    assert "events" not in notice
    assert sync["relayed"] and not chat_service.sync_delegated_work("alice", parent["conversation_id"])["relayed"]
    for _ in range(3):
        assert chat_service.get_conversation("alice", target_id)["messages"][-1] == result
    assert len(store.tasks_for_conversation(target_id, "alice")) == 1


def test_existing_bot_chat_is_reused_and_owner_scope_is_preserved(setup):
    store, session, parent = setup
    existing = chat_service.create_conversation("alice", bot_id="dev")
    first = submit(session)
    second = submit(session, text="Inspect the sidebar only.")
    assert first["target_conversation_id"] == second["target_conversation_id"] == existing["conversation_id"]
    assert len([c for c in chat_service.list_conversations("alice") if c.get("bot_id") == "dev"]) == 1
    assert chat_service.get_conversation("bob", existing["conversation_id"]) is None
    before = chat_service.list_conversations("alice")
    ok, result = session._handle_delegation({"target_bot_id": "foreign", "task": "Inspect files"})
    assert not ok
    assert chat_service.list_conversations("alice") == before
    assert not chat_service.sync_delegated_work("bob", parent["conversation_id"])["delegations"]
    rec = store.get_delegation(first["delegation_id"])
    assert "target_conversation_id" not in chat_service._delegation_view(store, {**rec, "owner": "bob"})


@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
def test_failure_and_cancellation_reach_both_chats_once(setup, outcome):
    store, session, parent = setup
    view = submit(session)
    if outcome == "failed":
        store.fail(view["task_id"], "Repository access was unavailable")
    else:
        store.cancel_effective(view["task_id"], reason="Stopped by owner")
    assert chat_service.sync_delegated_work("alice", parent["conversation_id"])["relayed"]
    assert not chat_service.sync_delegated_work("alice", parent["conversation_id"])["relayed"]
    messages = chat_service.get_conversation("alice", view["target_conversation_id"])["messages"]
    assert len([m for m in messages if m["role"] == "assistant"]) == 1
    assert outcome.split('ed')[0] in messages[-1]["content"].lower()


def test_target_approval_stays_owned_and_is_not_auto_answered(setup):
    store, session, parent = setup
    view = submit(session)
    store.set_status(view["task_id"], "running")
    store.persist_approval_request(view["task_id"], "dev", "msg", 2, "PRIVATE-TOKEN",
                                   "Review the edit", "Needs approval")
    conv = chat_service.get_conversation("alice", view["target_conversation_id"])
    assert conv["messages"][-1]["approval"]["task_id"] == view["task_id"]
    sync = chat_service.sync_delegated_work("alice", parent["conversation_id"])
    assert sync["delegations"][0]["status"] == "awaiting_approval"
    assert "PRIVATE-TOKEN" not in json.dumps([conv, sync])
    assert not store.get_pending_approval(view["task_id"]).get("operator_reply")
