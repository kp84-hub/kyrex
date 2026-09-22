"""Delegated-work FOLLOW-UP experience — status, relay, and one answer.

Covers the coordinator follow-up contract end to end at the host seam:

  1. running -> done        : the status tool/``delegation_status`` reports the
                              target's LIVE status from the durable record;
  2. running -> awaiting    : a delegated approval is REPORTED, never resolved —
                              the coordinator cannot approve/deny it;
  3. failure / cancellation : a terminal non-success is relayed once;
  4. foreign-owner denial   : another owner (or another coordinator) sees
                              nothing — no cross-owner probe;
  5. result relay           : the terminal result is appended to the parent
                              conversation EXACTLY ONCE (idempotent);
  6. one answer per turn    : a coordinator turn that queries status yields ONE
                              concise answer and does NOT re-append the result.

Run: python3 -m pytest test_delegation_followup.py
"""
import asyncio
import os
import queue as _queue
import sys
import tempfile
import threading
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
_TMP = tempfile.mkdtemp(prefix="kyrex_delegation_followup_")
os.environ.setdefault("KYREX_DATA_DIR", _TMP)
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "delegation-followup-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402
import chat_service  # noqa: E402
import chat_api  # noqa: E402
import bots  # noqa: E402
import serve  # noqa: E402
import delegation  # noqa: E402
import bot_capabilities  # noqa: E402
from task_store import CloudTaskStore  # noqa: E402


