"""Integration: a Bot-bound Chat task is consumed by the live TaskWorker loop."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLOUD = ROOT.parent
for value in (str(ROOT), str(CLOUD)):
    if value not in sys.path:
        sys.path.insert(0, value)

os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-chat-worker-lifecycle")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "test-model")
os.environ.setdefault("KYREX_API_KEY", "test-key")

import chat_service
from task_store import CloudTaskStore, TaskWorker


async def _frames(generator):
    return [frame async for frame in generator]


def test_bot_chat_task_is_claimed_and_reaches_terminal(tmp_path, monkeypatch):
    rift = tmp_path / "bot-rift"
    rift.mkdir()
    subprocess.run(["git", "init", "-q", str(rift)], check=True)

    store = CloudTaskStore(db_path=tmp_path / "tasks.db")
    bot = {
        "id": "dev",
        "rift": str(rift),
        "status": "running",
        "policy": {"fs:write": 1},
        "model": "test-model",
    }
    monkeypatch.setattr(chat_service, "_task_store", lambda: store)

    def executor(**kwargs):
        kwargs["on_progress"]({"phase": "integration"})
        kwargs["on_result"]({"status": "no_changes", "final_response": "worker completed"})

    worker = TaskWorker(store, worker_id="lifecycle-test", executor=executor,
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        frames = asyncio.run(_frames(chat_service._stream_writable_bot_task(
            "alice", {"conversation_id": "conversation-1", "messages": []}, bot, "test task", "conversation-1",
            asyncio.Event(),
        )))
    finally:
        worker.stop()

    task_frame = next(frame for frame in frames if frame.get("type") == "task")
    task_id = task_frame["task_id"]
    task = store.get(task_id)
    events = store.get_events(task_id, after_event_id=0)

    assert task["status"] == "done"
    assert any(event["type"] == "submitted" for event in events)
    assert any(event["type"] == "claimed" for event in events)
    assert any(event["type"] == "result" for event in events)
    assert frames[-1] == {
        "type": "status", "status": "complete", "content": "worker completed"
    }


def test_routed_mail_task_is_not_recovered_as_raw_chat_reply(tmp_path, monkeypatch):
    import dev_bot
    import jev_stream_router
    import serve
    import delegation
    from types import SimpleNamespace
    store = CloudTaskStore(db_path=tmp_path / 'gmail-recovery.db')
    target = {'id': 'email', 'owner': 'alice', 'status': 'running', 'policy': {}}
    monkeypatch.setattr(dev_bot._bots, 'is_running', lambda bot: True)
    monkeypatch.setattr(dev_bot, 'gmail_route_ready', lambda bot: True)
    monkeypatch.setattr(jev_stream_router, '_owned_running_bot', lambda *a: target)
    monkeypatch.setattr(jev_stream_router, '_gmail_command_for_routed_turn', lambda *a, **k: 'gmail: search homecoming')
    chat = SimpleNamespace(_task_store=lambda: store, delegation=delegation, serve=serve)
    session = SimpleNamespace(delegation_ctx={'owner': 'alice', 'bot': {'id': 'chief'}, 'conversation_id': 'conv'})
    ok, result = jev_stream_router._submit_routed_gmail(chat, dev_bot, session, {'task': 'search'}, {'selected_bot_id': 'email'})
    assert ok
    task = store.get(result['task_id'])
    assert task['parent_delegation_id'] == result['delegation_id']
    store.complete(task['task_id'], {'final_response': 'RAW EMAIL BODY'})
    monkeypatch.setattr(chat_service, '_task_store', lambda: store)
    conv = {'conversation_id': 'conv', 'messages': []}
    assert chat_service._recover_finished_bot_task_messages('alice', conv)['messages'] == []
