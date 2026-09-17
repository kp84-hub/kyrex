"""browser_host_channel.py — Cloud side of the secure Cloud <-> Host channel.

One :class:`HostChannel` is the Cloud's view of ONE authenticated host
connection. It speaks the newline-delimited JSON frame protocol the host
agent (``browser-host/agent.py``) implements, runs the Cloud's policy for
every browser operation the host proposes, and routes a managed browser task
to the connected host instead of spawning a local executor.

Wire protocol (v1)
------------------
Every frame is ``{"v": 1, "type": str, "id": str|None, "ts": float,
"payload": {...}}``. Direction matters: the HOST dials out, so the Cloud only
ever replies on an existing connection. It never dials the host.

  host -> Cloud  hello {host_id, nonce, proof}
  Cloud -> host  hello_ok {session_id, protocol, heartbeat_interval}
  Cloud -> host  hello_err {reason}            (then the connection closes)
  host -> Cloud  heartbeat {state, load}
  Cloud -> host  task {task_id, owner, bot_id, session_id, task_text, allowlist}
  host -> Cloud  progress {note}
  host -> Cloud  operation {op, target, summary, detail}
  Cloud -> host  verdict {task_id, op_id, decision: ALLOW|APPROVE|DENY}
  host -> Cloud  approval {tier, summary, detail, token}
  Cloud -> host  approval_decision {task_id, approval_id, decision}
  host -> Cloud  result {result}
  host -> Cloud  error {reason}

Authority
---------
The Cloud is the ONLY policy/approval authority. The host proposes an
operation; the Cloud derives the tier from its own table
(``serve.OPERATION_TIERS``), re-checks the Bot's site allowlist, evaluates the
Bot's policy, and replies with the verdict. An operation the Cloud does not
recognise is denied. The host's own allowlist check is defense in depth only.

Approval pause/resume
---------------------
When the Cloud replies ``APPROVE``, the host raises an ``approval`` frame and
parks in its executor's ``readline()``. The Cloud parks in an ``Event.wait()``
until the OWNER resolves it (``resolve_approval``). A timeout resolves as
``DENIED`` — the host stops cleanly, never hangs. This is the same handshake
``serve.run_task`` implements over stdin/stdout, carried over the channel.

Fail-closed
-----------
A task is refused before a single frame is sent when the host is offline,
unavailable (stale heartbeat), or not connected. Nothing is queued for later.

Cross-process topology
----------------------
A live channel is process-local: it is owned by whichever process accepted the
host WebSocket (in production the FastAPI web process). ``serve.run_task`` runs
in the worker process, which owns no channels, so it dispatches through
``browser_host_bridge`` rather than calling its own (empty) manager. When a
process that holds no channel is asked to dispatch, it raises
``browser_hosts.BrowserChannelUnavailable`` — a TOPOLOGY fault, explicitly
distinct from a host that is genuinely offline. There is no local browser
fallback in any process.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

# browser_host_channel.py sits inside kyrex-cloud/ — resolve the Cloud package
# the same way delegation.py / routines.py do, so the EXISTING tier table,
# policy engine, allowlist reader, audit log, and host registry are reused
# rather than re-implemented.
_CLOUD_DIR = Path(__file__).resolve().parent
if str(_CLOUD_DIR) not in sys.path:
    sys.path.insert(0, str(_CLOUD_DIR))

import audit as _audit              # noqa: E402 — append-only audit log
import browser_hosts as _hosts      # noqa: E402 — Cloud host registry
import policy as _policy            # noqa: E402 — the EXISTING policy engine
import serve as _serve              # noqa: E402 — tier table + allowlist authority

PROTOCOL_VERSION = 1

# Cloud-side defaults. The task budget matches serve.TASK_TIMEOUT; the approval
# budget matches serve.APPROVAL_TIMEOUT so the channel and the in-process path
# give the operator the same window.
DEFAULT_TASK_TIMEOUT = int(getattr(_serve, "TASK_TIMEOUT", 1800))
DEFAULT_APPROVAL_TIMEOUT = int(getattr(_serve, "APPROVAL_TIMEOUT", 600))

# The browser executor's dotted operations, in the wire form the host sends.
_BROWSER_OPS = frozenset(
    {"browser.navigate", "browser.read", "browser.click", "browser.screenshot",
     "browser.type", "browser.upload", "browser.download", "browser.submit",
     "browser.delete"}
)

# Operations whose TARGET is a URL that must be re-checked against the Bot's
# allowlist Cloud-side (defense at the authority).
_URL_OPS = frozenset({"browser.navigate", "browser.download"})


class ProtocolError(Exception):
    """A frame could not be decoded or is structurally invalid."""


class ChannelError(Exception):
    """The channel operation is not permitted (fail closed)."""


# ── Frame codec ───────────────────────────────────────────────────────

def frame(type_: str, payload: dict | None = None, *, id: str | None = None,
          ts: float | None = None) -> dict:
    """Build one protocol frame."""
    return {
        "v": PROTOCOL_VERSION,
        "type": str(type_),
        "id": id,
        "ts": time.time() if ts is None else ts,
        "payload": payload or {},
    }


def encode(f: dict) -> str:
    """Serialize a frame to one line of JSON (redacted)."""
    return json.dumps(_hosts.redact_obj(f), sort_keys=True)


def decode(line: str) -> dict:
    """Parse one frame line. Raises :class:`ProtocolError` on anything invalid."""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", "replace")
    text = str(line or "").strip()
    if not text:
        raise ProtocolError("empty frame")
    try:
        parsed = json.loads(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"undecodable frame: {exc}")
    if not isinstance(parsed, dict) or "type" not in parsed:
        raise ProtocolError("a frame must be a JSON object with a 'type'")
    if "payload" in parsed and not isinstance(parsed["payload"], dict):
        raise ProtocolError("a frame 'payload' must be an object")
    return parsed


# ── Cloud-side policy decision ────────────────────────────────────────

def decide_operation(op: str, target, *, allowlist, policy) -> tuple[str, object]:
    """Decide one proposed browser operation. Returns ``(decision, tier)``.

    ``decision`` is ``"ALLOW"``, ``"APPROVE"``, or ``"DENY"``. Fail closed at
    every step: an unknown operation, a target off the Bot's allowlist, or a
    policy with no matching rule all deny.
    """
    op = str(op or "").strip()
    if op not in _serve.KNOWN_OPERATIONS:
        return "DENY", "unknown"

    colon = op.replace(".", ":", 1)
    tier = _serve.derive_host_tier(colon, str(target or ""))
    if tier is None:
        return "DENY", "unknown"

    # Re-check the Bot's site allowlist Cloud-side — the authority's own copy.
    if op in _URL_OPS:
        try:
            import browser_operator as _bo
            allowed, _reason = _bo.domain_allowed(target, allowlist or [])
        except Exception:
            allowed = False
        if not allowed:
            return "DENY", "allowlist"

    try:
        decision = _policy.evaluate(policy or {}, colon, tier)
        effective = _policy.enforce(decision)
    except Exception:
        return "DENY", "policy-error"

    if not isinstance(effective, int):
        return "DENY", "deny"
    if effective == 0:
        return "ALLOW", 0
    return "APPROVE", effective


# ── One in-flight task ────────────────────────────────────────────────

class _TaskRun:
    def __init__(self, task_id, owner, bot_id, *, timeout, on_progress=None):
        self.task_id = task_id
        self.owner = owner
        self.bot_id = bot_id
        self.q: "queue.Queue[dict]" = queue.Queue()
        self.deadline = time.time() + max(int(timeout), 1)
        self.on_progress = on_progress


# ── The channel ───────────────────────────────────────────────────────

class HostChannel:
    """The Cloud's server-side state for one host connection.

    ``send(frame)`` must write one frame to the host (a no-op-safe callable in
    tests). ``handle(frame)`` consumes one inbound frame; it never blocks on an
    approval (the owner resolves asynchronously) so the transport read loop
    stays free to keep delivering frames.
    """

    def __init__(self, send, *, now_fn=time.time, audit_fn=None,
                 task_timeout: int | None = None,
                 approval_timeout: int | None = None):
        self._send_raw = send
        self._now = now_fn
        self._audit = audit_fn
        self._task_timeout = (DEFAULT_TASK_TIMEOUT if task_timeout is None
                              else int(task_timeout))
        self._approval_timeout = (DEFAULT_APPROVAL_TIMEOUT
                                  if approval_timeout is None
                                  else int(approval_timeout))
        self.host_id: str = ""
        self.owner: str = ""
        self.session_id: str = ""
        self.authenticated = False
        self.closed = False
        # Optional callback invoked once, after a successful handshake. The
        # manager uses it to register the channel without the transport having
        # to know about the manager.
        self.on_authenticated = None
        self._task: _TaskRun | None = None
        self._pending: dict[tuple, dict] = {}
        self._lock = threading.Lock()

    # ── outbound ─────────────────────────────────────────────────────

    def _send(self, f: dict) -> None:
        try:
            self._send_raw(f)
        except Exception:
            self.closed = True

    def _log(self, *, operation, decision, outcome, tier="n/a", detail=None,
             bot_id=None) -> None:
        fn = self._audit if self._audit is not None else _audit.log
        try:
            fn(
                bot_id=bot_id or self.host_id or "browser-host",
                operation=operation,
                tier=tier,
                decision=decision,
                outcome=outcome,
                detail=detail or {},
            )
        except Exception:
            pass

    # ── inbound dispatch ─────────────────────────────────────────────

    def handle(self, f: dict) -> None:
        """Consume one inbound frame (called by the transport read loop)."""
        type_ = str(f.get("type") or "")
        payload = f.get("payload") or {}

        if type_ == "hello":
            self._on_hello(payload)
            return

        if not self.authenticated:
            self._send(frame("error", {"reason": "not authenticated"}))
            self.closed = True
            return

        if type_ == "heartbeat":
            _hosts.heartbeat(self.host_id, now=self._now())
            return
        if type_ in ("progress", "operation", "approval", "result", "error"):
            run = self._task
            if run is not None:
                run.q.put(f)
            return
        self._send(frame("error", {"reason": f"unknown frame {type_!r}"}))

    def _on_hello(self, payload: dict) -> None:
        host_id = str(payload.get("host_id") or "")
        nonce = str(payload.get("nonce") or "")
        proof = str(payload.get("proof") or "")
        if not _hosts.verify_proof(host_id, nonce, proof, now=self._now()):
            self._send(frame("hello_err", {"reason": "authentication failed"}))
            self.closed = True
            self._log(operation="browser.host", decision="deny",
                      outcome="auth_failed",
                      detail={"host_id": _hosts.redact_text(host_id)})
            return
        rec = _hosts.get_host(host_id)
        self.host_id = host_id
        self.owner = str(getattr(rec, "owner", "") or "")
        self.session_id = uuid.uuid4().hex
        self.authenticated = True
        _hosts.mark_online(host_id, now=self._now())
        self._send(frame("hello_ok", {
            "host_id": host_id,
            "session_id": self.session_id,
            "protocol": PROTOCOL_VERSION,
            "heartbeat_interval": _hosts.heartbeat_interval(),
        }))
        self._log(operation="browser.host", decision="allow",
                  outcome="online",
                  detail={"host_id": host_id, "session": self.session_id})
        if self.on_authenticated is not None:
            try:
                self.on_authenticated()
            except Exception:
                pass

    def mark_lost(self, *, now: float | None = None) -> None:
        """The transport dropped: mark the host unavailable (fail closed)."""
        if self.host_id:
            _hosts.mark_unavailable(self.host_id,
                                    now=self._now() if now is None else now)
            self._log(operation="browser.host", decision="timeout",
                      outcome="disconnected", detail={"host_id": self.host_id})
        self.closed = True

    # ── task routing ─────────────────────────────────────────────────

    def dispatch_task(self, *, owner, bot_id, task_text, session_id="",
                      allowlist=None, policy=None, on_progress=None,
                      timeout: int | None = None) -> dict:
        """Send one managed browser task and drive it to a terminal result.

        Fail closed when the host is not authenticated or not available: no
        frame is sent and :class:`~browser_hosts.HostUnavailable` is raised.
        """
        if not self.authenticated or self.closed:
            raise _hosts.HostUnavailable("browser host is not connected")
        rec = _hosts.get_host(self.host_id, now=self._now())
        if rec is None or not rec.is_available(self._now()):
            raise _hosts.HostUnavailable(
                f"browser host {self.host_id!r} is unavailable"
            )

        task_id = f"bx-{uuid.uuid4().hex[:16]}"
        run = _TaskRun(task_id, str(owner or ""), str(bot_id or ""),
                       timeout=self._task_timeout if timeout is None else timeout,
                       on_progress=on_progress)
        with self._lock:
            if self._task is not None:
                raise ChannelError("a browser host runs one task at a time")
            self._task = run

        self._log(operation="browser.task", decision="allow",
                  outcome="dispatched",
                  detail={"task_id": task_id, "bot_id": run.bot_id,
                          "owner": run.owner, "host_id": self.host_id})
        self._send(frame("task", {
            "task_id": task_id,
            "owner": run.owner,
            "bot_id": run.bot_id,
            "session_id": str(session_id or ""),
            "task_text": str(task_text or ""),
            "allowlist": list(allowlist or []),
        }))

        try:
            return self._drive(run, allowlist=allowlist, policy=policy)
        finally:
            with self._lock:
                self._task = None

    def _drive(self, run: _TaskRun, *, allowlist, policy) -> dict:
        while True:
            remaining = run.deadline - self._now()
            if remaining <= 0:
                return self._timeout_result(run)
            try:
                f = run.q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            type_ = str(f.get("type") or "")
            payload = f.get("payload") or {}

            if type_ == "progress":
                if run.on_progress is not None:
                    try:
                        run.on_progress(payload.get("note") or payload)
                    except Exception:
                        pass
                continue
            if type_ == "error":
                return _error_result(
                    _hosts.redact_text(payload.get("reason") or "host error")
                )
            if type_ == "result":
                result = payload.get("result") or {}
                if not isinstance(result, dict):
                    result = {"status": "error", "errors": ["malformed result"]}
                self._log(operation="browser.task", decision="allow",
                          outcome=str(result.get("status") or "unknown"),
                          detail={"task_id": run.task_id})
                return result
            if type_ == "operation":
                self._handle_operation(run, payload, allowlist=allowlist,
                                       policy=policy)
                continue
            if type_ == "approval":
                self._handle_approval(run, payload, allowlist=allowlist,
                                      policy=policy)
                continue

    def _handle_operation(self, run: _TaskRun, payload: dict, *, allowlist,
                          policy) -> None:
        op = str(payload.get("op") or "")
        target = payload.get("target") or ""
        decision, tier = decide_operation(
            op, target, allowlist=allowlist, policy=policy
        )
        op_id = uuid.uuid4().hex[:8]
        audit_decision = {"ALLOW": "allow", "APPROVE": "approval_required",
                          "DENY": "deny"}[decision]
        self._log(operation=op or "(missing)", decision=audit_decision,
                  outcome="auto" if decision != "DENY" else "blocked",
                  tier=(f"tier{tier}" if isinstance(tier, int) else str(tier)),
                  detail={"task_id": run.task_id, "target": target,
                          "op_id": op_id})
        self._send(frame("verdict", {
            "task_id": run.task_id,
            "op_id": op_id,
            "decision": decision,
        }))

    def _handle_approval(self, run: _TaskRun, payload: dict, *, allowlist,
                         policy) -> None:
        approval_id = uuid.uuid4().hex[:12]
        tier = payload.get("tier", 2)
        key = (run.owner, run.bot_id)
        event = threading.Event()
        entry = {
            "approval_id": approval_id,
            "task_id": run.task_id,
            "owner": run.owner,
            "bot_id": run.bot_id,
            "tier": tier,
            "summary": _hosts.redact_text(payload.get("summary") or ""),
            "detail": _hosts.redact_text(payload.get("detail") or ""),
            "token": str(payload.get("token") or ""),
            "event": event,
            "decision": None,
        }
        with self._lock:
            self._pending[key] = entry

        got = event.wait(timeout=max(int(self._approval_timeout), 1))
        with self._lock:
            current = self._pending.get(key)
            if current is entry:
                self._pending.pop(key, None)
        decision = entry["decision"] if got else "TIMEOUT"
        resolve = "APPROVED" if decision == "APPROVED" else "DENIED"
        audit_decision = {"APPROVED": "approved", "DENIED": "denied",
                          "TIMEOUT": "timeout"}.get(decision, "denied")
        self._log(operation="browser.approval", decision=audit_decision,
                  outcome="resolved" if got else "timeout",
                  tier=(f"tier{tier}" if isinstance(tier, int) else "tier2"),
                  detail={"task_id": run.task_id,
                          "approval_id": approval_id})
        self._send(frame("approval_decision", {
            "task_id": run.task_id,
            "approval_id": approval_id,
            "decision": resolve,
        }))

    # ── owner-side approval resolution ───────────────────────────────

    def pending_approval(self, owner, bot_id) -> dict | None:
        """The pending approval for ``(owner, bot_id)``, or ``None``.

        Returns a NON-SECRET view: the summary/detail are redacted, and the
        exact-match token is deliberately withheld (it is a challenge the
        owner types back, not state to render).
        """
        with self._lock:
            entry = self._pending.get((str(owner or ""), str(bot_id or "")))
        if entry is None:
            return None
        return {
            "approval_id": entry["approval_id"],
            "task_id": entry["task_id"],
            "owner": entry["owner"],
            "bot_id": entry["bot_id"],
            "tier": entry["tier"],
            "summary": entry["summary"],
            "detail": entry["detail"],
        }

    def resolve_approval(self, owner, bot_id, decision, *,
                         approval_id: str | None = None) -> bool:
        """Resolve the pending approval (owner action). Returns True if one was.

        A decision is accepted only for the SAME ``(owner, bot_id)`` it was
        raised for; a foreign identity resolves nothing.
        """
        key = (str(owner or ""), str(bot_id or ""))
        want = "APPROVED" if str(decision).upper() in ("APPROVED", "ALLOW",
                                                       "YES") else "DENIED"
        with self._lock:
            entry = self._pending.get(key)
            if entry is None:
                return False
            if approval_id and str(approval_id) != entry["approval_id"]:
                return False
            entry["decision"] = want
            entry["event"].set()
        return True

    # ── terminal helpers ─────────────────────────────────────────────

    def _timeout_result(self, run: _TaskRun) -> dict:
        self._log(operation="browser.task", decision="timeout",
                  outcome="timeout", detail={"task_id": run.task_id})
        return _error_result("browser host task timed out")


def _error_result(reason: str) -> dict:
    return {
        "status": "error",
        "final_response": "",
        "browser_artifacts": [],
        "errors": [_hosts.redact_text(reason)],
    }


# ── The manager (connected channels + task routing) ───────────────────

class HostManager:
    """Tracks live host channels and routes managed browser tasks to them.

    The transport (a websocket server) calls :meth:`attach` on a new outbound
    host connection and feeds each inbound frame to ``channel.handle``. The
    rest of the Cloud calls :meth:`dispatch_browser_task`.
    """

    def __init__(self, *, on_progress=None, task_timeout: int | None = None,
                 approval_timeout: int | None = None, audit_fn=None):
        self._channels: dict[str, HostChannel] = {}
        self._lock = threading.Lock()
        self._on_progress = on_progress
        self._task_timeout = task_timeout
        self._approval_timeout = approval_timeout
        self._audit_fn = audit_fn
        # Heartbeat sweeper lifecycle (started by the Cloud app's startup hook,
        # stopped on shutdown). None until start() is called.
        self._sweeper: threading.Thread | None = None
        self._sweeper_stop: threading.Event | None = None

    def attach(self, send) -> HostChannel:
        """Create the channel for a new (unauthenticated) host connection."""
        channel = HostChannel(
            send, task_timeout=self._task_timeout,
            approval_timeout=self._approval_timeout, audit_fn=self._audit_fn,
        )
        channel.on_authenticated = lambda: self.on_authenticated(channel)
        return channel

    def on_authenticated(self, channel: HostChannel) -> HostChannel:
        """Register a channel once it has authenticated."""
        with self._lock:
            self._channels[channel.host_id] = channel
        return channel

    def channel_for(self, host_id: str) -> HostChannel | None:
        with self._lock:
            return self._channels.get(str(host_id or "").strip())

    def owned_host_ids(self) -> list[str]:
        """Host ids this process currently holds a live (authenticated) channel.

        The dispatch bridge uses this to claim ONLY requests it can serve: the
        process that owns the host socket is the process that performs the
        dispatch, so no other process ever fabricates one.
        """
        with self._lock:
            return [
                host_id for host_id, channel in self._channels.items()
                if channel.authenticated and not channel.closed
            ]

    def detach(self, channel: HostChannel) -> None:
        """Drop a channel when its transport closes (host -> unavailable)."""
        channel.mark_lost()
        with self._lock:
            if self._channels.get(channel.host_id) is channel:
                self._channels.pop(channel.host_id, None)

    def sweep(self, *, now: float | None = None) -> list[dict]:
        """Sweep stale hosts; connected-but-stale channels are dropped."""
        swept = _hosts.sweep_stale(now=now)
        for rec in swept:
            ch = self.channel_for(rec.get("host_id"))
            if ch is not None:
                self.detach(ch)
        return swept

    # ── lifecycle ────────────────────────────────────────────────────
    #
    # The Cloud app starts the manager on startup and stops it on shutdown.
    # The background sweeper is what turns a silently-dead host (missed
    # heartbeats, no TCP FIN) into the ``unavailable`` state and drops its
    # channel, so routing to it fails closed instead of hanging.

    def start(self) -> "HostManager":
        """Start the background heartbeat sweeper (idempotent)."""
        if self._sweeper is not None and self._sweeper.is_alive():
            return self
        self._sweeper_stop = threading.Event()
        stop = self._sweeper_stop

        def _loop():
            # Poll at a third of the timeout so a host is swept within one
            # extra poll of going stale, without busy-waiting.
            interval = max(_hosts.heartbeat_timeout() / 3.0, 1.0)
            while not stop.wait(interval):
                try:
                    self.sweep()
                except Exception:  # never let the sweeper die
                    pass

        self._sweeper = threading.Thread(
            target=_loop, name="browser-host-sweeper", daemon=True
        )
        self._sweeper.start()
        return self

    def stop(self, *, timeout: float = 2.0) -> None:
        """Stop the background sweeper (idempotent)."""
        stop = self._sweeper_stop
        if stop is not None:
            stop.set()
        if self._sweeper is not None:
            self._sweeper.join(timeout=timeout)
        self._sweeper = None
        self._sweeper_stop = None

    def dispatch_browser_task(self, owner, bot_id, task_text, *,
                              session_id="", on_progress=None,
                              profile_bot_id=None):
        """Route a managed browser task to the Bot's connected host.

        Resolves ``bot_id`` owner-scoped (its policy + site allowlist are the
        authority), runs the Cloud's own allowlist preflight, then dispatches
        over the host's channel using ``profile_bot_id`` for the binding and
        persistent Chromium profile. The identities may differ only for the
        byte-exact fixed Level 6 task with its exact dedicated grant.
        """
        owner = str(owner or "").strip()
        bot_id = str(bot_id or "").strip()
        profile_bot_id = str(profile_bot_id or bot_id).strip()
        if not owner or not bot_id or not profile_bot_id:
            raise ChannelError("a browser host task requires an owner and a bot id")

        ctx = _serve.build_context(bot_id)
        if str(getattr(ctx, "bot_owner", "") or "").strip() != owner:
            raise ChannelError(f"Bot {bot_id!r} is not owned by you")
        allowlist = list(getattr(ctx, "browser_allowlist", None) or [])
        policy = dict(getattr(ctx, "policy", None) or {})

        if profile_bot_id != bot_id:
            try:
                import level6_weekly as _level6
                split_allowed = (
                    profile_bot_id == _serve.level6_browser_bot_id()
                    and task_text == _level6.weekly_browser_task_spec()
                    and _serve.level6_weekly_granted(policy)
                    and allowlist == _serve.level6_weekly_preset_allowlist()
                )
            except Exception:
                split_allowed = False
            if not split_allowed:
                raise ChannelError(
                    "separate browser profile identity is not permitted"
                )
            profile_ctx = _serve.build_context(profile_bot_id)
            if str(getattr(profile_ctx, "bot_owner", "") or "").strip() != owner:
                raise ChannelError(
                    f"Browser profile Bot {profile_bot_id!r} is not owned by you"
                )

        try:
            import browser_operator as _bo
            allowed, reason = _bo.preflight(task_text, allowlist)
        except Exception as exc:
            raise ChannelError(f"browser operator unavailable: {exc}")
        if not allowed:
            raise ChannelError(f"browser task blocked: {reason}")

        host = _hosts.host_for(owner, profile_bot_id)
        if host is None:
            raise _hosts.HostUnavailable(
                f"no browser host is bound to Bot {profile_bot_id!r}"
            )
        channel = self.channel_for(host.host_id)
        if channel is None or not channel.authenticated:
            # TOPOLOGY, not liveness. This process holds no live channel for the
            # host, but the durable record may still be ``online`` (the host is
            # connected to a DIFFERENT, socket-owning process). Report the fault
            # explicitly rather than misreporting an online host as offline.
            raise _hosts.BrowserChannelUnavailable(
                f"no live browser host channel is owned by the dispatching "
                f"process for host {host.host_id!r} "
                f"(host record: {host.effective_state()})"
            )

        return channel.dispatch_task(
            owner=owner, bot_id=profile_bot_id, task_text=task_text,
            session_id=session_id, allowlist=allowlist, policy=policy,
            on_progress=on_progress or self._on_progress,
        )


# Process-wide manager used by the Cloud service wiring. Tests construct their
# own instance so no state leaks between cases.
_default_manager: HostManager | None = None
_default_lock = threading.Lock()


def default_manager() -> HostManager:
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = HostManager()
        return _default_manager
