"""Offline durable developer updates survive polling/reload without a rerun."""
import asyncio
import json
import os

os.environ.setdefault("WEB_SESSION_SECRET", "progress-tests-only")

import chat_service
import chat_api
import git_workflow
import threading
from task_store import CloudTaskStore
from developer_updates import public_progress


def test_real_tool_stages_are_bounded_and_honest(monkeypatch):
    events = []
    clock = [1.0]
    monkeypatch.setattr(git_workflow.time, "monotonic", lambda: clock[0])
    monkeypatch.setenv("KYREX_API_KEY", "never-display-this-token")
    relay = git_workflow.developer_progress(events.append)
    relay({"type": "reasoning", "content": "private reasoning"})
    relay({"type": "tool_start", "name": "read_local_file", "args": {"path": "/private/key"}})
    relay({"type": "tool_start", "name": "run_command", "args": {"command": "pytest"}})
    for i in range(20):
        clock[0] += 4
        relay({"type": "token", "content": f"Update {i}: api_key=never-display-this-token"})
        relay({"type": "tool_start", "name": "run_command", "args": {"command": "pytest private-path"}})
    relay({"type": "tool_result", "name": "run_command", "result": {"exit_code": 1, "stdout": "SECRET OUTPUT"}})
    relay({"type": "token", "content": "Final answer is separate."})
    relay({"type": "tool_start", "name": "task_complete"})
    relay({"type": "chat_done"})
    commentary = [e["content"] for e in events if e["type"] == "commentary"]
    stages = [e["payload"]["stage"] for e in events if e["type"] == "progress"]
    assert len(commentary) == 20  # later findings still appear on a long run
    assert stages[0] == "Inspecting the relevant files…"
    assert "Running checks…" in stages
    assert stages[-1] == "A tool failed; checking how to proceed."
    assert not any("passed" in stage.lower() for stage in stages)
    assert not any(secret in str(stages + commentary) for secret in (
        "never-display-this-token", "private reasoning", "SECRET OUTPUT", "/private/key", "Final answer"))
    assert public_progress([{"stage": "<invoke>raw tool"}, {"stage": "```raw"}]) == []


def setup_task(tmp_path, monkeypatch, *, delegated=False, owner="owner"):
    store = CloudTaskStore(db_path=tmp_path / "tasks.db")
    monkeypatch.setattr(chat_service, "_task_store", lambda: store)
    monkeypatch.setattr(chat_service, "_user_dir", lambda user: tmp_path)
    conv = chat_service.create_conversation(owner)
    did = store.create_delegation(owner=owner, coordinator_bot_id="overwatcher",
        target_bot_id="dev", task_text="Fix the parser", executor_prefix="developer",
        parent_conversation_id=conv["conversation_id"]) if delegated else None
    tid = store.submit(session_key="dev", task_text="Fix the parser", chat_id=owner,
        executor_prefix="developer", conversation_id=conv["conversation_id"], parent_delegation_id=did)
    if did:
        store.set_delegation_status(did, "running", task_id=tid)
    store.add_event(tid, "progress", {"stage": "Updating the code…", "api_key": "PRIVATE", "args": "PRIVATE"})
    store.add_event(tid, "progress", {"stage": "Running checks…"})
    return store, conv, tid, did


def test_delegation_polls_only_linked_owned_progress(tmp_path, monkeypatch):
    store, conv, tid, did = setup_task(tmp_path, monkeypatch, delegated=True)
    rec = store.get_delegation(did)
    view = chat_service._delegation_view(store, rec)
    assert [n["stage"] for n in view["progress"]] == ["Updating the code…", "Running checks…"]
    assert "PRIVATE" not in json.dumps(view)
    status_view = chat_service.coordinator_delegation_statuses("owner", "overwatcher", conv["conversation_id"])[0]
    assert status_view["progress_update"] == "Running checks…"
    assert "progress" not in status_view, "Do not replay the full activity history to the model"
    assert chat_service.coordinator_delegation_statuses("owner", "wrong-coordinator", conv["conversation_id"]) == []
    assert chat_service.sync_delegated_work("someone-else", conv["conversation_id"])["delegations"] == []
    assert "progress" not in chat_service._delegation_view(store, {**rec, "owner": "someone-else"})
    assert "progress" not in chat_service._delegation_view(store, {**rec, "delegation_id": "wrong-link"})
    store.add_event(tid, "progress", {"stage": "A tool failed; checking how to proceed."})
    assert chat_service.sync_delegated_work("owner", conv["conversation_id"])["delegations"][0]["progress"][-1]["stage"].startswith("A tool failed")
    assert len(store.tasks_for_conversation(conv["conversation_id"], "owner")) == 1


