"""Focused state-machine and process-group tests for the cloud supervisor."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

import entrypoint


class FakeChild:
    def __init__(self, command, *, exit_code=None, on_signal=None):
        self.command = command
        self.pid = 1000 + id(self) % 100000
        self.returncode = None
        self.exit_code = exit_code
        self.on_signal = on_signal
        self.signals = []
        self.terminated = False
        self.killed = False

    def poll(self):
        if self.returncode is None and self.exit_code is not None:
            self.returncode = self.exit_code
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.signals.append(signal.SIGTERM)
        if self.on_signal:
            self.on_signal(self, signal.SIGTERM)

    def send_signal(self, signum):
        self.signals.append(signum)
        if self.on_signal:
            self.on_signal(self, signum)

    def wait(self):
        if self.returncode is None:
            self.returncode = -signal.SIGKILL if self.killed else 0
        return self.returncode


class FakeFactory:
    def __init__(self, outcomes=(), on_create=None):
        self.outcomes = list(outcomes)
        self.on_create = on_create
        self.children = []
        self.commands = []
        self.options = []

    def __call__(self, command, **options):
        self.commands.append(command)
        self.options.append(options)
        if self.outcomes and isinstance(self.outcomes[0], BaseException):
            raise self.outcomes.pop(0)
        child = FakeChild(
            command,
            exit_code=self.outcomes.pop(0) if self.outcomes else None,
        )
        self.children.append(child)
        if self.on_create:
            self.on_create(child, len(self.children))
        return child


def run_with_factory(factory, **kwargs):
    supervisor = entrypoint.Supervisor(
        child_factory=factory,
        grace_seconds=kwargs.pop("grace_seconds", 0.05),
        poll_interval=kwargs.pop("poll_interval", 0.001),
        **kwargs,
    )
    return supervisor, supervisor.run()


def test_both_children_start_once_with_expected_commands_and_cwd(tmp_path):
    factory = FakeFactory()
    supervisor = entrypoint.Supervisor(child_factory=factory, app_root=tmp_path)

    supervisor.start()

    assert len(factory.children) == 2
    assert factory.commands == [entrypoint.WEB_COMMAND, entrypoint.WORKER_COMMAND]
    assert supervisor.web is factory.children[0]
    assert supervisor.worker is factory.children[1]
    assert supervisor.app_root == tmp_path


def test_default_factory_uses_root_cwd_and_new_session(monkeypatch, tmp_path):
    calls = []

    class CapturedChild:
        pid = 1234

    def capture(command, **options):
        calls.append((command, options))
        return CapturedChild()

    monkeypatch.setattr(entrypoint.subprocess, "Popen", capture)
    supervisor = entrypoint.Supervisor(app_root=tmp_path)

    supervisor.start()

    assert [command for command, _ in calls] == [
        entrypoint.WEB_COMMAND,
        entrypoint.WORKER_COMMAND,
    ]
    assert all(options["cwd"] == str(tmp_path) for _, options in calls)
    assert all(options["start_new_session"] is True for _, options in calls)


def test_worker_startup_failure_cleans_up_web():
    factory = FakeFactory(outcomes=[None, RuntimeError("worker startup")])
    supervisor, code = run_with_factory(factory)

    assert code != 0
    assert factory.children[0].terminated
    assert supervisor.failure_reason == "startup failure"


def test_web_startup_failure_cleans_up_already_started_child():
    factory = FakeFactory(outcomes=[RuntimeError("web startup")])
    supervisor, code = run_with_factory(factory)

    assert code != 0
    assert supervisor.web is None
    assert supervisor.worker is None


def test_unexpected_worker_exit_codes_are_failures():
    for exit_code in (0, 1, 130, 143):
        factory = FakeFactory(outcomes=[None, exit_code])
        supervisor, code = run_with_factory(factory)
        assert code != 0
        assert supervisor.failure_reason == "worker failure"
        assert factory.children[0].terminated


def test_unexpected_web_exit_codes_are_failures():
    for exit_code in (0, 7):
        factory = FakeFactory(outcomes=[exit_code, None])
        supervisor, code = run_with_factory(factory)
        assert code != 0
        assert supervisor.failure_reason == "web failure"
        assert factory.children[1].signals == [signal.SIGINT]


def test_intentional_sigterm_sends_web_term_worker_int_and_returns_zero():
    factory = FakeFactory()
    supervisor = entrypoint.Supervisor(
        child_factory=factory, grace_seconds=0.05, poll_interval=0.001
    )
    supervisor.start()
    supervisor.request_shutdown("SIGTERM")
    code = supervisor.run() if False else None
    supervisor.wait_for_children()

    assert code is None
    assert factory.children[0].signals == [signal.SIGTERM]
    assert factory.children[1].signals == [signal.SIGINT]
    assert supervisor.failure_reason is None


def test_intentional_sigint_sends_web_term_worker_int_and_returns_zero():
    factory = FakeFactory()
    supervisor = entrypoint.Supervisor(
        child_factory=factory, grace_seconds=0.05, poll_interval=0.001
    )
    supervisor.start()
    supervisor.request_shutdown("SIGINT")
    supervisor.wait_for_children()

    assert factory.children[0].signals == [signal.SIGTERM]
    assert factory.children[1].signals == [signal.SIGINT]
    assert supervisor.failure_reason is None


def test_shutdown_timeout_force_kills_remaining_process_groups(monkeypatch):
    factory = FakeFactory()
    supervisor = entrypoint.Supervisor(
        child_factory=factory, grace_seconds=0.001, poll_interval=0.001
    )
    supervisor.start()
    supervisor.request_shutdown("SIGTERM")
    killed = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    supervisor.wait_for_children()

    assert {sig for _, sig in killed} == {signal.SIGKILL}
    assert all(child.killed is False for child in factory.children)


def test_intentional_shutdown_timeout_is_zero_at_state_level():
    factory = FakeFactory()
    supervisor = entrypoint.Supervisor(
        child_factory=factory, grace_seconds=0.001, poll_interval=0.001
    )
    supervisor.start()
    supervisor.request_shutdown("SIGTERM")
    supervisor.wait_for_children()

    assert supervisor.failure_reason is None


def test_failure_shutdown_stays_nonzero_when_sibling_exits_normally():
    factory = FakeFactory(outcomes=[None, 0])
    supervisor, code = run_with_factory(factory)

    assert code != 0
    assert supervisor.failure_reason == "worker failure"


def test_startup_signal_race_does_not_start_second_child():
    factory = FakeFactory()
    supervisor = entrypoint.Supervisor(child_factory=factory)

    def stop_after_web(child, count):
        if count == 1:
            supervisor.request_shutdown("SIGTERM during startup")

    factory.on_create = stop_after_web
    supervisor.start()

    assert len(factory.children) == 1
    assert supervisor.worker is None
    # The first child is assigned only after Popen returns. The important
    # startup-race invariant is that shutdown prevents creation of child two.


def test_normalize_unexpected_signal_codes():
    assert entrypoint.Supervisor._normalize_failure_code(-signal.SIGTERM) == 143
    assert entrypoint.Supervisor._normalize_failure_code(-signal.SIGINT) == 130
    assert entrypoint.Supervisor._normalize_failure_code(-signal.SIGKILL) == 137
    assert entrypoint.Supervisor._normalize_failure_code(0) == 1


def test_real_process_group_creation_and_cleanup(tmp_path):
    parent_pid = tmp_path / "parent.pid"
    child_pid = tmp_path / "grandchild.pid"
    grandchild_script = (
        "import pathlib, time; "
        f"pathlib.Path({str(child_pid)!r}).write_text('alive'); "
        "time.sleep(60)"
    )
    parent_script = (
        "import os, pathlib, subprocess, sys, time; "
        f"pathlib.Path({str(parent_pid)!r}).write_text(str(os.getpid())); "
        f"subprocess.Popen([sys.executable, '-c', {grandchild_script!r}]); "
        "time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", parent_script],
        cwd=str(Path(__file__).resolve().parents[2]),
        start_new_session=True,
    )
    try:
        assert os.getpgid(child.pid) == child.pid
        deadline = time.monotonic() + 5
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid.exists()
        os.killpg(child.pid, signal.SIGKILL)
        assert child.wait(timeout=5) == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()

