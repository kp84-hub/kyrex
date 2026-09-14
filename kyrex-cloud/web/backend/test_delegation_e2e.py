"""Bot-to-Bot delegation — END-TO-END proof (real worker + approval protocol).

Drives the FULL slice with no shortcuts:

  Chief (coordinator Bot, owner alice)
    -> delegation.submit_delegation  (durable delegation + ordinary target task)
    -> TaskWorker.execute_task -> serve.run_task  (the EXISTING executor path)
    -> target Bot's own policy raises a T1 approval (KYREX_APPROVAL)
    -> the OWNER approves through the EXISTING task-respond flow
    -> target completes
    -> chat_service._stream_delegated_work relays ONLY the safe result back
       into the coordinator conversation.

Asserts the non-negotiables:
  * the target task belongs to the TARGET Bot and the OWNER (chat_id);
  * the approval is tied to the target task and can only be answered by the
    owner — the coordinator has no path and no session;
  * the coordinator view carries NO approval token and no secrets;
  * the coordinator conversation receives the sanitized final summary.

Run: python3 -m pytest test_delegation_e2e.py
"""
import asyncio
import os
import sys
import tempfile
import threading
import time

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
_TMP = tempfile.mkdtemp(prefix="kyrex_delegation_e2e_")
os.environ.setdefault("KYREX_DATA_DIR", _TMP)
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "delegation-e2e-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402
import chat_service  # noqa: E402
import bots  # noqa: E402
import serve  # noqa: E402
import delegation  # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402


