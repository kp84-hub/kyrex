"""Bot-to-Bot delegation — store + module tests (owner-scoped, single-level).

Proves the FIRST safe coordination slice without a live LLM, worker, or Rift
execution:

  1. the durable delegation table (create/get/list/status/result) and the
     ``parent_delegation_id`` link carried onto the target task row;
  2. the coordinator gate — only a Bot explicitly granted ``bot:delegate`` may
     delegate;
  3. single-level enforcement — depth > 1 and a nesting (parent_delegation_id)
     are refused;
  4. target eligibility — foreign owner, unknown, stopped/paused, no provider
     configuration, and an unavailable Rift are each refused with a clear
     message;
  5. submission creates an ORDINARY target task through the EXISTING store
     (session_key/bot_id = target, resolve_bot=True, chat_id = owner) — nothing
     is executed inline;
  6. two same-owner target Bots receive SEPARATE delegated work, and a foreign
     owner's Bot is neither visible nor delegatable;
  7. the safe views leak nothing sensitive.

Run: python3 -m pytest test_delegation.py
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolate the data root BEFORE importing paths-dependent modules.
_TMP = tempfile.mkdtemp(prefix="kyrex_delegation_")
os.environ["KYREX_DATA_DIR"] = _TMP

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import bots  # noqa: E402
import serve  # noqa: E402
import delegation  # noqa: E402
from task_store import CloudTaskStore, STATUS_QUEUED  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────

def _store(tmp_path):
    return CloudTaskStore(db_path=tmp_path / "delegation.db")


def _rift(tmp_path, name):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _register(monkeypatch, tmp_path, bot_id, *, owner, status="running",
              policy=None, provider=True):
    """Register a Bot with a resolvable Rift and (by default) a provider."""
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "test:model", _rift(tmp_path, f"rift-{bot_id}"),
        policy=policy or {}, status=status, owner=owner,
        provider_profile_id="p1" if provider else "",
    )


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    """Treat provider-profile resolution as configured by default.

    Tests override this to exercise the unconfigured case. This keeps the
    suite hermetic (no encrypted profile store needed) while still exercising
    the SAME ``serve._bot_llm_config`` seam the executor uses.
    """
    monkeypatch.setattr(
        serve, "_bot_llm_config",
        lambda bot: {"provider": "openai", "api_key": "x", "model": "m"},
    )


COORD_POLICY = {"fs:read": 0, "bot:delegate": 0}
TARGET_POLICY = {"fs:read": 0}


# ── store layer ────────────────────────────────────────────────────

def test_store_create_get_list_and_result(tmp_path):
    store = _store(tmp_path)
    did = store.create_delegation(
        owner="alice", coordinator_bot_id="chief", target_bot_id="dev",
        task_text="do a thing", parent_conversation_id="conv-1", depth=1,
    )
    rec = store.get_delegation(did)
    assert rec["delegation_id"] == did
    assert rec["owner"] == "alice"
    assert rec["status"] == STATUS_QUEUED
    assert rec["parent_conversation_id"] == "conv-1"

    store.set_delegation_status(did, "running")
    assert store.get_delegation(did)["status"] == "running"

    store.set_delegation_result(did, "all good", status="done")
    rec = store.get_delegation(did)
    assert rec["result_summary"] == "all good"
    assert rec["status"] == "done"
    assert rec["finished_at"]

    listed = store.list_delegations(owner="alice")
    assert [r["delegation_id"] for r in listed] == [did]
    assert store.list_delegations(owner="bob") == []


def test_target_task_carries_parent_delegation_link(tmp_path, monkeypatch):
    store = _store(tmp_path)
    coord = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice", policy=TARGET_POLICY)

    view = delegation.submit_delegation(
        "alice", coord, "dev", "build it", store=store,
        parent_conversation_id="conv-1",
    )
    task = store.get(view["task_id"])
    assert task["parent_delegation_id"] == view["delegation_id"]
    assert task["conversation_id"] == "conv-1"
    # The target task is an ORDINARY task bound to the TARGET, owned by the
    # submitting owner (so the owner — never the coordinator — can answer an
    # approval through the existing task-respond flow).
    assert task["session_key"] == "dev"
    assert task["bot_id"] == "dev"
    assert task["chat_id"] == "alice"


# ── coordinator gate + single-level ────────────────────────────────

def test_only_coordinator_can_delegate(tmp_path, monkeypatch):
    store = _store(tmp_path)
    plain = _register(monkeypatch, tmp_path, "plain", owner="alice",
                      policy=TARGET_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice", policy=TARGET_POLICY)
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", plain, "dev", "x", store=store)


def test_depth_and_nesting_refused(tmp_path, monkeypatch):
    store = _store(tmp_path)
    coord = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice", policy=TARGET_POLICY)
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", coord, "dev", "x", store=store,
                                     depth=2)
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", coord, "dev", "x", store=store,
                                     parent_delegation_id="dlg-prev")


def test_self_delegation_refused(tmp_path, monkeypatch):
    store = _store(tmp_path)
    coord = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", coord, "chief", "x", store=store)


def test_foreign_owner_not_visible_or_delegatable(tmp_path, monkeypatch):
    store = _store(tmp_path)
    coord = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "bobdev", owner="bob",
              policy=TARGET_POLICY)

    ids = {m["id"] for m in delegation.visible_targets("alice",
                                                       exclude_bot_id="chief")}
    assert "bobdev" not in ids
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation("alice", coord, "bobdev", "x", store=store)


# ── target eligibility ─────────────────────────────────────────────

def test_stopped_and_paused_targets_refused(tmp_path, monkeypatch):
    store = _store(tmp_path)
    coord = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "stopdev", owner="alice",
              status="stopped", policy=TARGET_POLICY)
    _register(monkeypatch, tmp_path, "pausedev", owner="alice",
              status="paused", policy=TARGET_POLICY)
    for target in ("stopdev", "pausedev"):
        with pytest.raises(delegation.DelegationError) as ei:
            delegation.submit_delegation("alice", coord, target, "x",
                                         store=store)
        assert "start it" in str(ei.value)


def test_unconfigured_provider_refused(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(serve, "_bot_llm_config", lambda bot: None)
    coord = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice", provider=False,
              policy=TARGET_POLICY)
    with pytest.raises(delegation.DelegationError) as ei:
        delegation.submit_delegation("alice", coord, "dev", "x", store=store)
    assert "provider configuration" in str(ei.value)


def test_unavailable_rift_refused(tmp_path, monkeypatch):
    store = _store(tmp_path)
    coord = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    bots.add_bot("ghost", "Ghost", "test:model",
                 str(tmp_path / "does-not-exist"), policy=TARGET_POLICY,
                 status="running", owner="alice", provider_profile_id="p1")
    with pytest.raises(delegation.DelegationError) as ei:
        delegation.submit_delegation("alice", coord, "ghost", "x", store=store)
    assert "Rift" in str(ei.value)


# ── two same-owner targets, separate work ──────────────────────────

def test_two_targets_receive_separate_work(tmp_path, monkeypatch):
    store = _store(tmp_path)
    coord = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice",
              policy=TARGET_POLICY)
    _register(monkeypatch, tmp_path, "qa", owner="alice",
              policy=TARGET_POLICY)

    d1 = delegation.submit_delegation("alice", coord, "dev", "fix bug",
                                      store=store)
    d2 = delegation.submit_delegation("alice", coord, "qa", "test fix",
                                      store=store)

    assert d1["delegation_id"] != d2["delegation_id"]
    assert d1["task_id"] != d2["task_id"]
    assert store.get(d1["task_id"])["session_key"] == "dev"
    assert store.get(d2["task_id"])["session_key"] == "qa"
    listed = store.list_delegations(owner="alice")
    assert {r["target_bot_id"] for r in listed} == {"dev", "qa"}


# ── safe metadata / views ──────────────────────────────────────────

def test_safe_metadata_leaks_nothing(tmp_path, monkeypatch):
    _register(monkeypatch, tmp_path, "dev", owner="alice",
              policy={"fs:read": 0, "fs:write": 0})
    bot = bots.get_bot("dev")
    meta = delegation.safe_bot_metadata(bot)
    assert set(meta) == {"id", "name", "status", "role", "capabilities",
                         "model", "available"}
    blob = repr(meta)
    assert bot["rift"] not in blob
    assert "policy" not in meta


def test_public_view_excludes_secrets(tmp_path, monkeypatch):
    store = _store(tmp_path)
    coord = _register(monkeypatch, tmp_path, "chief", owner="alice",
                      policy=COORD_POLICY)
    _register(monkeypatch, tmp_path, "dev", owner="alice",
              policy=TARGET_POLICY)
    view = delegation.submit_delegation("alice", coord, "dev", "x",
                                        store=store)
    assert "rift" not in view
    assert "policy" not in view
    assert "system_prompt" not in view
    assert "provider_profile_id" not in view


def test_coordinator_gate_predicate():
    assert serve.is_coordinator_policy({"bot:delegate": 0}) is True
    assert serve.is_coordinator_policy({"bot:delegate": 1}) is False
    assert serve.is_coordinator_policy({"fs:read": 0}) is False
    assert serve.is_coordinator_policy({}) is False
    assert serve.is_coordinator_policy("nonsense") is False
