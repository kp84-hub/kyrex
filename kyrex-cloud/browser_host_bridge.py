"""browser_host_bridge.py — durable cross-process Browser Host dispatch bridge.

Why this exists
---------------
The LIVE Browser Host channel (a :class:`~browser_host_channel.HostChannel`)
is an in-memory, process-local object: it is created by the transport that
accepts the host's outbound WebSocket. In production that transport is the
FastAPI web process (``web/backend/main.py`` -> ``browser_host_api``), whose
app lifespan owns the ``HostManager`` and its live channels.

``serve.run_task`` — the single execution path for a browser task — runs in a
SEPARATE worker process (``worker.py``). That process's ``default_manager()``
holds no channels, so calling it directly can never dispatch; it would raise a
misleading "host is offline" even while the host is connected and heart-beating
in the web process. That is a TOPOLOGY fault, not host liveness.

This module is the bridge. It reuses the *existing* durable request/reply
discipline the codebase already uses for approvals
(``task_store.record_operator_reply`` / ``deliver_operator_replies``), but in
reverse:

  * The **worker** (``request_browser_dispatch``) creates an owner-scoped,
    task-bound durable request in the shared store and waits ONLY for its
    terminal durable result (relaying any progress notes as they appear).
  * The **socket-owning web process** (``BrowserDispatchPoller``) claims
    pending requests it can serve (it currently owns a live channel for the
    host), runs the EXISTING dispatch path
    (``HostManager.dispatch_browser_task``), and persists progress + the
    terminal result for the worker.
  * A request whose host this socket owner does NOT own is failed closed with
    an explicit ``BrowserChannelUnavailable`` topology error — never a false
    "offline" for a host whose record says it is online.

Authority, policy, allowlist, managed session, approvals, audit, redaction,
cancellation, timeout, owner isolation, explicit binding, and fail-closed
behaviour are all **unchanged**: the bridge carries a request to the SAME
``dispatch_browser_task`` call the single-process deployment always made.

There is NO Redis, service, bus, or local browser fallback — only the shared
SQLite task store and in-process threads.
"""
from __future__ import annotations

import json
import os
import threading
import time

import browser_host_channel as _channel
import browser_hosts as _hosts

# Poll cadence for both the worker's terminal-result wait and the poller's scan.
POLL_SECONDS = 0.05

# How many requests one poller tick claims/executes (bounded work per tick).
_TICK_LIMIT = 4

_BROWSER_CHANNEL_UNAVAILABLE = "BrowserChannelUnavailable"
HOST_CANCELLED = "HostUnavailable: browser host task cancelled"
HOST_TIMEOUT = ("HostUnavailable: browser host dispatch timed out waiting for "
                "the socket-owning process")

# ── the process's own durable store (set by the worker) ───────────────
#
# The bridge must write the dispatch request into the SAME store (and DB) the
# worker reads results from. TaskWorker.execute_task registers that store here
# so serve.run_task never has to grow a store parameter for callers that lack
# one, and so a bare run_task (no worker) degrades to the co-located fast path.

_active_store = None
_active_store_lock = threading.Lock()


def set_active_store(store) -> None:
    """Register the store this process uses for executor tasks (worker side)."""
    global _active_store
    with _active_store_lock:
        _active_store = store


def get_active_store():
    """The store registered by the current worker, or ``None``."""
    with _active_store_lock:
        return _active_store


def topology_error(host_id, state: str = "unknown") -> str:
    """The explicit TOPOLOGY failure string (never a false "offline")."""
    return (
        f"{_BROWSER_CHANNEL_UNAVAILABLE}: no live browser host channel is owned "
        f"by the dispatching process for host {str(host_id or '')!r} "
        f"(host record: {state})"
    )


def _local_claimant() -> str:
    return f"local-{os.getpid()}"


def _decode_result(raw):
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── worker side ───────────────────────────────────────────────────────