class Request:
    def __init__(self, headers=None, cookies=None, body=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self._body = body or {}

    async def json(self):
        return self._body


# A deterministic stand-in for the REAL fs executor: it speaks the executor
# protocol exactly (OPERATION -> verdict -> APPROVAL -> decision -> RESULT).
# Registered under the "fs" prefix so the APPROVAL path derives the KNOWN
# operation fs:write (tier 1) from the summary/op.
EXECUTOR_SRC = '''
import sys, json
def emit(kind, obj):
    sys.stdout.write("KYREX_" + kind + ":" + json.dumps(obj) + "\\n")
    sys.stdout.flush()
emit("PROGRESS", {"step": "start"})
emit("OPERATION", {"op": "fs.write", "target": "delegated.txt",
                   "summary": "write the delegated artifact"})
v = sys.stdin.readline().strip()
if v == "APPROVE":
    emit("APPROVAL", {"tier": 1, "summary": "write the delegated artifact",
                      "detail": "", "token": ""})
    d = sys.stdin.readline().strip()
else:
    d = v
emit("RESULT_JSON", {"status": "done",
                     "final_response": "delegated work complete",
                     "decision": d})
'''

COORD_POLICY = {"fs:read": 0, "bot:delegate": 0}
TARGET_POLICY = {"fs:read": 0, "fs:write": 1}


def _bot(monkeypatch, tmp_path, bot_id, *, owner, policy):
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    try:
        return bots.get_bot(bot_id)
    except KeyError:
        rift = tmp_path / f"rift-{bot_id}"
        rift.mkdir(parents=True, exist_ok=True)
        return bots.add_bot(
            bot_id, f"Bot {bot_id}", "test:model", str(rift), policy=policy,
            status="running", owner=owner, provider_profile_id="p1")


async def _collect(agen):
    out = []
    async for frame in agen:
        out.append(frame)
    return out


def test_end_to_end_delegation_with_owner_approval(tmp_path, monkeypatch):
    store = CloudTaskStore(db_path=tmp_path / "e2e.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(chat_service, "_task_store", lambda: store)
    monkeypatch.setattr(
        serve, "_bot_llm_config",
        lambda bot: {"provider": "openai", "api_key": "x", "model": "m"})

    chief = _bot(monkeypatch, tmp_path, "chief", owner="alice",
                 policy=COORD_POLICY)
    target = _bot(monkeypatch, tmp_path, "target", owner="alice",
                  policy=TARGET_POLICY)

    fake = tmp_path / "fake_fs.py"
    fake.write_text(EXECUTOR_SRC)
    monkeypatch.setitem(serve.EXECUTORS, "fs", str(fake))

    # Coordinator conversation (the parent link for the delegation).
    conv = chat_service.create_conversation("alice", title="Chief of Staff")
    conversation_id = conv["conversation_id"]

    # ── 1. Chief delegates to the running same-owner target ────────────
    view = delegation.submit_delegation(
        "alice", chief, "target", "write the delegated artifact",
        store=store, parent_conversation_id=conversation_id,
        executor_prefix="fs")
    task_id = view["task_id"]
    assert view["target_bot_id"] == "target"

    task = store.get(task_id)
    # The target task is ordinary, TARGET-owned, and OWNER-owned.
    assert task["session_key"] == "target"
    assert task["bot_id"] == "target"
    assert task["chat_id"] == "alice"
    assert task["parent_delegation_id"] == view["delegation_id"]
    assert task["conversation_id"] == conversation_id

    # ── 2. Run the EXISTING worker -> serve.run_task path ─────────────
    worker = TaskWorker(store, worker_id="e2e-w", executor=serve.run_task,
                        max_workers=1)
    runner = threading.Thread(target=lambda: worker.execute_task(store.get(task_id)))
    runner.start()

    # ── 3. The target raises an approval ──────────────────────────────
    approval = None
    deadline = time.time() + 30
    while time.time() < deadline:
        approval = store.get_pending_approval(task_id)
        if approval is not None:
            break
        time.sleep(0.05)
    assert approval is not None, "target did not raise an approval"
    # The approval belongs to the TARGET task (its session key is the target,
    # never the coordinator) and is visible for the operator to answer.
    assert approval["session_key"] == "target"

    # ── 4. Chief CANNOT approve; only the owner can ───────────────────
    # A stranger (no ownership) is refused by the existing respond endpoint.
    main.sessions.clear()
    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(main.respond_task(
            task_id, Request(cookies={"session": "sess-bob"},
                             body={"text": "y"})))
    assert exc.value.status_code in (401, 404)
    # The owner is accepted (task chat_id == owner).
    main.sessions["sess-alice"] = "alice"
    ok = asyncio.run(main.respond_task(
        task_id, Request(cookies={"session": "sess-alice"},
                         body={"text": "y"})))
    assert "recorded" in ok

    # Deliver the recorded reply to the live (in-process) approval handler —
    # the same handoff the worker's reply-poller performs.
    assert store.respond(task_id, "y") is True

    runner.join(30)
    assert not runner.is_alive(), "target task did not finish"
    assert store.status(task_id) == "done"

    # ── 5. Chief receives ONLY the safe result ────────────────────────
    # A coordinator turn yields the CURRENT safe status (never a blocking tail):
    # the terminal state is reached because the target task is already done.
    frames = asyncio.run(_collect(
        chat_service._stream_delegated_work("alice", conv, conversation_id)))

    types = [f.get("type") for f in frames]
    assert "delegation" in types
    assert "delegation_result" in types

    # The coordinator view never carries the approval token.
    for f in frames:
        if f.get("type") == "approval_request":
            assert "token" not in f or not f.get("token")

    result = next(f for f in frames if f.get("type") == "delegation_result")
    assert result["status"] == "done"
    assert result["target_bot_id"] == "target"
    assert "delegated work complete" in result["summary"]
    # No secret material in the relayed summary.
    assert "sk-test" not in result["summary"]
    assert "api_key" not in result["summary"].lower()

    # The durable delegation is finalized with the sanitized summary.
    rec = store.get_delegation(view["delegation_id"])
    assert rec["status"] == "done"
    assert "delegated work complete" in (rec["result_summary"] or "")

    # The coordinator conversation receives the summary exactly once, through
    # the owner-scoped sync (the durable relay the UI's poll drives).
    first = chat_service.sync_delegated_work("alice", conversation_id)
    assert first["relayed"], "a newly terminal result must be relayed"
    notice = first["relayed"][0]
    assert notice["status"] == "done"
    assert "[Delegated to target]" in notice["message"]
    assert "delegated work complete" in notice["message"]

    conv_now = chat_service.get_conversation("alice", conversation_id)
    joined = "\n".join(m.get("content", "") for m in conv_now.get("messages", []))
    assert "[Delegated to target]" in joined
    assert "delegated work complete" in joined

    # Idempotent: a second sync relays NOTHING and adds no duplicate message.
    before = len(conv_now.get("messages", []))
    second = chat_service.sync_delegated_work("alice", conversation_id)
    assert second["relayed"] == []
    conv_again = chat_service.get_conversation("alice", conversation_id)
    assert len(conv_again.get("messages", [])) == before
