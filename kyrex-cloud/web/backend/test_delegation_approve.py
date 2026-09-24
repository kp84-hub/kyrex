"""Delegated T2 approve — host-side, exact-task resolution.

Proves the smallest safe fix for the Delegated Work "Approve" action:

  * approving NAMES the exact task and the backend resolves THAT task's STORED
    approval token host-side (mirroring the Chief exact-word "approve"
    shortcut), so the owner never types — and the frontend/model never see —
    the token;
  * the action fails closed, recording NOTHING, unless the caller owns the
    task AND it has EXACTLY ONE pending approval AND that approval is tier 2
    AND a usable token exists;
  * a refusal never poisons ``operator_reply`` (so a later correct attempt can
    still succeed) and a repeated approve never overwrites a recorded reply;
  * the token is never present in the route response or the safe delegated
    view.

Run: python3 -m pytest test_delegation_approve.py
"""
import asyncio
import json
import os
import sys
import tempfile

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
_TMP = tempfile.mkdtemp(prefix="kyrex_delegation_approve_")
os.environ.setdefault("KYREX_DATA_DIR", _TMP)
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "delegation-approve-secret")

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
from task_store import CloudTaskStore  # noqa: E402

TOKEN = "delete-token-123"
SUMMARY = "delete calendar event \u201cWorkout\u201d"
DETAIL = "destructive calendar action"


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


COORD_POLICY = {"fs:read": 0, "bot:delegate": 0}
READ_POLICY = {"fs:read": 0}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """One isolated durable store shared by every seam under test."""
    s = CloudTaskStore(db_path=tmp_path / "chat-delegation-approve.db")
    monkeypatch.setattr(main, "store", s)
    monkeypatch.setattr(chat_service, "_task_store", lambda: s)
    monkeypatch.setattr(delegation, "CloudTaskStore", lambda *a, **k: s)
    monkeypatch.setattr(
        serve, "_bot_llm_config",
        lambda bot: {"provider": "openai", "api_key": "x", "model": "m"})
    main.sessions.clear()
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


def _delegated_task(store, monkeypatch, tmp_path, *, conversation_id="conv-1"):
    """A REAL delegated target task owned by ``alice`` (chat_id), then awaiting."""
    chief = _bot(monkeypatch, tmp_path, "chief", owner="alice",
                 policy=COORD_POLICY)
    _bot(monkeypatch, tmp_path, "dev", owner="alice", policy=READ_POLICY)
    view = delegation.submit_delegation(
        "alice", chief, "dev", "fix it", store=store,
        parent_conversation_id=conversation_id)
    return view


def _await_t2(store, task_id, *, token=TOKEN, tier=2, message_id="m1"):
    store.set_status(task_id, "running")
    store.persist_approval_request(
        task_id, "dev", message_id, tier, token, SUMMARY, DETAIL)


# ── 1. successful delegated approve ────────────────────────────────────

def test_delegated_approve_resolves_the_exact_task_with_the_stored_token(
        store, monkeypatch, tmp_path):
    view = _delegated_task(store, monkeypatch, tmp_path)
    task_id = view["task_id"]
    _await_t2(store, task_id)

    assert store.get_pending_approval(task_id)["operator_reply"] is None
    ok, message = chat_service.approve_delegated_task("alice", task_id)
    assert ok is True
    assert message == "Approved."
    # The EXACT stored token was recorded (the value the live resolver needs),
    # and the approval is still pending delivery — the owner typed nothing.
    assert store.get_pending_approval(task_id)["operator_reply"] == TOKEN


def test_approve_route_returns_only_a_safe_confirmation(
        store, monkeypatch, tmp_path):
    view = _delegated_task(store, monkeypatch, tmp_path)
    task_id = view["task_id"]
    _await_t2(store, task_id)

    out = chat_api.approve_delegated_task(task_id, _cookie("alice"))
    assert out == {"approved": True, "task_id": task_id}
    # No token, and no secret-shaped key, in the response.
    assert "token" not in json.dumps(out)
    assert TOKEN not in json.dumps(out)


# ── 2. unrelated / ambiguous task refusal ──────────────────────────────

def test_delegated_approve_refuses_a_task_the_caller_does_not_own(
        store, monkeypatch, tmp_path):
    view = _delegated_task(store, monkeypatch, tmp_path)
    task_id = view["task_id"]
    _await_t2(store, task_id)

    ok, message = chat_service.approve_delegated_task("bob", task_id)
    assert ok is False
    # Nothing recorded: the approval is untouched.
    assert store.get_pending_approval(task_id)["operator_reply"] is None

    # The route maps a foreign/absent task to 404 (indistinguishable).
    with pytest.raises(main.HTTPException) as exc:
        chat_api.approve_delegated_task(task_id, _cookie("bob"))
    assert exc.value.status_code == 404


