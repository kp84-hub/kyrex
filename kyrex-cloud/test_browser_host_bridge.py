"""Two-process regression tests for the cross-process Browser Host dispatch bridge.

The LIVE host channel is an in-memory object owned by the socket-accepting
process (production: the FastAPI web app). ``serve.run_task`` runs in the
worker process, which owns no channels. These tests reproduce that topology
FOR REAL: the worker runs in a CHILD PROCESS (``multiprocessing`` fork) with
its OWN CloudTaskStore and its OWN (empty, channel-less) default manager,
while the parent process plays the socket-owning web process — a rig-backed
manager plus its own store instance — with BOTH processes sharing the same
SQLite task store file, the way worker.py and web/backend/main.py share it.

Covered:
  1. two-process dispatch: worker -> durable request -> web claim/execute ->
     terminal result + progress relayed back;
  2. topology failure: the socket owner owns no channel for the host ->
     explicit ``BrowserChannelUnavailable`` (never a false "offline");
  3. offline host: accurate fail-closed before any request exists;
  4. duplicate claim: two claimants racing on one request — exactly one wins;
  5. owner isolation: a foreign owner's request cannot ride another owner's
     bound channel;
  6. cancellation: cancelled task -> the request fails closed (terminal);
  7. result relay exactly-once: re-completing keeps the first result;
  8. expired pending row: never claimed, terminalized fail-closed;
  9. orphaned expired row after a restart: never executed;
 10. cancel/expiry racing IMMEDIATELY after a claim: no host dispatch;
 11. ``browser_dispatch_admissible`` reports every fail-closed state.

Run: python3 -m pytest test_browser_host_bridge.py
"""

import json
import multiprocessing
import os
import sys
import threading
import time

import pytest

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import browser_host_bridge as bridge   # noqa: E402
import bots                            # noqa: E402
import browser_hosts as bh             # noqa: E402
import task_store                      # noqa: E402
from test_browser_host_channel import (  # noqa: E402 — the shared duplex rig
    BOT, HOST, OWNER, CAND, NAV, Rig,
)

_OK_RESULT = {"kind": "result",
              "result": {"status": "no_changes", "final_response": "example.com"}}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_SESSION_SECRET", "bridge-test-secret")
    monkeypatch.delenv("KYREX_PROVIDER_SECRETS_KEY", raising=False)
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    # The bridge's active store is process-local test state: never leak.
    bridge.set_active_store(None)
    yield tmp_path
    bridge.set_active_store(None)


# ── the child (worker) process ─────────────────────────────────────────

def _worker_child(out, db_path, data_dir, owner, bot_id, host_id, task_text,
                  task_id, timeout):
    """Run the worker side of the bridge exactly as production does.

    ``KYREX_DATA_DIR`` points at the SHARED data dir, so the durable host
    registry (enrollment + binding + heartbeat state) is the same FILE the
    parent (web) process reads. The child opens its own store over the same
    SQLite file — the state worker.py's ``build_worker`` setup creates — and
    its default HostManager owns ZERO channels (the production fault the
    bridge exists to fix).
    """
    os.environ["KYREX_DATA_DIR"] = data_dir
    sys.path.insert(0, _CLOUD)
    store = task_store.CloudTaskStore(db_path)
    bridge.set_active_store(store)          # worker.py build_worker step

    progress = []

    def on_progress(note):
        progress.append(note)               # mirrors run_task's message relay

    result, error = bridge.request_browser_dispatch(
        owner=owner, bot_id=bot_id, host_id=host_id, task_text=task_text,
        task_id=task_id, timeout=timeout, on_progress=on_progress, store=store,
    )
    out.put({"result": result, "error": error, "progress": progress})


