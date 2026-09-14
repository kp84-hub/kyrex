"""Bot-to-Bot delegation — Chat layer tests.

Proves the user-facing safety properties of the first coordination slice:

  1. the ``delegate_task`` capability is present ONLY when the coordinator
     capability (``bot:delegate``) is granted — a read-only Bot never gets it;
  2. a coordinator engine session answers a ``delegation`` confirm by creating
     the durable delegation + ordinary target task HOST-side, and refuses an
     ineligible target with a clear, safe reason;
  3. the delegated target task is owned by the submitting OWNER (chat_id), so
     an approval can be answered ONLY by that owner through the EXISTING
     ``/api/task/{id}/respond`` flow — never by the coordinator or another user;
  4. ``/api/delegations`` is owner-scoped and read-only;
  5. the coordinator preset configures a Bot through the existing owner-scoped
     configure endpoint.

Run: python3 -m pytest test_delegation_chat.py
"""
import asyncio
import os
import sys
import tempfile

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
_TMP = tempfile.mkdtemp(prefix="kyrex_delegation_chat_")
os.environ.setdefault("KYREX_DATA_DIR", _TMP)
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "delegation-chat-secret")

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
    """Minimal request shim (mirrors test_bearer_auth's)."""

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
    s = CloudTaskStore(db_path=tmp_path / "chat-delegation.db")
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


# ── capability gating ──────────────────────────────────────────────

def test_delegate_tool_only_when_coordinator_granted():
    coord = bot_capabilities.derive_bot_capabilities(COORD_POLICY)
    assert "delegate_task" in coord["tools"]
    readonly = bot_capabilities.derive_bot_capabilities(READ_POLICY)
    assert "delegate_task" not in readonly["tools"]


def test_coordinator_context_safe_roster(store, monkeypatch, tmp_path):
    coord = _bot(monkeypatch, tmp_path, "chief", owner="alice", policy=COORD_POLICY)
    _bot(monkeypatch, tmp_path, "dev", owner="alice", policy=READ_POLICY)
    _bot(monkeypatch, tmp_path, "bobqa", owner="bob", policy=READ_POLICY)

    ctx = chat_service.build_coordinator_context("alice", coord)
    assert "id: dev" in ctx                 # the owner's peer is listed
    assert "id: bobqa" not in ctx           # a foreign owner's Bot is not
    assert "id: chief" not in ctx           # the coordinator excludes itself
    assert "delegate_task" in ctx
    # Never any sensitive VALUE: no Rift path, provider reference, or prompt.
    assert coord["rift"] not in ctx
    assert "provider_profile_id" not in ctx
    assert "rift" not in ctx.lower()


# ── engine session delegation handling ─────────────────────────────

def test_engine_session_creates_delegation_host_side(store, monkeypatch, tmp_path):
    coord = _bot(monkeypatch, tmp_path, "chief", owner="alice", policy=COORD_POLICY)
    _bot(monkeypatch, tmp_path, "dev", owner="alice", policy=READ_POLICY)

    sess = object.__new__(chat_service.EngineSession)
    sess.delegation_ctx = {"owner": "alice", "bot": coord, "conversation_id": "c1"}
    ok, result = sess._handle_delegation(
        {"target_bot_id": "dev", "task": "build it"})
    assert ok is True
    assert result["target_bot_id"] == "dev"
    assert result["task_id"]
    assert "rift" not in result and "policy" not in result
    task = store.get(result["task_id"])
    assert task["session_key"] == "dev"
    assert task["chat_id"] == "alice"


def test_engine_session_refuses_ineligible_target(store, monkeypatch, tmp_path):
    coord = _bot(monkeypatch, tmp_path, "chief", owner="alice", policy=COORD_POLICY)
    _bot(monkeypatch, tmp_path, "stopped-dev", owner="alice",
         policy=READ_POLICY, status="stopped")

    sess = object.__new__(chat_service.EngineSession)
    sess.delegation_ctx = {"owner": "alice", "bot": coord, "conversation_id": "c1"}
    ok, result = sess._handle_delegation(
        {"target_bot_id": "stopped-dev", "task": "x"})
    assert ok is False
    assert "start it" in result["error"]


# ── approval ownership ─────────────────────────────────────────────

def test_target_approval_owner_only(store, monkeypatch, tmp_path):
    coord = _bot(monkeypatch, tmp_path, "chief", owner="alice", policy=COORD_POLICY)
    _bot(monkeypatch, tmp_path, "dev", owner="alice", policy=READ_POLICY)
    view = delegation.submit_delegation(
        "alice", coord, "dev", "fix it", store=store,
        parent_conversation_id="conv-1")
    task_id = view["task_id"]

    # A different (or unauthenticated) user cannot touch the delegated task.
    with pytest.raises(main.HTTPException) as exc:
        _call(main.respond_task(task_id, Request(body={"text": "y"})))
    assert exc.value.status_code in (401, 404)

    # The owner (the task's chat_id) is accepted by the EXISTING flow. The
    # coordinator has no user session and no path here at all.
    main.sessions["sess-alice"] = "alice"
    out = _call(main.respond_task(
        task_id, Request(cookies={"session": "sess-alice"}, body={"text": "y"})))
    assert "recorded" in out


# ── delegated work view ────────────────────────────────────────────

def test_delegations_api_owner_scoped(store, monkeypatch, tmp_path):
    coord = _bot(monkeypatch, tmp_path, "chief", owner="alice", policy=COORD_POLICY)
    _bot(monkeypatch, tmp_path, "dev", owner="alice", policy=READ_POLICY)
    delegation.submit_delegation("alice", coord, "dev", "x", store=store,
                                 parent_conversation_id="conv-1")

    alice = chat_api.list_delegations(_cookie("alice"))
    bob = chat_api.list_delegations(_cookie("bob"))
    assert len(alice["delegations"]) == 1
    assert alice["delegations"][0]["target_bot_id"] == "dev"
    assert bob["delegations"] == []


# ── coordinator preset configuration ───────────────────────────────

def test_configure_coordinator_preset(store, monkeypatch, tmp_path):
    _bot(monkeypatch, tmp_path, "chief", owner="alice", policy=READ_POLICY)
    main.sessions["sess-alice"] = "alice"
    out = _call(chat_api.configure_bot(
        "chief", Request(cookies={"session": "sess-alice"},
                         body={"preset": "coordinator"})))
    assert out["coordinator"] is True
    assert out["policy"]["bot:delegate"] == 0
    assert serve.is_coordinator_policy(bots.get_bot("chief")["policy"]) is True
