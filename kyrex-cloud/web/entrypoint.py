#!/usr/bin/env python3
"""Supervise exactly one Kyrex web process and one worker process.

The supervisor deliberately does not restart either child internally.  A child
failure tears down the sibling and exits non-zero so the container runtime can
observe the service failure as a unit.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Optional


APP_ROOT = Path(__file__).resolve().parents[2]
WORKER_COMMAND = [sys.executable, "kyrex-cloud/worker.py"]
WEB_COMMAND = [sys.executable, "kyrex-cloud/web/backend/main.py"]
SHUTDOWN_GRACE_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.1


class Supervisor:
    """Own and supervise one web child and one worker child."""

    def __init__(
        self,
        *,
        child_factory=None,
        app_root: Path = APP_ROOT,
        grace_seconds: float = SHUTDOWN_GRACE_SECONDS,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.child_factory = child_factory or self._default_child_factory
        self.app_root = Path(app_root)
        self.grace_seconds = grace_seconds
        self.poll_interval = poll_interval
        self.worker: Optional[subprocess.Popen] = None
        self.web: Optional[subprocess.Popen] = None
        self.shutdown_requested = False
        self.shutdown_reason: Optional[str] = None
        self.failure_reason: Optional[str] = None
        self.failure_code: Optional[int] = None

    def _default_child_factory(self, command: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            command,
            cwd=str(self.app_root),
            start_new_session=True,
        )

    @staticmethod
    def _running(child) -> bool:
        return child is not None and child.poll() is None

    def _start_child(self, command: list[str]):
        return self.child_factory(command)

    def start(self) -> None:
        """Start web first, then worker without waiting for HTTP readiness."""
        web = self._start_child(WEB_COMMAND)
        self.web = web

        # A signal may arrive while the first Popen call is returning. Take
        # ownership of that child before deciding whether child two is allowed.
        if self.shutdown_requested:
            self.request_shutdown(self.shutdown_reason or "signal during startup")
            return

        worker = self._start_child(WORKER_COMMAND)
        self.worker = worker

        if self.shutdown_requested:
            self.request_shutdown(self.shutdown_reason or "signal during startup")

    def _send_web_term(self) -> None:
        if self._running(self.web):
            self.web.terminate()

    def _send_worker_int(self) -> None:
        if self._running(self.worker):
            self.worker.send_signal(signal.SIGINT)

    def request_shutdown(self, reason: str) -> None:
        """Begin intentional shutdown, or preserve an existing failure state."""
        if not self.shutdown_requested:
            self.shutdown_requested = True
            self.shutdown_reason = reason

        self._send_web_term()
        self._send_worker_int()

    def request_failure(self, name: str, code: int) -> None:
        """Record unexpected child failure and shut down the sibling."""
        if self.failure_reason is None:
            self.failure_reason = f"{name} failure"
            self.failure_code = code
        self.request_shutdown(self.failure_reason)

    def _force_kill_group(self, child) -> None:
        if not self._running(child):
            return
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def wait_for_children(self) -> None:
        """Wait boundedly, then kill remaining child process groups and reap."""
        deadline = time.monotonic() + self.grace_seconds
        while time.monotonic() < deadline:
            if not self._running(self.web) and not self._running(self.worker):
                return
            time.sleep(self.poll_interval)

        print(
            "[supervisor] shutdown grace period expired; force-killing "
            "remaining child process groups",
            file=sys.stderr,
            flush=True,
        )
        self._force_kill_group(self.worker)
        self._force_kill_group(self.web)

        for child in (self.worker, self.web):
            if child is not None:
                try:
                    child.wait()
                except ChildProcessError:
                    pass

    @staticmethod
    def _normalize_failure_code(code: Optional[int]) -> int:
        """Return a conventional non-zero exit code for an unexpected child."""
        if code is None:
            return 1
        if code > 0:
            return code
        if code < 0:
            return 128 + (-code)
        # A required child exiting normally is still an unexpected failure.
        return 1

    def _startup_failure(self, exc: BaseException) -> int:
        print(
            f"[supervisor] startup failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        self.failure_reason = "startup failure"
        self.failure_code = 1
        self.request_shutdown("startup failure")
        self.wait_for_children()
        return 1

    def run(self) -> int:
        try:
            self.start()
        except BaseException as exc:
            return self._startup_failure(exc)

        if self.shutdown_requested:
            self.wait_for_children()
            return 1 if self.failure_reason is not None else 0

        while not self.shutdown_requested:
            worker_code = self.worker.poll() if self.worker is not None else 1
            web_code = self.web.poll() if self.web is not None else 1

            if worker_code is not None:
                print(
                    f"[supervisor] worker exited with status {worker_code}",
                    file=sys.stderr,
                    flush=True,
                )
                self.request_failure("worker", worker_code)
                break

            if web_code is not None:
                print(
                    f"[supervisor] web exited with status {web_code}",
                    file=sys.stderr,
                    flush=True,
                )
                self.request_failure("web", web_code)
                break

            time.sleep(self.poll_interval)

        self.wait_for_children()
        if self.failure_reason is not None:
            return self._normalize_failure_code(self.failure_code)
        return 0


def main() -> int:
    supervisor = Supervisor()

    def handle_signal(signum: int, _frame: object) -> None:
        supervisor.request_shutdown(
            f"received {signal.Signals(signum).name}"
        )

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        return supervisor.run()
    except KeyboardInterrupt:
        supervisor.request_shutdown("supervisor KeyboardInterrupt")
        supervisor.wait_for_children()
        return 0 if supervisor.failure_reason is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