def _wait_for_payload(out, timeout=25.0):
    """Poll the child's queue (SimpleQueue.get takes no timeout argument)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not out.empty():
            return out.get()
        time.sleep(0.05)
    return {"result": None, "error": "child never reported", "progress": []}


def _run_child_worker(db_path, data_dir, owner, *, task_text=CAND,
                      bot_id=BOT, host_id=HOST, timeout=30,
                      wait=25.0):
    """Start the child worker and return its exit payload."""
    ctx = multiprocessing.get_context("fork")
    out = ctx.SimpleQueue()
    proc = ctx.Process(
        target=_worker_child,
        args=(out, db_path, data_dir, owner, bot_id, host_id, task_text,
              "bridge-%s" % owner, timeout),
        daemon=True,
    )
    proc.start()
    try:
        payload = _wait_for_payload(out, wait)
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
    return payload


def _drive_web_poller(store, manager, *, claimant="web-poller", max_ticks=400):
    """The socket-owning web process executed explicitly.

    Mirrors ``BrowserDispatchPoller.tick``: expire anything past its deadline,
    claim servable requests (hosts WE own), execute them (with the durable
    re-check inside), and fail closed the pending requests we cannot serve.
    """
    executed = []
    for _ in range(max_ticks):
        store.expire_browser_dispatches()
        owned = manager.owned_host_ids()
        claimed = store.claim_browser_dispatches(claimant, host_ids=owned,
                                                 limit=4)
        for row in claimed:
            bridge._execute_and_persist(
                store, row["dispatch_id"], manager,
                row.get("owner"), row.get("bot_id"), row.get("task_text"),
                row.get("session_id") or "")
            executed.append(row["dispatch_id"])
        pending = store.pending_browser_dispatches(exclude_host_ids=owned,
                                                   limit=4)
        for row in pending:
            if row.get("host_id") not in owned:
                rec = bh.get_host(row.get("host_id"))
                state = rec.effective_state() if rec is not None else "unknown"
                store.fail_browser_dispatch(
                    row["dispatch_id"],
                    bridge.topology_error(row.get("host_id"), state))
        if not claimed and not pending:
            if store.pending_browser_dispatches(exclude_host_ids=None,
                                                limit=4) == []:
                break
        time.sleep(0.02)
    return executed


# ── 1. the real two-process path ────────────────────────────────────────

def test_two_process_dispatch_end_to_end(tmp_path):
    """Child worker submits; the parent's web poller executes; result returns."""
    data_dir = str(tmp_path)
    db_path = str(tmp_path / "shared.db")
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    ctx = multiprocessing.get_context("fork")
    out = ctx.SimpleQueue()
    proc = ctx.Process(
        target=_worker_child,
        args=(out, db_path, data_dir, OWNER, BOT, HOST, CAND,
              "bridge-two-process", 30),
        daemon=True,
    )
    executed = []
    try:
        proc.start()
        # Give the child a moment to create the durable request, then run the
        # socket-owning process's poller against the SHARED store.
        child_store = task_store.CloudTaskStore(db_path)
        web_store = task_store.CloudTaskStore(db_path)
        for _ in range(200):
            if child_store.pending_browser_dispatches(
                    exclude_host_ids=None, limit=4):
                break
            time.sleep(0.05)
        for _ in range(4):
            executed += _drive_web_poller(web_store, rig.manager)
            time.sleep(0.05)
        payload = _wait_for_payload(out, 25.0)
    finally:
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
        rig.disconnect()

    assert executed, "the web process never claimed/executed the request"
    assert rig.created, "the host executor never ran"
    assert rig.created[0].decisions == ["ALLOW"]
    assert payload["error"] is None, payload["error"]
    assert payload["result"] == {"status": "no_changes",
                                 "final_response": "example.com"}
    assert isinstance(payload["progress"], list)
    # The request is durable and terminal for everyone inspecting the store.
    store = task_store.CloudTaskStore(db_path)
    assert store.claim_browser_dispatches("late-comer", host_ids=[HOST],
                                          limit=4) == [], \
        "a terminal request must never be claimed again"
    assert store.get_browser_dispatch(executed[0])["status"] == "done"


# ── 2. topology failure is explicit, never a false offline ────────────