def request_browser_dispatch(*, owner, bot_id, host_id, task_text,
                             task_id: str = "", session_id: str = "",
                             timeout=None, on_progress=None, store=None,
                             profile_bot_id=None):
    """Dispatch a browser task and return ``(result, error)``.

    Runs the EXISTING dispatch path against a live channel when THIS process
    owns one (a co-located deployment, or the single-process tests); otherwise
    records an owner-scoped, task-bound durable request and waits for the
    socket-owning process to fill in the terminal result.

    ``error`` is ``None`` only on success. Every other outcome is a fail-closed
    string — an accurate ``HostUnavailable`` for a genuinely offline/unavailable
    host, or an explicit ``BrowserChannelUnavailable`` topology error.

    ``bot_id`` is always the authorization identity. ``profile_bot_id``
    normally defaults to it; the fixed Level 6 route supplies ``browser-bot``
    so the channel can reuse that persistent profile without borrowing its
    policy. The socket-owning process revalidates the split before dispatch.
    """
    if timeout is None:
        timeout = _channel.DEFAULT_TASK_TIMEOUT
    timeout = max(int(timeout), 1)
    profile_bot_id = str(profile_bot_id or bot_id)
    if store is None:
        store = get_active_store()

    # The durable host record is the SHARED liveness authority. If it is not
    # available, fail closed here with the accurate reason and never create a
    # request — the socket owner has nothing to dispatch to either.
    rec = _hosts.get_host(host_id)
    if rec is None or not rec.is_available():
        state = rec.effective_state() if rec is not None else "offline"
        return None, f"HostUnavailable: browser host {str(host_id)!r} is {state}"

    manager = _channel.default_manager()
    channel = manager.channel_for(host_id)
    owns_channel = channel is not None and channel.authenticated

    if store is None:
        # No durable handoff available: only a co-located live channel can serve
        # this. Never local-fallback to a browser executor — fail closed.
        if not owns_channel:
            return None, topology_error(host_id, rec.effective_state())
        return _dispatch_via_manager(
            manager, owner, bot_id, profile_bot_id, task_text,
            session_id, on_progress)

    dispatch_id = store.submit_browser_dispatch(
        task_id=task_id, owner=owner, bot_id=bot_id, host_id=host_id,
        task_text=task_text, session_id=session_id, timeout=timeout,
        profile_bot_id=profile_bot_id,
    )

    # Co-located fast path: if THIS process owns the live channel, claim the
    # request atomically and execute now. The atomic claim is what keeps a
    # concurrent poller from executing the same request twice.
    if owns_channel and store.claim_one_browser_dispatch(dispatch_id,
                                                         _local_claimant()):
        _execute_and_persist(
            store, dispatch_id, manager, owner, bot_id, profile_bot_id,
            task_text, session_id)

    return _await_terminal(store, dispatch_id, task_id, timeout, on_progress)


