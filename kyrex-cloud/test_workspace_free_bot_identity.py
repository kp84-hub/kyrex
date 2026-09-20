"""Regression tests for owner propagation to workspace-free Bot executors."""

import io
import os
import sys
from unittest.mock import patch

_CLOUD = os.path.dirname(os.path.abspath(__file__))
if _CLOUD not in sys.path:
    sys.path.insert(0, _CLOUD)

import serve  # noqa: E402


class _FakeProc:
    def __init__(self):
        self.stdout = []
        self.stderr = []
        self.stdin = io.StringIO()
        self.returncode = 0
        self.pid = 1

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, *args, **kwargs):
        return 0

    def communicate(self, *args, **kwargs):
        return ("", "")


def test_workspace_free_calendar_writer_exports_registry_owner(monkeypatch):
    """Delegated writers use the Bot owner, never the caller or inherited env."""
    bot = {
        "id": "calendar-writer",
        "owner": "alice",
        "name": "Calendar Writer",
        "rift": None,
        "policy": serve.calendar_writer_preset_policy(),
        "model": "",
        "system_prompt": "",
    }
    monkeypatch.setattr(serve, "resolve_bot", lambda session_key: (
        bot if session_key == "calendar-writer" else None
    ))
    monkeypatch.setenv("KYREX_FS_ROOT", "/must-not-leak")
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    with patch("serve.subprocess.Popen", fake_popen):
        serve.run_task(
            chat_id="mallory",
            repo_url=None,
            task_text=(
                'Create a calendar event titled "Delegated" on '
                "2026-09-21 from 19:30 to 19:45."
            ),
            executor_prefix="cal_write",
            send=lambda *_args: "message-id",
            edit=lambda *_args: None,
            session_key="calendar-writer",
            resolve_bot=True,
        )

    env = captured["env"]
    assert captured["cmd"][1].endswith("calendar_writer_executor.py")
    assert env is not None
    assert env["KYREX_BOT_ID"] == "calendar-writer"
    assert env["KYREX_BOT_OWNER"] == "alice"
    assert "KYREX_FS_ROOT" not in env


def test_unbound_workspace_free_executor_does_not_gain_identity(monkeypatch):
    """An unbound executor still inherits no fabricated Bot identity."""
    monkeypatch.setattr(serve, "resolve_bot", lambda _session_key: None)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    with patch("serve.subprocess.Popen", fake_popen):
        serve.run_task(
            chat_id="alice",
            repo_url=None,
            task_text="plain task",
            executor_prefix="cal_write",
            send=lambda *_args: "message-id",
            edit=lambda *_args: None,
            session_key="not-a-bot",
            resolve_bot=True,
        )

    assert captured["env"] is None