def test_topology_error_when_this_process_has_no_channel(tmp_path):
    """Host record ONLINE + no owned channel => BrowserChannelUnavailable.

    The socket-owning poller does the failing: its scan sees the pending
    request for a host it holds NO channel for and fails it closed with the
    explicit topology error while the DURABLE RECORD still says ``online``.
    """
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()   # marks the host ONLINE in the durable registry
    import browser_host_channel as _ch
    empty_manager = _ch.HostManager()          # owns NO channels
    try:
        store = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
        bridge.set_active_store(store)
        outcome = {}

        def _worker():
            outcome["r"], outcome["e"] = bridge.request_browser_dispatch(
                owner=OWNER, bot_id=BOT, host_id=HOST, task_text=CAND,
                task_id="topology-test", timeout=10, store=store,
            )

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        poller = bridge.BrowserDispatchPoller(store, empty_manager,
                                              claimant="web-without-channel",
                                              poll_interval=0.01)
        for _ in range(50):
            if poller.tick():
                break
            time.sleep(0.05)
        thread.join(timeout=15)
        result, error = outcome.get("r"), outcome.get("e")
        assert result is None
        assert error is not None
        assert error.startswith("BrowserChannelUnavailable"), error
        assert "online" in error, "host record state missing from the report"
        assert "is offline" not in error, "topology misreported as offline"
    finally:
        rig.disconnect()


def test_offline_host_fails_accurately_with_no_request(tmp_path):
    """A genuinely offline host fails closed BEFORE any durable request."""
    Rig(tmp_path, [])  # enrollment + binding only; host never connects
    store = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
    result, error = bridge.request_browser_dispatch(
        owner=OWNER, bot_id=BOT, host_id=HOST, task_text=CAND,
        task_id="offline-test", timeout=1, store=store,
    )
    assert result is None and error is not None
    assert error.startswith("HostUnavailable:"), error
    assert "BrowserChannelUnavailable" not in error
    assert store.pending_browser_dispatches(exclude_host_ids=[], limit=4) == []


# ── 4. duplicate claim: exactly one claimant wins ──────────────────────

def test_duplicate_claim_across_processes(tmp_path):
    """Two web processes racing — the atomic claim lets exactly one through."""
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    try:
        store_a = task_store.CloudTaskStore(str(tmp_path / "a.db"))
        store_b = task_store.CloudTaskStore(str(tmp_path / "a.db"))
        dispatch_a = store_a.submit_browser_dispatch(
            task_id="dup-claim", owner=OWNER, bot_id=BOT, host_id=HOST,
            task_text=CAND, timeout=60,
        )
        first = store_a.claim_browser_dispatches(
            "web-A", host_ids=[HOST], limit=4)
        second = store_b.claim_browser_dispatches(
            "web-B", host_ids=[HOST], limit=4)
        assert len(first) == 1
        assert first[0]["dispatch_id"] == dispatch_a
        assert second == []
        assert store_b.pending_browser_dispatches(exclude_host_ids=[],
                                                  limit=4) == []
    finally:
        rig.disconnect()


# ── 5. owner isolation end to end ───────────────────────────────────────

def test_foreign_owner_request_is_refused_on_the_bound_channel(tmp_path):
    """A request whose owner does not own the binding fails closed."""
    data_dir = str(tmp_path)
    db_path = str(tmp_path / "shared.db")
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    try:
        payload = _run_child_worker(db_path, data_dir, "intruder",
                                    timeout=15, wait=20.0)
    finally:
        rig.disconnect()
    assert payload is not None
    assert payload["result"] is None
    assert payload["error"] is not None
    assert rig.created == [], "another owner's request reached the host"


# ── 6. cancellation: fail closed, terminal for late pollers ────────────