def _dispatch_via_manager(manager, owner, bot_id, profile_bot_id, task_text,
                          session_id, on_progress):
    """Call the EXISTING channel dispatch path; fail closed on any error."""
    try:
        result = manager.dispatch_browser_task(
            owner, bot_id, task_text,
            session_id=session_id, on_progress=on_progress,
            profile_bot_id=profile_bot_id,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed, never local
        return None, f"{type(exc).__name__}: {exc}"
    return result, None


def _admissible(store, dispatch_id):
    """Re-read durable status/deadline/cancel state just before dispatch.

    Returns ``(True, "")`` when the request may still be handed to a host.
    The store owns this decision (``browser_dispatch_admissible``) so the
    predicate is identical in every process; a store without the method
    (an older/duck-typed store in a test double) is treated as admissible so
    co-located paths keep working.
    """
    checker = getattr(store, "browser_dispatch_admissible", None)
    if checker is None:
        return True, ""
    try:
        return checker(dispatch_id)
    except Exception as exc:  # noqa: BLE001 — a fault is NOT permission
        return False, f"HostUnavailable: browser dispatch state unreadable: {exc}"


def _execute_and_persist(store, dispatch_id, manager, owner, bot_id,
                         profile_bot_id, task_text, session_id):
    """Execute one claimed request and persist its terminal result.

    Re-checks the durable request IMMEDIATELY before the host dispatch: if the
    request expired, was cancelled, or was already terminalized while this
    process held the claim, the work is NOT handed to a host — it is failed
    closed (conditional, so exactly one terminal write ever lands). This closes
    the claim-vs-timeout / claim-vs-cancellation race.

    Progress is persisted as it streams so the worker can relay it live. The
    terminal write is conditional (``task_store`` enforces a single
    non-terminal -> terminal transition), so exactly one result is recorded.
    """
    allowed, reason = _admissible(store, dispatch_id)
    if not allowed:
        try:
            store.fail_browser_dispatch(dispatch_id, reason)
        except Exception:
            pass
        return None, reason

    def on_progress(note):
        try:
            store.record_browser_dispatch_progress(dispatch_id, note)
        except Exception:
            pass

    result, error = _dispatch_via_manager(
        manager, owner, bot_id, profile_bot_id, task_text, session_id,
        on_progress)
    if error is not None:
        store.fail_browser_dispatch(dispatch_id, error)
    else:
        store.complete_browser_dispatch(dispatch_id, result)
    return result, error


def _await_terminal(store, dispatch_id, task_id, timeout, on_progress):
    """Wait ONLY for the request's terminal durable result (relaying progress).

    Bounded by ``timeout``. Also honours cancellation of the underlying task
    (or of this request) — both fail closed and mark the request terminal so a
    late poller never executes an abandoned dispatch.
    """
    deadline = time.time() + timeout
    cursor = 0
    while True:
        try:
            notes, cursor = store.browser_dispatch_progress(dispatch_id, cursor)
        except Exception:
            notes = []
        for note in notes:
            if on_progress is not None:
                try:
                    on_progress(note)
                except Exception:
                    pass

        rec = store.get_browser_dispatch(dispatch_id)
        if rec is None:
            return None, topology_error("", "unknown")
        status = rec.get("status")
        if status == "done":
            return _decode_result(rec.get("result")), None
        if status == "failed":
            return None, rec.get("error") or "browser host dispatch failed"

        cancelled = bool(rec.get("cancel_requested"))
        if not cancelled and task_id:
            try:
                cancelled = bool(store.is_cancel_requested(task_id))
            except Exception:
                cancelled = False
        if cancelled:
            store.fail_browser_dispatch(dispatch_id, HOST_CANCELLED)
            return None, HOST_CANCELLED

        if time.time() >= deadline:
            store.fail_browser_dispatch(dispatch_id, HOST_TIMEOUT)
            return None, HOST_TIMEOUT

        time.sleep(POLL_SECONDS)


# ── socket-owning (web) side ──────────────────────────────────────────

class BrowserDispatchPoller:
    """Executes durable dispatch requests against the channels THIS process owns.

    Started by the web app's lifespan (the same place that starts the
    ``HostManager``), so the process that accepts the host WebSocket is the
    process that performs the dispatch. A request for a host this process does
    not currently serve is failed closed with an explicit topology error rather
    than being silently dropped or misreported.
    """

    def __init__(self, store, manager, *, claimant=None,
                 poll_interval: float = POLL_SECONDS):
        self.store = store
        self.manager = manager
        self.claimant = claimant or f"web-{os.getpid()}"
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = None

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> "BrowserDispatchPoller":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="browser-dispatch-poller", daemon=True)
        self._thread.start()
        return self

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                self.tick()
            except Exception:  # never let the poller die
                pass

    # ── one scan ─────────────────────────────────────────────────────
    def tick(self, *, limit: int = _TICK_LIMIT) -> int:
        """Claim + execute servable requests; fail closed the rest. Returns count.

        Order matters: ANY request whose deadline has passed is terminalized
        fail-closed FIRST (a claim from a process that died mid-run leaves an
        ``executing`` row that must never be resumed), so the claim scan below
        only ever sees live requests. ``_execute_and_persist`` then re-checks
        the durable row once more immediately before the host dispatch.
        """
        try:
            self.store.expire_browser_dispatches()
        except Exception:
            pass
        owned = self.manager.owned_host_ids()
        ran = 0
        # Requests this socket owner CAN serve: claim only where it owns a live
        # channel for the request's host, then execute the EXISTING path.
        for row in self.store.claim_browser_dispatches(
                self.claimant, host_ids=owned, limit=limit):
            _execute_and_persist(
                self.store, row["dispatch_id"], self.manager,
                row.get("owner"), row.get("bot_id"),
                row.get("profile_bot_id") or row.get("bot_id"),
                row.get("task_text"), row.get("session_id") or "",
            )
            ran += 1
        # Requests this socket owner CANNOT serve (no live channel): fail closed
        # with an explicit TOPOLOGY error so the worker reports the truth
        # instead of timing out or calling an online host "offline".
        for row in self.store.pending_browser_dispatches(
                exclude_host_ids=owned, limit=limit):
            rec = _hosts.get_host(row.get("host_id"))
            state = rec.effective_state() if rec is not None else "unknown"
            self.store.fail_browser_dispatch(
                row["dispatch_id"], topology_error(row.get("host_id"), state))
        return ran


# Process-wide default poller, started/stopped by the web app lifespan.
_default_poller = None
_default_poller_lock = threading.Lock()


def start_dispatch_poller(store, manager, **kwargs) -> BrowserDispatchPoller:
    """Start (idempotently) the process-wide dispatch poller."""
    global _default_poller
    with _default_poller_lock:
        if _default_poller is None:
            _default_poller = BrowserDispatchPoller(store, manager, **kwargs)
        return _default_poller.start()


def stop_dispatch_poller(**kwargs) -> None:
    """Stop the process-wide dispatch poller (idempotent)."""
    global _default_poller
    with _default_poller_lock:
        poller, _default_poller = _default_poller, None
    if poller is not None:
        poller.stop(**kwargs)