class Request:
    """Minimal request shim (mirrors test_delegation_chat's)."""

    def __init__(self, headers=None, cookies=None, body=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self._body = body or {}

    async def json(self):
        return self._body


def _call(coro):
    return asyncio.run(coro)


async def _collect(agen):
    out = []
    async for frame in agen:
        out.append(frame)
    return out


COORD_POLICY = {"fs:read": 0, "bot:delegate": 0}
READ_POLICY = {"fs:read": 0}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """One isolated durable store shared by every seam under test."""
    s = CloudTaskStore(db_path=tmp_path / "followup.db")
    monkeypatch.setattr(main, "store", s)
    monkeypatch.setattr(chat_service, "_task_store", lambda: s)
    monkeypatch.setattr(delegation, "CloudTaskStore", lambda *a, **k: s)
    monkeypatch.setattr(
        serve, "_bot_llm_config",
        lambda bot: {"provider": "openai", "api_key": "x", "model": "m"})
    return s


def _bot(monkeypatch, tmp_path, bot_id, *, owner, policy, status="running"):
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    try:
        return bots.get_bot(bot_id)
    except KeyError:
        rift = tmp_path / f"rift-{bot_id}"
        rift.mkdir(parents=True, exist_ok=True)
        return bots.add_bot(
            bot_id, f"Bot {bot_id}", "test:model", str(rift), policy=policy,
            status=status, owner=owner, provider_profile_id="p1")


def _cookie(user):
    tok = f"sess-{user}"
    main.sessions[tok] = user
    return Request(cookies={"session": tok})


def _setup(monkeypatch, tmp_path, store):
    """Chief (coordinator, alice) + a same-owner target; one conversation."""
    chief = _bot(monkeypatch, tmp_path, "chief", owner="alice",
                 policy=COORD_POLICY)
    target = _bot(monkeypatch, tmp_path, "target", owner="alice",
                  policy=READ_POLICY)
    conv = chat_service.create_conversation("alice", title="Chief")
    return chief, target, conv["conversation_id"]


def _submit(store, chief, cid, text="do the thing"):
    view = delegation.submit_delegation(
        "alice", chief, "target", text, store=store,
        parent_conversation_id=cid)
    return view


# ── 1. running -> done ─────────────────────────────────────────────

def test_status_running_then_done(store, monkeypatch, tmp_path):
    chief, _target, cid = _setup(monkeypatch, tmp_path, store)
    view = _submit(store, chief, cid)
    task_id = view["task_id"]

    # Target is claimed/running: the status query reflects the LIVE task.
    store.set_status(task_id, "running")
    rows = chat_service.coordinator_delegation_statuses("alice", "chief", cid)
    assert rows and rows[0]["status"] == "running"
    assert rows[0]["target_bot_id"] == "target"
    assert rows[0]["relayed"] is False

    # Target completes: the status is read from the EXISTING task record.
    store.complete(task_id, {"status": "done",
                             "final_response": "the delegated thing is done"})
    rows = chat_service.coordinator_delegation_statuses("alice", "chief", cid)
    assert rows[0]["status"] == "done"
    assert "the delegated thing is done" in rows[0]["result_summary"]


# ── 2. running -> awaiting approval (report-only) ──────────────────

def test_status_awaiting_approval_is_report_only(store, monkeypatch, tmp_path):
    chief, _target, cid = _setup(monkeypatch, tmp_path, store)
    view = _submit(store, chief, cid)
    task_id = view["task_id"]

    store.set_status(task_id, "running")
    store.persist_approval_request(
        task_id, "target", "m1", 1, "super-secret-token",
        "write the artifact", "needs your approval")

    rows = chat_service.coordinator_delegation_statuses("alice", "chief", cid)
    assert rows[0]["status"] == "awaiting_approval"
    # Coordinator status remains report-only: no token or decision.
    assert "token" not in rows[0]
    assert "decision" not in rows[0]

    # The owner-scoped Delegated Work refresh exposes only safe fields needed
    # to render task-scoped controls; the secret approval token never appears.
    synced = chat_service.sync_delegated_work("alice", cid)
    approval = synced["delegations"][0]["approval"]
    assert approval == {
        "task_id": task_id,
        "tier": 1,
        "summary": "write the artifact",
        "detail": "needs your approval",
    }
    assert "super-secret-token" not in str(synced)

    # The coordinator turn REPORTS the pending approval but never the token.
    frames = asyncio.run(_collect(
        chat_service._stream_delegated_work("alice", {"messages": []}, cid)))
    approvals = [f for f in frames if f.get("type") == "approval_request"]
    assert approvals, "a target-owned pending approval must be reported"
    assert not approvals[0].get("token")
    assert not any(f.get("type") == "delegation_result" for f in frames), \
        "non-terminal work must not be finalized"

    # Approval ownership is unchanged: only the OWNER can answer through the
    # task-scoped endpoint. A foreign session is refused outright.
    main.sessions.clear()
    with pytest.raises(main.HTTPException) as exc:
        _call(main.respond_task(
            task_id, Request(cookies={"session": "sess-bob"},
                             body={"text": "y"})))
    assert exc.value.status_code in (401, 404)


# ── 3. failure / cancellation relay ────────────────────────────────

def test_failure_relayed_once(store, monkeypatch, tmp_path):
    chief, _target, cid = _setup(monkeypatch, tmp_path, store)
    view = _submit(store, chief, cid)
    task_id = view["task_id"]
    store.set_status(task_id, "running")
    store.fail(task_id, "upstream exploded")

    rows = chat_service.coordinator_delegation_statuses("alice", "chief", cid)
    assert rows[0]["status"] == "failed"

    first = chat_service.sync_delegated_work("alice", cid)
    assert first["relayed"] and first["relayed"][0]["status"] == "failed"
    assert "failed" in first["relayed"][0]["message"].lower()

    conv = chat_service.get_conversation("alice", cid)
    n = len(conv["messages"])
    # Idempotent: the failure is announced exactly once.
    assert chat_service.sync_delegated_work("alice", cid)["relayed"] == []
    assert len(chat_service.get_conversation("alice", cid)["messages"]) == n


def test_cancellation_relayed(store, monkeypatch, tmp_path):
    chief, _target, cid = _setup(monkeypatch, tmp_path, store)
    view = _submit(store, chief, cid)
    task_id = view["task_id"]
    store.set_status(task_id, "running")
    store.cancel_effective(task_id, reason="stopped by owner")

    rows = chat_service.coordinator_delegation_statuses("alice", "chief", cid)
    assert rows[0]["status"] == "cancelled"

    relayed = chat_service.sync_delegated_work("alice", cid)["relayed"]
    assert relayed and relayed[0]["status"] == "cancelled"
    assert "cancelled" in relayed[0]["message"].lower()


def test_missing_target_task_fails_stale_delegation(
        store, monkeypatch, tmp_path):
    chief, _target, cid = _setup(monkeypatch, tmp_path, store)
    view = _submit(store, chief, cid)
    task_id = view["task_id"]

    # Simulate an orphaned delegation row: the linked task vanished while the
    # delegation still says queued. Reconciliation must not leave it active.
    with store._lock:
        store._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        store._conn.commit()

    synced = chat_service.sync_delegated_work("alice", cid)
    row = synced["delegations"][0]
    assert row["status"] == "failed"
    assert "linked task no longer exists" in row["error"]
    assert synced["relayed"][0]["status"] == "failed"


# ── 4. foreign-owner denial ────────────────────────────────────────

def test_foreign_owner_and_foreign_coordinator_denied(store, monkeypatch,
                                                      tmp_path):
    chief, _target, cid = _setup(monkeypatch, tmp_path, store)
    did = _submit(store, chief, cid)["delegation_id"]

    # Another owner sees nothing.
    assert chat_service.coordinator_delegation_statuses("bob", "chief", cid) == []
    # Another coordinator (same owner) sees nothing.
    assert chat_service.coordinator_delegation_statuses(
        "alice", "other-chief", cid) == []
    # A single-id probe is denied without revealing existence.
    assert delegation.fetch_delegation("bob", did) is None
    assert delegation.fetch_delegation(
        "alice", did, coordinator_bot_id="other-chief") is None
    assert delegation.fetch_delegation("alice", did).get("delegation_id") == did

    # The owner-scoped HTTP surface agrees.
    assert chat_api.list_delegations(_cookie("bob"))["delegations"] == []
    assert len(chat_api.list_delegations(_cookie("alice"))["delegations"]) == 1


# ── 5. result relay into the parent conversation (exactly once) ─────

def test_result_relay_exactly_once(store, monkeypatch, tmp_path):
    chief, _target, cid = _setup(monkeypatch, tmp_path, store)
    view = _submit(store, chief, cid)
    store.complete(view["task_id"], {"status": "done",
                                     "final_response": "artifact written"})

    sync = chat_service.sync_delegated_work("alice", cid)
    assert len(sync["delegations"]) == 1
    assert sync["delegations"][0]["status"] == "done"
    assert sync["delegations"][0]["relayed"] is True
    assert len(sync["relayed"]) == 1
    assert "[Delegated to target]" in sync["relayed"][0]["message"]
    assert "artifact written" in sync["relayed"][0]["message"]

    conv = chat_service.get_conversation("alice", cid)
    joined = "\n".join(m.get("content", "") for m in conv["messages"])
    assert joined.count("[Delegated to target]") == 1

    # A second sync is a no-op: no new notice, no duplicate message.
    n = len(conv["messages"])
    again = chat_service.sync_delegated_work("alice", cid)
    assert again["relayed"] == []
    assert len(chat_service.get_conversation("alice", cid)["messages"]) == n

    # The durable marker is what makes this survive a reload.
    rec = store.get_delegation(view["delegation_id"])
    assert rec["relayed_at"]


# ── 6. one concise answer per coordinator turn ─────────────────────

def test_one_answer_per_coordinator_turn(store, monkeypatch, tmp_path):
    chief, _target, cid = _setup(monkeypatch, tmp_path, store)
    view = _submit(store, chief, cid)
    store.set_status(view["task_id"], "running")

    sess = object.__new__(chat_service.EngineSession)
    sess._closed = False
    sess._turn_lock = threading.Lock()
    sess._stderr_lock = threading.Lock()
    sess.stderr_tail = []
    sess._proc = MagicMock()
    sess._proc.poll.return_value = None
    sess.surface_context = None
    sess.denied_requests = []
    sess.delegation_ctx = {"owner": "alice", "bot": chief,
                           "conversation_id": cid}
    sent = []
    sess._send = lambda payload: sent.append(payload)
    sess._frames = _queue.Queue()

    def _drain():
        # The coordinator model asks for status (the "did it finish?" turn),
        # then answers ONCE.
        sess._frames.put({"type": "confirm_request", "id": "c1",
                          "value": "delegation_status", "delegation_id": ""})
        sess._frames.put({"type": "chat_done",
                          "content": "It's still running."})
        sess._frames.put({"type": "phase", "value": "IDLE"})

    threading.Thread(target=_drain, daemon=True).start()
    final, err = sess.run_turn("Did it finish?", lambda t: None)
    assert err is None
    assert final == "It's still running."

    # Exactly one confirm_response, answered with the SAFE status payload.
    responses = [p for p in sent if p.get("type") == "confirm_response"]
    assert len(responses) == 1
    assert responses[0]["approved"] is True
    statuses = responses[0]["result"]["delegations"]
    assert statuses and statuses[0]["status"] == "running"

    # The status stream does NOT append a second assistant message: the turn's
    # single answer stands alone (no repeated/paraphrased reply).
    conv = chat_service.get_conversation("alice", cid)
    before = len(conv["messages"])
    asyncio.run(_collect(
        chat_service._stream_delegated_work("alice", conv, cid)))
    after = chat_service.get_conversation("alice", cid)
    assert len(after["messages"]) == before


# ── capability gating for the status tool ──────────────────────────

def test_status_tool_only_for_coordinator():
    coord = bot_capabilities.derive_bot_capabilities(COORD_POLICY)
    assert "delegation_status" in coord["tools"]
    readonly = bot_capabilities.derive_bot_capabilities(READ_POLICY)
    assert "delegation_status" not in readonly["tools"]