def test_cancelled_task_fails_the_request_closed(tmp_path):
    """A cancelled underlying task cancels the in-flight dispatch request."""
    Rig(tmp_path, [])                      # enrollment + binding only
    bh.mark_online(HOST)                   # durable record: available
    store = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
    bridge.set_active_store(store)
    result, error = None, None

    def _worker():
        nonlocal result, error
        result, error = bridge.request_browser_dispatch(
            owner=OWNER, bot_id=BOT, host_id=HOST, task_text=CAND,
            task_id="cancel-me", timeout=30, store=store,
        )

    # The underlying durable task exists (the worker created it), so
    # request_cancel performs the same operator cancellation as the API.
    store.submit(session_key="cancel-me", task_text=CAND, chat_id=OWNER,
                 task_id="cancel-me", resolve_bot=False)
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    for _ in range(100):
        if store.pending_browser_dispatches(exclude_host_ids=[], limit=4):
            break
        time.sleep(0.02)
    assert store.request_cancel("cancel-me")
    thread.join(timeout=10)
    assert result is None
    assert "cancelled" in (error or "").lower(), error
    pending = store.pending_browser_dispatches(exclude_host_ids=[], limit=4)
    assert pending == [], "a failed request must not be claimable again"


# ── 7. the terminal result is recorded exactly once ────────────────────

def test_result_relay_is_exactly_once(tmp_path):
    """Re-completing a terminal request leaves the FIRST result in place."""
    store = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
    dispatch_id = store.submit_browser_dispatch(
        task_id="once-only", owner=OWNER, bot_id=BOT, host_id=HOST,
        task_text=CAND, timeout=60,
    )
    assert store.complete_browser_dispatch(
        dispatch_id, {"status": "no_changes", "final_response": "first"})
    assert not store.complete_browser_dispatch(
        dispatch_id, {"status": "no_changes", "final_response": "second"})
    row = store.get_browser_dispatch(dispatch_id)
    assert json.loads(row["result"])["final_response"] == "first"
    # A terminal request can never be re-completed: the closed attempt already
    # finalized the state, exactly once either way.
    assert not store.complete_browser_dispatch(
        dispatch_id, {"status": "no_changes", "final_response": "late"})


# ── 8. deadline-aware claiming ─────────────────────────────────────────

def test_expired_pending_row_is_never_claimed(tmp_path):
    """A pending request past its deadline is unclaimable and terminalized."""
    store = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
    dispatch_id = store.submit_browser_dispatch(
        task_id="expired-pending", owner=OWNER, bot_id=BOT, host_id=HOST,
        task_text=CAND, timeout=1,
    )
    time.sleep(1.2)                      # the deadline has now passed
    # Deadline-aware claim INSIDE the atomic transaction: never selected.
    assert store.claim_browser_dispatches("web-A", host_ids=[HOST],
                                          limit=4) == []
    assert store.claim_one_browser_dispatch(dispatch_id, "web-A") is False
    # The sweep terminalizes it fail-closed, exactly once.
    assert store.expire_browser_dispatches() == 1
    assert store.expire_browser_dispatches() == 0     # idempotent
    row = store.get_browser_dispatch(dispatch_id)
    assert row["status"] == "failed"
    assert row["error"] == task_store.BROWSER_DISPATCH_EXPIRED


def test_orphaned_expired_row_after_restart_is_never_executed(tmp_path):
    """A claimed-then-orphaned (dead owner) expired request stays unexecuted."""
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    db_path = str(tmp_path / "shared.db")
    try:
        crashed = task_store.CloudTaskStore(db_path)
        dispatch_id = crashed.submit_browser_dispatch(
            task_id="orphan-expired", owner=OWNER, bot_id=BOT, host_id=HOST,
            task_text=CAND, timeout=1,
        )
        # The socket owner claims it and then DIES before executing.
        assert crashed.claim_one_browser_dispatch(dispatch_id, "web-dead")
        time.sleep(1.2)
        # A restart opens a NEW store over the same file (the dead claimer's
        # in-memory state is gone; only the durable row survives).
        restarted = task_store.CloudTaskStore(db_path)
        poller = bridge.BrowserDispatchPoller(restarted, rig.manager,
                                              claimant="web-restarted",
                                              poll_interval=0.01)
        poller.tick()          # sweep terminalizes; claim finds nothing
        assert rig.created == [], "an orphaned expired request reached a host"
        row = restarted.get_browser_dispatch(dispatch_id)
        assert row["status"] == "failed"
        assert row["error"] == task_store.BROWSER_DISPATCH_EXPIRED
    finally:
        rig.disconnect()


