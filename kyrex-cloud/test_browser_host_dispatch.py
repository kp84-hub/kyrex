"""Focused end-to-end tests for the Browser Bot execution slice.

Proves the EXISTING browser path — ``serve.run_task`` -> the host channel
(``HostManager.dispatch_browser_task``) -> the REAL host agent (``agent.py``) —
runs a task for a Bot that is EXPLICITLY bound to a host, and that EVERY other
case (unbound, revoked binding, host-registry fault, offline host) FAILS CLOSED
and never invokes a local browser executor. Reuses the in-memory duplex rig
from ``test_browser_host_channel`` (real Cloud channel + real host agent,
scripted browser executor).

Run: python3 -m pytest test_browser_host_dispatch.py
"""

import os
import sys

import pytest

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import bots                        # noqa: E402
import browser_host_channel as ch  # noqa: E402
import browser_hosts as bh         # noqa: E402
import routines                    # noqa: E402
import serve                       # noqa: E402
from test_browser_host_channel import (  # noqa: E402  — the shared duplex rig
    BOT, HOST, OWNER, CAND, NAV, Rig,
)

SECRET = "dispatch-test-secret"

_OK_RESULT = {"kind": "result",
              "result": {"status": "no_changes", "final_response": "example.com"}}

# The exact step shape a routine stores (navigate + read are browser-executable;
# extract/summarize are coordinator-side and end the browser prefix).
ROUTINE_STEPS = [
    {"action": "navigate", "url": "https://example.com/status"},
    {"action": "read"},
    {"action": "summarize", "prompt": "summarize"},
]


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_SESSION_SECRET", SECRET)
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    yield tmp_path


def _no_local_spawn(monkeypatch):
    """Fail the test if ANY local executor subprocess is spawned.

    A production browser task has no local executor; the fail-closed cases must
    never reach ``subprocess.Popen``.
    """
    calls = []

    def _boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("a local browser executor was spawned")

    monkeypatch.setattr("subprocess.Popen", _boom)
    return calls


def _run_task(rig, monkeypatch, task_text=CAND):
    monkeypatch.setattr(ch, "default_manager", lambda: rig.manager)
    sent = []
    serve.run_task(
        chat_id=OWNER, repo_url=None, task_text=task_text,
        executor_prefix="browser", session_key=BOT, resolve_bot=True,
        send=lambda cid, text: (sent.append(text), "m1")[1],
        edit=lambda cid, mid, text: None,
    )
    return sent


# ── the real dispatch path (bound success) ────────────────────────────

def test_bound_bot_runs_read_only_example_com(tmp_path):
    """One ordinary read-only task, bound Bot, through the real host path."""
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    try:
        result = rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        assert result["status"] == "no_changes"
        assert result["final_response"] == "example.com"
        assert rig.created[0].decisions == ["ALLOW"]
        assert "browser.task" in rig.audit.ops()
    finally:
        rig.disconnect()


def test_run_task_routes_bound_bot_through_host(tmp_path, monkeypatch):
    """The durable browser path routes a bound Bot to its host — never local."""
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    calls = _no_local_spawn(monkeypatch)
    try:
        sent = _run_task(rig, monkeypatch)
    finally:
        rig.disconnect()
    assert calls == [], "a local executor was spawned for a bound Bot"
    assert rig.created, "the host executor never ran"
    assert rig.created[0].decisions == ["ALLOW"]
    assert any("example.com" in (t or "") for t in sent)


# ── fail closed: no local fallback ────────────────────────────────────

def test_serve_helper_fails_closed_when_unbound(tmp_path):
    Rig(tmp_path, [])                       # register Bot + host
    bh.unbind_bot(OWNER, BOT)               # no implicit fallback
    ctx = serve.build_context(BOT)
    result, err = serve.browser_host_dispatch(ctx, CAND)
    assert result is None
    assert "no Browser Host is bound" in err


def test_serve_helper_fails_closed_when_revoked(tmp_path):
    Rig(tmp_path, [])
    bh.revoke_host(OWNER, HOST)             # revoke drops the binding
    ctx = serve.build_context(BOT)
    result, err = serve.browser_host_dispatch(ctx, CAND)
    assert result is None
    assert "no Browser Host is bound" in err


def test_serve_helper_fails_closed_on_registry_fault(tmp_path, monkeypatch):
    Rig(tmp_path, [])
    ctx = serve.build_context(BOT)

    def _boom(*args, **kwargs):
        raise RuntimeError("registry down")

    monkeypatch.setattr(bh, "binding_for", _boom)
    result, err = serve.browser_host_dispatch(ctx, CAND)
    assert result is None
    assert "browser host registry fault" in err


def test_serve_helper_routes_when_bound(tmp_path, monkeypatch):
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    monkeypatch.setattr(ch, "default_manager", lambda: rig.manager)
    rig.connect()
    try:
        ctx = serve.build_context(BOT)
        result, err = serve.browser_host_dispatch(ctx, CAND)
        assert err is None
        assert result["status"] == "no_changes"
    finally:
        rig.disconnect()


def test_run_task_fails_closed_when_unbound(tmp_path, monkeypatch):
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    bh.unbind_bot(OWNER, BOT)
    rig.connect()                            # the host IS online
    calls = _no_local_spawn(monkeypatch)
    try:
        sent = _run_task(rig, monkeypatch)
    finally:
        rig.disconnect()
    assert calls == [], "unbound Bot fell back to a local executor"
    assert rig.created == []                 # nothing ran on the host either
    assert any("failed closed" in (t or "") for t in sent)