def test_delegated_approve_refuses_an_unknown_task(store, monkeypatch, tmp_path):
    _delegated_task(store, monkeypatch, tmp_path)
    ok, message = chat_service.approve_delegated_task("alice", "task-does-not-exist")
    assert ok is False
    assert message == chat_service.DELEGATED_APPROVE_NOT_FOUND


def test_delegated_approve_refuses_when_many_approvals_are_pending(
        store, monkeypatch, tmp_path):
    view = _delegated_task(store, monkeypatch, tmp_path)
    task_id = view["task_id"]
    _await_t2(store, task_id, message_id="m1")
    # A SECOND pending approval on the same task makes the target ambiguous.
    store.persist_approval_request(
        task_id, "dev", "m2", 2, "other-token", "another", "detail")
    assert len(store.pending_approvals_for_task(task_id)) == 2

    ok, message = chat_service.approve_delegated_task("alice", task_id)
    assert ok is False
    assert "single pending approval" in message
    assert all(r["operator_reply"] is None
               for r in store.pending_approvals_for_task(task_id))

    with pytest.raises(main.HTTPException) as exc:
        chat_api.approve_delegated_task(task_id, _cookie("alice"))
    assert exc.value.status_code == 409


def test_delegated_approve_refuses_when_no_approval_is_pending(
        store, monkeypatch, tmp_path):
    view = _delegated_task(store, monkeypatch, tmp_path)
    ok, message = chat_service.approve_delegated_task("alice", view["task_id"])
    assert ok is False
    assert "single pending approval" in message


def test_delegated_approve_refuses_a_non_t2_approval(
        store, monkeypatch, tmp_path):
    view = _delegated_task(store, monkeypatch, tmp_path)
    task_id = view["task_id"]
    _await_t2(store, task_id, tier=1)

    ok, message = chat_service.approve_delegated_task("alice", task_id)
    assert ok is False
    assert "not a T2 approval" in message
    assert store.get_pending_approval(task_id)["operator_reply"] is None


# ── 3. no poisoning of operator_reply ──────────────────────────────────

def test_delegated_approve_refuses_without_a_usable_token_and_poisons_nothing(
        store, monkeypatch, tmp_path):
    view = _delegated_task(store, monkeypatch, tmp_path)
    task_id = view["task_id"]
    # A token-less T2 approval (the Calendar Editor executor emits no token):
    # the confirmation value is empty, so the action must RECORD NOTHING.
    _await_t2(store, task_id, token="")

    ok, message = chat_service.approve_delegated_task("alice", task_id)
    assert ok is False
    assert "no usable token" in message
    assert store.get_pending_approval(task_id)["operator_reply"] is None

    with pytest.raises(main.HTTPException) as exc:
        chat_api.approve_delegated_task(task_id, _cookie("alice"))
    assert exc.value.status_code == 409


def test_delegated_approve_retry_is_safe(store, monkeypatch, tmp_path):
    view = _delegated_task(store, monkeypatch, tmp_path)
    task_id = view["task_id"]
    _await_t2(store, task_id)

    assert chat_service.approve_delegated_task("alice", task_id)[0] is True
    # A retry is refused and NEVER overwrites the recorded reply.
    ok, message = chat_service.approve_delegated_task("alice", task_id)
    assert ok is False
    assert "already answered" in message
    assert store.get_pending_approval(task_id)["operator_reply"] == TOKEN
    with pytest.raises(main.HTTPException) as exc:
        chat_api.approve_delegated_task(task_id, _cookie("alice"))
    assert exc.value.status_code == 409


# ── 4. token non-exposure ──────────────────────────────────────────────

def test_delegated_view_never_carries_the_token(store, monkeypatch, tmp_path):
    conv = chat_service.create_conversation("alice", title="Chief of Staff")
    cid = conv["conversation_id"]
    view = _delegated_task(store, monkeypatch, tmp_path, conversation_id=cid)
    task_id = view["task_id"]
    _await_t2(store, task_id)

    synced = chat_service.sync_delegated_work("alice", cid)
    row = next(r for r in synced["delegations"] if r["task_id"] == task_id)
    approval = row.get("approval") or {}
    assert approval.get("tier") == 2
    assert "token" not in approval
    assert TOKEN not in json.dumps(synced)

    # The coordinator stream frame is likewise token-free.
    frames = asyncio.run(_collect(
        chat_service._stream_delegated_work("alice", conv, cid)))
    for f in frames:
        assert TOKEN not in json.dumps(f)


async def _collect(agen):
    out = []
    async for frame in agen:
        out.append(frame)
    return out