def test_final_reply_reloads_with_activity_and_full_text(tmp_path, monkeypatch):
    store, conv, tid, _ = setup_task(tmp_path, monkeypatch)
    text = "Fixed the parser.\n\n" + "Detailed evidence. " * 180 + "\nChecks: 3 passed."
    store.complete(tid, {"mode": "developer", "status": "completed", "final_response": text})
    first = chat_service.get_conversation("owner", conv["conversation_id"])
    second = chat_service.get_conversation("owner", conv["conversation_id"])
    assert first["messages"] == second["messages"]
    message = first["messages"][-1]
    assert message["content"] == text
    assert message["developer_result"] is True
    assert message["events"][-1]["payload"]["stage"] == "Running checks…"
    monkeypatch.setattr(chat_service.dev_bot, "submit_bot_task", lambda *a, **k: tid)
    def gen():
        return chat_service._stream_writable_bot_task("owner", conv, {}, "Fix the parser",
            conv["conversation_id"], threading.Event())
    async def collect():
        return [frame async for frame in chat_api._drive_stream(gen(), "request", conv["conversation_id"])]
    # Verify the same metadata reaches the live SSE path as the reload path.
    frames = asyncio.run(collect())
    payloads = [json.loads(frame.removeprefix("data: ")) for frame in frames]
    done = next(frame for frame in payloads if frame["type"] == "done")
    assert done["developer_result"] is True
    assert done["events"][-1]["payload"]["stage"] == "Running checks…"
    assert len(store.tasks_for_conversation(conv["conversation_id"], "owner")) == 1


def test_progress_query_is_bounded(tmp_path):
    store = CloudTaskStore(db_path=tmp_path / "bounded.db")
    tid = store.submit(session_key="dev", task_text="inspect")
    for n in range(130):
        store.add_event(tid, "progress", {"stage": str(n)})
    notes = store.get_progress(tid)
    assert len(notes) == 100
    assert notes[0]["stage"] == "30"
    assert notes[-1]["stage"] == "129"


def test_failed_task_sse_preserves_authoritative_outcome(tmp_path, monkeypatch):
    store, conv, tid, _ = setup_task(tmp_path, monkeypatch)
    store.fail(tid, "Workspace command unavailable")
    monkeypatch.setattr(chat_service.dev_bot, "submit_bot_task", lambda *a, **k: tid)

    async def collect():
        gen = chat_service._stream_writable_bot_task("owner", conv, {}, "Fix the parser",
            conv["conversation_id"], threading.Event())
        return [json.loads(frame.removeprefix("data: "))
                async for frame in chat_api._drive_stream(gen, "request", conv["conversation_id"])]

    terminal = asyncio.run(collect())[-1]
    assert terminal == {"type": "error", "message": "Workspace command unavailable",
                        "task_id": tid, "task_status": "failed"}
    assert len(store.tasks_for_conversation(conv["conversation_id"], "owner")) == 1


def test_viewer_exception_is_not_an_executor_failure():
    async def gen():
        yield {"type": "task", "task_id": "existing", "status": "running"}
        raise RuntimeError("private transport error")

    async def collect():
        return [json.loads(frame.removeprefix("data: "))
                async for frame in chat_api._drive_stream(gen(), "request", "conversation")]

    terminal = asyncio.run(collect())[-1]
    assert terminal == {"type": "error", "message": "Chat request failed. Try again."}
    assert "task_status" not in terminal
