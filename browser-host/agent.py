"""agent.py — Kyrex Browser Host Phase-2 outbound Cloud agent.

This is the SEPARATE Phase-2 file the Phase-1 README promised: a Phase-1
machine (which runs only ``profiles.py`` + the operator + compose) can never
reach Cloud by accident, because Cloud connectivity lives HERE and is only
started by running this file.

What it does
------------
The host DIALS OUT to the Cloud over an authenticated websocket and becomes a
worker on that one connection:

  * **authenticate** — it proves possession of its enrollment secret with
    ``HMAC_SHA256(secret, host_id + "." + nonce)``. The secret never travels.
  * **register / heartbeat** — a periodic heartbeat keeps the Cloud's liveness
    view fresh; a dropped connection reconnects with bounded backoff, and each
    reconnect re-runs the handshake (restart recovery).
  * **execute** — on a ``task`` frame it runs the real Browser Operator
    (``browser_operator.py``) as a subprocess, translating the executor's
    ``KYREX_*`` protocol into channel frames, and translating the Cloud's
    ``verdict`` / ``approval_decision`` frames back into the operator's stdin.
    The approval pause/resume is exactly the operator's existing blocking
    ``readline()``, now fed by the channel.
  * **never expose CDP** — it opens NO listening socket. The Chromium DevTools
    endpoint stays on loopback; the agent never emits a CDP URL, and every log
    line and error is redacted for one anyway.

Defense in depth
----------------
Cloud is the policy authority. The host applies ITS OWN allowlist intersection
(``host_allowlist.effective_allowlist``) and refuses to run a task that would
navigate off-list, even if the Cloud said to. And the host rejects ANY command
not received through the authenticated channel: ``_handle_task`` refuses unless
``self._authed`` is set on this connection.

Run:
    KYREX_HOST_ID=... KYREX_HOST_OWNER=... KYREX_HOST_CLOUD_URL=wss://... \\
    KYREX_HOST_ENROLLMENT_SECRET=... python3 agent.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_allowlist  # noqa: E402 — independent host-side safety checks

try:
    import profiles  # noqa: E402 — shared with Phase 1
except Exception:  # pragma: no cover - profiles ships alongside in the image
    profiles = None

SCRIPT_DIR = Path(__file__).resolve().parent

PROTOCOL_VERSION = 1

# Substrings of the host-side browser-task spec whose target URLs must be
# re-checked before the operator is allowed to run.
_URL_ACTIONS = ("navigate", "download")


def frame(type_: str, payload: dict | None = None, *, id: str | None = None,
          ts: float | None = None) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": str(type_),
        "id": id,
        "ts": time.time() if ts is None else ts,
        "payload": payload or {},
    }


def encode(f: dict) -> str:
    return json.dumps(host_allowlist.redact_obj(f), sort_keys=True)


def decode(raw) -> dict:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    parsed = json.loads(str(raw))
    if not isinstance(parsed, dict) or "type" not in parsed:
        raise ValueError("a frame must be a JSON object with a 'type'")
    return parsed


class AgentError(Exception):
    """The agent cannot proceed (fail closed, with a redacted reason)."""


class AuthError(AgentError):
    """The Cloud refused the handshake."""


# ── Config ────────────────────────────────────────────────────────────

@dataclass
class HostConfig:
    host_id: str
    owner: str
    secret: str
    cloud_url: str
    allowlist: list = field(default_factory=list)
    profiles_root: str = ""
    heartbeat_interval: float = 15.0
    operator_script: str = ""
    executable: str = ""

    @classmethod
    def from_env(cls, env=None) -> "HostConfig":
        env = os.environ if env is None else env
        host_id = str(env.get("KYREX_HOST_ID") or "").strip()
        owner = str(env.get("KYREX_HOST_OWNER") or "").strip()
        secret = str(env.get("KYREX_HOST_ENROLLMENT_SECRET") or "").strip()
        cloud_url = str(env.get("KYREX_HOST_CLOUD_URL") or "").strip()
        missing = [name for name, val in (
            ("KYREX_HOST_ID", host_id), ("KYREX_HOST_OWNER", owner),
            ("KYREX_HOST_ENROLLMENT_SECRET", secret),
            ("KYREX_HOST_CLOUD_URL", cloud_url),
        ) if not val]
        if missing:
            raise AgentError("missing required environment: " + ", ".join(missing))
        try:
            interval = float(str(env.get("KYREX_HOST_HEARTBEAT_INTERVAL") or "15"))
        except (TypeError, ValueError):
            interval = 15.0
        return cls(
            host_id=host_id,
            owner=owner,
            secret=secret,
            cloud_url=cloud_url,
            allowlist=host_allowlist.parse_allowlist(env.get("KYREX_HOST_ALLOWLIST")),
            profiles_root=str(env.get("KYREX_BROWSER_PROFILES_ROOT") or "").strip(),
            heartbeat_interval=max(1.0, interval),
            operator_script=str(env.get("KYREX_BROWSER_OPERATOR") or "").strip(),
            executable=str(env.get("KYREX_BROWSER_EXECUTABLE") or "").strip(),
        )

    def proof(self, nonce: str) -> str:
        return hmac.new(
            self.secret.encode(), f"{self.host_id}.{nonce}".encode(),
            hashlib.sha256,
        ).hexdigest()


# ── Connection ────────────────────────────────────────────────────────

class WebSocketConnection:
    """Thin adapter over ``websockets.sync.client`` (lazy import)."""

    def __init__(self, ws):
        self._ws = ws

    def send(self, f: dict) -> None:
        self._ws.send(encode(f))

    def recv(self, timeout: float | None = None) -> dict | None:
        try:
            raw = self._ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        if raw is None:
            raise AgentError("connection closed")
        return decode(raw)

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def default_connect(url: str):
    """Open the outbound websocket. Import only on the real connect path."""
    try:
        from websockets.sync.client import connect
    except Exception as exc:  # pragma: no cover - present in the host image
        raise AgentError(
            f"the 'websockets' package is required to reach Cloud: {exc}"
        )
    return WebSocketConnection(connect(url, open_timeout=20))


# ── Executor (subprocess driver around browser_operator.py) ────────────

class Executor:
    """Interface the agent drives: read protocol frames, write decisions."""

    def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self) -> dict | None:  # pragma: no cover - interface
        raise NotImplementedError

    def send(self, decision: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class SubprocessExecutor(Executor):
    """Runs the real Browser Operator, mirroring serve.run_task's parsing.

    stdout carries ``KYREX_PROGRESS`` / ``KYREX_OPERATION`` / ``KYREX_APPROVAL``
    / ``KYREX_RESULT_JSON`` lines; stdin takes one verdict per operation
    (``ALLOW`` / ``APPROVE`` / ``DENY``) and one decision per approval
    (``APPROVED`` / ``DENIED``). stderr is drained separately so it can never
    corrupt the protocol channel.
    """

    def __init__(self, task_text: str, env: dict, *, script: str):
        self._task_text = task_text
        self._env = env
        self._script = script
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, self._script, "--task", self._task_text],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=self._env,
        )
        import threading
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            sys.stderr.write(host_allowlist.redact_text(line))
        sys.stderr.flush()

    def read(self) -> dict | None:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("KYREX_PROGRESS:"):
                return {"kind": "progress",
                        "note": _loads(line[len("KYREX_PROGRESS:"):])}
            if line.startswith("KYREX_OPERATION:"):
                data = _loads(line[len("KYREX_OPERATION:"):])
                return {"kind": "operation", **data}
            if line.startswith("KYREX_APPROVAL:"):
                data = _loads(line[len("KYREX_APPROVAL:"):])
                return {"kind": "approval", **data}
            if line.startswith("KYREX_RESULT_JSON:"):
                return {"kind": "result",
                        "result": _loads(line[len("KYREX_RESULT_JSON:"):])}
        return None  # EOF

    def send(self, decision: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(f"{decision}\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.wait(timeout=10)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


def _loads(text):
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return {}


# ── The agent ─────────────────────────────────────────────────────────

class HostAgent:
    """The host-side worker: authenticate, heartbeat, execute, reconnect."""

    def __init__(self, config: HostConfig, connect, *,
                 executor_factory=None, sleep=time.sleep, now=time.time,
                 profiles_api=None, stop=None):
        self.config = config
        self._connect = connect
        self._now = now
        self._sleep = sleep
        self._profiles = profiles_api if profiles_api is not None else profiles
        self._executor_factory = executor_factory or self._default_executor
        self._stop = stop if stop is not None else (lambda: False)
        self._authed = False
        self._last_hb = 0.0
        self._session_id = ""

    # ── helpers ──────────────────────────────────────────────────────

    def _operator_script(self) -> str:
        if self.config.operator_script:
            return self.config.operator_script
        return str(SCRIPT_DIR / "browser_operator.py")

    def _default_executor(self, task_text, env):
        return SubprocessExecutor(task_text, env, script=self._operator_script())

    def _profile_dir(self, owner, bot_id) -> str:
        """The isolated persistent profile dir for ``(owner, bot_id)``.

        Fail closed: without both an owner and a bot id there is no isolation
        key and the task must not run.
        """
        owner = str(owner or "").strip()
        bot_id = str(bot_id or "").strip()
        if not owner or not bot_id:
            raise AgentError("a managed task requires an owner and a bot id")
        if self._profiles is None:
            raise AgentError("profile isolation module unavailable")
        path = self._profiles.ensure_profile(
            owner, bot_id, root=(self.config.profiles_root or None)
        )
        return str(path)

    def _maybe_heartbeat(self, conn) -> None:
        if self._now() - self._last_hb >= self.config.heartbeat_interval:
            conn.send(frame("heartbeat", {"state": "idle", "host_id":
                                          self.config.host_id}))
            self._last_hb = self._now()

    def _emit(self, conn, type_, payload) -> None:
        conn.send(frame(type_, payload))

    # ── handshake ────────────────────────────────────────────────────

    def _handshake(self, conn) -> None:
        nonce = secrets.token_hex(16)
        conn.send(frame("hello", {
            "host_id": self.config.host_id,
            "owner": self.config.owner,
            "nonce": nonce,
            "proof": self.config.proof(nonce),
            "protocol": PROTOCOL_VERSION,
        }))
        deadline = self._now() + 30
        while self._now() < deadline:
            f = conn.recv(timeout=min(5, max(deadline - self._now(), 0.1)))
            if f is None:
                continue
            type_ = f.get("type")
            if type_ == "hello_ok":
                payload = f.get("payload") or {}
                self._session_id = str(payload.get("session_id") or "")
                interval = payload.get("heartbeat_interval")
                if isinstance(interval, (int, float)) and interval > 0:
                    self.config.heartbeat_interval = float(interval)
                self._authed = True
                return
            if type_ == "hello_err":
                raise AuthError(
                    host_allowlist.redact_text(
                        (f.get("payload") or {}).get("reason") or "rejected")
                )
        raise AuthError("handshake timed out")

    # ── task execution ───────────────────────────────────────────────

    def _await_decision(self, conn, want, task_id, deadline):
        """Receive frames until one of type *want* for *task_id* arrives.

        Sends heartbeats while waiting so a long approval never lets the Cloud
        mark this host stale. Returns the payload, or ``{}`` on timeout (which
        the caller treats as a deny — fail closed).
        """
        while self._now() < deadline:
            self._maybe_heartbeat(conn)
            f = conn.recv(timeout=min(self.config.heartbeat_interval, 5.0))
            if f is None:
                continue
            if f.get("type") != want:
                continue
            payload = f.get("payload") or {}
            if str(payload.get("task_id") or "") != str(task_id):
                continue
            return payload
        return {}

    def _handle_task(self, conn, payload) -> None:
        # No command is honoured off the authenticated channel.
        if not self._authed:
            return
        task_id = str(payload.get("task_id") or "")
        owner = str(payload.get("owner") or "")
        bot_id = str(payload.get("bot_id") or "")
        task_text = str(payload.get("task_text") or "")
        cloud_allow = payload.get("allowlist") or []

        def fail(reason: str) -> None:
            self._emit(conn, "result", {"task_id": task_id, "result": {
                "status": "error", "final_response": "",
                "browser_artifacts": [],
                "errors": [host_allowlist.redact_text(reason)],
            }})

        # Defense in depth: the HOST's own allowlist, intersected with Cloud's.
        allow = host_allowlist.effective_allowlist(self.config.allowlist,
                                                   cloud_allow)
        ok, reason = host_allowlist.preflight(task_text, allow)
        if not ok:
            fail(f"blocked on host: {reason}")
            return

        try:
            profile_dir = self._profile_dir(owner, bot_id)
        except AgentError as exc:
            fail(str(exc))
            return

        env = os.environ.copy()
        env["KYREX_BOT_ID"] = bot_id
        env["KYREX_BOT_OWNER"] = owner
        env["KYREX_BROWSER_SESSION_DIR"] = profile_dir
        env["KYREX_BROWSER_MANAGED"] = "1"
        env["KYREX_BROWSER_ALLOWLIST"] = json.dumps(allow)
        if self.config.executable:
            env["KYREX_BROWSER_EXECUTABLE"] = self.config.executable

        try:
            executor = self._executor_factory(task_text, env)
            executor.start()
        except Exception as exc:  # noqa: BLE001
            fail(f"could not start browser operator: {type(exc).__name__}")
            return

        deadline = self._now() + 1800
        try:
            while True:
                executor_frame = executor.read()
                if executor_frame is None:
                    break
                kind = str(executor_frame.get("kind") or "")
                if kind == "progress":
                    self._emit(conn, "progress",
                               {"task_id": task_id,
                                "note": host_allowlist.redact_obj(
                                    executor_frame.get("note") or {})})
                elif kind == "operation":
                    self._emit(conn, "operation", {
                        "task_id": task_id,
                        "op": executor_frame.get("op"),
                        "target": executor_frame.get("target"),
                        "summary": host_allowlist.redact_text(
                            executor_frame.get("summary")),
                        "detail": host_allowlist.redact_text(
                            executor_frame.get("detail")),
                    })
                    decision = self._await_decision(
                        conn, "verdict", task_id, deadline).get("decision")
                    # Fail closed: anything but an explicit ALLOW/APPROVE
                    # becomes DENY.
                    executor.send(decision if decision in ("ALLOW", "APPROVE")
                                  else "DENY")
                elif kind == "approval":
                    self._emit(conn, "approval", {
                        "task_id": task_id,
                        "tier": executor_frame.get("tier", 2),
                        "summary": host_allowlist.redact_text(
                            executor_frame.get("summary")),
                        "detail": host_allowlist.redact_text(
                            executor_frame.get("detail")),
                        "token": str(executor_frame.get("token") or ""),
                    })
                    decision = self._await_decision(
                        conn, "approval_decision", task_id,
                        self._now() + 3600).get("decision")
                    executor.send(decision if decision == "APPROVED"
                                  else "DENIED")
                elif kind == "result":
                    self._emit(conn, "result", {
                        "task_id": task_id,
                        "result": host_allowlist.redact_obj(
                            executor_frame.get("result") or {}),
                    })
                    return
        finally:
            executor.stop()

    # ── connection lifecycle ─────────────────────────────────────────

    def run_once(self) -> None:
        """One connection: handshake, then serve until the peer drops."""
        conn = self._connect()
        self._authed = False
        try:
            self._handshake(conn)
            self._last_hb = self._now()
            while not self._stop():
                self._maybe_heartbeat(conn)
                f = conn.recv(timeout=self.config.heartbeat_interval)
                if f is None:
                    continue
                type_ = f.get("type")
                if type_ == "task":
                    self._handle_task(conn, f.get("payload") or {})
                elif type_ == "error":
                    sys.stderr.write(
                        "[agent] channel error: "
                        + host_allowlist.redact_text(
                            (f.get("payload") or {}).get("reason") or "")
                        + "\n"
                    )
                # hello_ok / heartbeat / anything else: ignore.
        finally:
            self._authed = False
            try:
                conn.close()
            except Exception:
                pass

    def run_forever(self, *, max_backoff: float = 30.0) -> None:
        """Reconnect with bounded exponential backoff until stopped.

        Each reconnect re-runs the handshake, which is the restart-recovery
        path: a restarted or re-booted host simply re-registers.
        """
        backoff = 1.0
        while not self._stop():
            try:
                self.run_once()
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001 — never crash the worker
                sys.stderr.write(
                    "[agent] disconnected: "
                    + host_allowlist.redact_text(f"{type(exc).__name__}: {exc}")
                    + "\n"
                )
            if self._stop():
                break
            self._sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


def main() -> None:
    try:
        config = HostConfig.from_env()
    except AgentError as exc:
        print(f"[agent] {exc}", file=sys.stderr)
        raise SystemExit(2)
    agent = HostAgent(config, lambda: default_connect(config.cloud_url))
    agent.run_forever()


if __name__ == "__main__":
    main()