def test_run_task_fails_closed_when_revoked(tmp_path, monkeypatch):
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()                            # authenticated BEFORE the revoke
    bh.revoke_host(OWNER, HOST)              # revoke drops the binding
    calls = _no_local_spawn(monkeypatch)
    try:
        sent = _run_task(rig, monkeypatch)
    finally:
        rig.disconnect()
    assert calls == [], "revoked binding fell back to a local executor"
    assert rig.created == []
    assert any("failed closed" in (t or "") for t in sent)


def test_run_task_fails_closed_when_host_offline(tmp_path, monkeypatch):
    rig = Rig(tmp_path, [NAV, _OK_RESULT])   # bound, but never connected
    calls = _no_local_spawn(monkeypatch)
    sent = _run_task(rig, monkeypatch)
    assert calls == [], "offline host fell back to a local executor"
    assert rig.created == []
    assert any("failed closed" in (t or "") for t in sent)


def test_run_task_fails_closed_on_registry_fault(tmp_path, monkeypatch):
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    calls = _no_local_spawn(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("registry down")

    monkeypatch.setattr(bh, "binding_for", _boom)
    try:
        sent = _run_task(rig, monkeypatch)
    finally:
        rig.disconnect()
    assert calls == [], "registry fault fell back to a local executor"
    assert any("failed closed" in (t or "") for t in sent)


# ── the direct channel boundary (unchanged) ───────────────────────────

def test_dispatch_requires_an_explicit_binding(tmp_path):
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    bh.unbind_bot(OWNER, BOT)
    rig.connect()
    try:
        with pytest.raises(bh.HostUnavailable):
            rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
        assert rig.created == []
    finally:
        rig.disconnect()


def test_offline_host_fails_closed(tmp_path):
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    with pytest.raises(bh.HostUnavailable):
        rig.manager.dispatch_browser_task(OWNER, BOT, CAND)
    assert rig.created == []


def test_foreign_owner_bot_is_refused(tmp_path):
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    try:
        with pytest.raises(ch.ChannelError):
            rig.manager.dispatch_browser_task("someone-else", BOT, CAND)
        assert rig.created == []
    finally:
        rig.disconnect()


def test_cloud_allowlist_blocks_offlist_before_dispatch(tmp_path):
    rig = Rig(tmp_path, [NAV, _OK_RESULT], cloud_allowlist=("example.com",))
    rig.connect()
    try:
        with pytest.raises(ch.ChannelError):
            rig.manager.dispatch_browser_task(
                OWNER, BOT, '{"url": "https://evil.example.org/"}')
        assert rig.created == []
    finally:
        rig.disconnect()


# ── routine browser tasks use the SAME required-bound-host path ───────

def test_routine_browser_task_fails_closed_when_unbound(tmp_path, monkeypatch):
    """A routine's compiled browser task requires a binding, exactly like a
    task submitted directly."""
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    bh.unbind_bot(OWNER, BOT)
    rig.connect()
    calls = _no_local_spawn(monkeypatch)
    task_text = routines.browser_task_text_for(ROUTINE_STEPS)
    assert task_text                              # a browser prefix exists
    try:
        sent = _run_task(rig, monkeypatch, task_text)
    finally:
        rig.disconnect()
    assert calls == [], "routine browser task fell back to a local executor"
    assert rig.created == []
    assert any("failed closed" in (t or "") for t in sent)


def test_routine_browser_task_dispatches_when_bound(tmp_path, monkeypatch):
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    calls = _no_local_spawn(monkeypatch)
    task_text = routines.browser_task_text_for(ROUTINE_STEPS)
    try:
        sent = _run_task(rig, monkeypatch, task_text)
    finally:
        rig.disconnect()
    assert calls == []
    assert rig.created and rig.created[0].decisions == ["ALLOW"]
    assert any("example.com" in (t or "") for t in sent)


# ── a fail-closed reason IS the durable task's error (not the generic) ─

def _run_browser_task_through_worker(tmp_path, rig, monkeypatch) -> dict:
    """Execute one browser task via the REAL worker + store; return its row."""
    from task_store import CloudTaskStore, TaskWorker

    monkeypatch.setattr(ch, "default_manager", lambda: rig.manager)
    store = CloudTaskStore(db_path=tmp_path / "tasks.db")
    task_id = store.submit(session_key=BOT, task_text=CAND, repo_url=None,
                           executor_prefix="browser", bot_id=BOT,
                           chat_id=OWNER, resolve_bot=True)
    store.register_worker("dispatch-test")
    TaskWorker(store, worker_id="dispatch-test",
               executor=serve.run_task).execute_task(
                   store.claim_next("dispatch-test"))
    return store.get(task_id)


def test_offline_host_failure_keeps_the_real_reason(tmp_path, monkeypatch):
    """Regression: a fail-closed browser task ends with its REAL reason, never
    the generic 'no result produced by executor'."""
    rig = Rig(tmp_path, [NAV, _OK_RESULT])       # bound, but never connected
    row = _run_browser_task_through_worker(tmp_path, rig, monkeypatch)
    assert row["status"] == "failed"
    err = row.get("error") or ""
    assert "no result produced by executor" not in err
    assert "failed closed" in err and "offline" in err


def test_unavailable_host_failure_keeps_the_real_reason(tmp_path, monkeypatch):
    """The channel can be authenticated while the host record is stale: the
    durable error must still name the real fail-closed reason."""
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()                                # authenticated...
    bh.mark_unavailable(HOST)                    # ...but the record went stale
    try:
        row = _run_browser_task_through_worker(tmp_path, rig, monkeypatch)
    finally:
        rig.disconnect()
    assert row["status"] == "failed"
    err = row.get("error") or ""
    assert "no result produced by executor" not in err
    assert "failed closed" in err and "unavailable" in err