def test_cancel_racing_immediately_after_claim_prevents_dispatch(tmp_path):
    """A cancellation landing right after the claim stops the host dispatch."""
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    try:
        store = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
        store.submit(session_key="race-cancel", task_text=CAND, chat_id=OWNER,
                     task_id="race-cancel", resolve_bot=False)
        dispatch_id = store.submit_browser_dispatch(
            task_id="race-cancel", owner=OWNER, bot_id=BOT, host_id=HOST,
            task_text=CAND, timeout=60,
        )
        # Exactly the poller's sequence: claim, then the operator cancels
        # BEFORE the durable re-check / host dispatch.
        assert store.claim_one_browser_dispatch(dispatch_id, "web-race")
        assert store.request_cancel("race-cancel")
        result, error = bridge._execute_and_persist(
            store, dispatch_id, rig.manager, OWNER, BOT, CAND, "")
        assert result is None
        assert error == task_store.BROWSER_DISPATCH_CANCELLED, error
        assert rig.created == [], "cancelled work reached the host"
        row = store.get_browser_dispatch(dispatch_id)
        assert row["status"] == "failed"
        assert row["error"] == task_store.BROWSER_DISPATCH_CANCELLED
    finally:
        rig.disconnect()


def test_expiring_race_immediately_after_claim_prevents_dispatch(tmp_path):
    """A deadline passing right after the claim stops the host dispatch."""
    rig = Rig(tmp_path, [NAV, _OK_RESULT])
    rig.connect()
    try:
        store = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
        dispatch_id = store.submit_browser_dispatch(
            task_id="race-timeout", owner=OWNER, bot_id=BOT, host_id=HOST,
            task_text=CAND, timeout=1,
        )
        assert store.claim_one_browser_dispatch(dispatch_id, "web-race")
        time.sleep(1.2)
        result, error = bridge._execute_and_persist(
            store, dispatch_id, rig.manager, OWNER, BOT, CAND, "")
        assert result is None
        assert error == task_store.BROWSER_DISPATCH_EXPIRED, error
        assert rig.created == [], "expired work reached the host"
    finally:
        rig.disconnect()


def test_admissible_helper_reports_each_fail_closed_state(tmp_path):
    """Non-terminal, unexpired, uncancelled is the ONLY admissible state."""
    store = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
    live = store.submit_browser_dispatch(
        task_id="adm-live", owner=OWNER, bot_id=BOT, host_id=HOST,
        task_text=CAND, timeout=60,
    )
    assert store.browser_dispatch_admissible(live) == (True, "")
    store.complete_browser_dispatch(live, {"ok": True})
    ok, reason = store.browser_dispatch_admissible(live)
    assert not ok and "terminal" in reason

    gone = store.submit_browser_dispatch(
        task_id="adm-gone", owner=OWNER, bot_id=BOT, host_id=HOST,
        task_text=CAND, timeout=1,
    )
    time.sleep(1.2)
    ok, reason = store.browser_dispatch_admissible(gone)
    assert not ok and reason == task_store.BROWSER_DISPATCH_EXPIRED

    missing_ok, missing_reason = store.browser_dispatch_admissible("bd-nope")
    assert not missing_ok and "not found" in missing_reason


# ── progress relay through the durable store ───────────────────────────

def test_progress_notes_are_relayed_from_store(tmp_path):
    """The worker reads progress written by the socket-owning process."""
    store_a = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
    store_b = task_store.CloudTaskStore(str(tmp_path / "shared.db"))
    dispatch_id = store_a.submit_browser_dispatch(
        task_id="progress-1", owner=OWNER, bot_id=BOT, host_id=HOST,
        task_text=CAND, timeout=60,
    )
    store_b.record_browser_dispatch_progress(
        dispatch_id, {"browser.task": "allow"})
    store_b.record_browser_dispatch_progress(
        dispatch_id, {"browser.task": "visited example.com"})
    notes, cursor = store_a.browser_dispatch_progress(dispatch_id, 0)
    assert len(notes) == 2
    notes2, _ = store_a.browser_dispatch_progress(dispatch_id, 1)
    assert len(notes2) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
