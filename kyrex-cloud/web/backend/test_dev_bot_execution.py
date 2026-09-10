"""Grok-style developer-Bot routing, first increment.

Focused regression coverage proving the writable-vs-read-only decision and
the routing to the EXISTING executor path — without exercising a live engine:

  1. A writable developer policy is detected (fs:write grant).
  2. Read-only / deny / malformed / non-developer policies stay read-only.
  3. ``submit_bot_task`` binds the Bot's own rift (repo_url=None) and never
     the user's connected repo.
  4. ``submit_bot_task`` refuses a read-only Bot (fail closed).
  5. ``serve.run_task`` keeps a Bot-bound rift writable even when an external
     repo_url is present (no --read-only, no KYREX_READ_ONLY_REPO).
  6. ``serve.run_task`` keeps an unbound external-repo task read-only
     (regression: no weakening of the existing external-repo gate).
  7. The existing headless approval gate is unchanged (edits auto-approved by
     the Phase 0 headless driver; deletions remain fail-closed).

Run: python3 -m pytest test_dev_bot_execution.py
"""

import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

_BACKEND = os.path.dirname(os.path.abspath(__file__))            # web/backend/
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))              # kyrex-cloud/
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import serve  # noqa: E402
import bots  # noqa: E402
import dev_bot  # noqa: E402
from task_store import CloudTaskStore  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────

def _rift(tmp_path) -> str:
    rift = tmp_path / "bot-rift"
    rift.mkdir()
    return str(rift)


def _register(monkeypatch, tmp_path, bot_id, rift, policy):
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "test:model", rift,
        policy=policy, status="stopped",
    )


class _FakeProc:
    """Minimal Popen stand-in: empty output, immediate exit."""

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

    def wait(self, *a, **kw):
        return 0

    def communicate(self, *a, **kw):
        return ("", "")


def _capture_popen(captured):
    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        return _FakeProc()
    return fake_popen


# ── 1-2. policy detection ──────────────────────────────────────────

def test_writable_developer_policy_is_detected():
    assert dev_bot.is_writable_bot_policy({"fs:write": 1}) is True
    # A numeric rule may only raise the host tier; host fs:write tier is 1,
    # so even {"fs:write": 0} is a write grant (effective tier 1).
    assert dev_bot.is_writable_bot_policy({"fs:write": 0}) is True
    assert dev_bot.is_writable_bot_policy({"fs:*": 0}) is True
    assert dev_bot.is_writable_bot_policy({"*": 1}) is True


def test_readonly_policy_remains_readonly():
    assert dev_bot.is_writable_bot_policy({}) is False
    assert dev_bot.is_writable_bot_policy({"fs:read": 0}) is False
    assert dev_bot.is_writable_bot_policy({"fs:write": "deny"}) is False
    assert dev_bot.is_writable_bot_policy({"*": "deny"}) is False
    # Non-developer write grants must not mark a Bot as a coding Bot.
    assert dev_bot.is_writable_bot_policy({"cal:create": 1}) is False
    assert dev_bot.is_writable_bot_policy({"repo:push": 2}) is False
    # Malformed policies fail closed (read-only).
    assert dev_bot.is_writable_bot_policy(None) is False
    assert dev_bot.is_writable_bot_policy("not-a-dict") is False
    assert dev_bot.is_writable_bot_policy({"fs:write": True}) is False


# ── 3-4. task submission ───────────────────────────────────────────

def test_submit_bot_task_binds_bot_rift_not_user_repo(tmp_path, monkeypatch):
    rift = _rift(tmp_path)
    bot = _register(monkeypatch, tmp_path, "dev", rift, {"fs:write": 1})

    store = CloudTaskStore(db_path=str(tmp_path / "cloud_tasks.db"))
    task_id = dev_bot.submit_bot_task("alice", bot, "fix the thing", store=store)

    row = store.get(task_id)
    assert row is not None
    assert row["session_key"] == "dev"
    assert row["bot_id"] == "dev"
    assert row["rift"] == rift
    assert row["repo_url"] is None          # never the user's connected repo
    assert row["executor_prefix"] == "repo"
    assert row["task_text"] == "fix the thing"
    assert row["chat_id"] == "alice"


def test_submit_bot_task_refuses_readonly_bot(tmp_path, monkeypatch):
    rift = _rift(tmp_path)
    bot = _register(monkeypatch, tmp_path, "readonly-dev", rift, {})

    store = CloudTaskStore(db_path=str(tmp_path / "cloud_tasks.db"))
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_bot_task("alice", bot, "fix the thing", store=store)


# ── 5-6. serve.run_task writable/read-only scoping ─────────────────

def test_serve_run_task_bot_rift_writable_even_with_external_repo_url(
    tmp_path, monkeypatch
):
    rift = _rift(tmp_path)
    _register(monkeypatch, tmp_path, "dev", rift, {"fs:write": 1})

    captured = {}
    with patch("serve.subprocess.Popen", _capture_popen(captured)):
        serve.run_task(
            chat_id="alice",
            repo_url="https://github.com/other/other.git",  # external
            task_text="fix the thing",
            executor_prefix="repo",
            send=lambda c, t: 1,
            edit=lambda c, m, t: None,
            session_key="dev",
            resolve_bot=True,
        )

    cmd = captured["cmd"]
    env = captured["env"]
    assert cmd[1].endswith("git_workflow.py")
    assert "--rift" in cmd
    assert cmd[cmd.index("--rift") + 1] == rift
    assert "--repo-url" in cmd          # may seed an empty rift, but...
    assert "--read-only" not in cmd     # ...must never force the rift read-only
    assert env is not None
    assert env.get("KYREX_FS_ROOT") == rift
    assert env.get("KYREX_READ_ONLY_REPO") != "1"


def test_serve_run_task_bot_rift_without_fs_write_stays_readonly(
    tmp_path, monkeypatch
):
    # A Bot with a rift but NO fs:write grant: the rift alone must not grant
    # write capability, so an external repo stays read-only (previous behaviour).
    rift = _rift(tmp_path)
    _register(monkeypatch, tmp_path, "readonly", rift, {})

    captured = {}
    with patch("serve.subprocess.Popen", _capture_popen(captured)):
        serve.run_task(
            chat_id="alice",
            repo_url="https://github.com/other/other.git",  # external
            task_text="fix the thing",
            executor_prefix="repo",
            send=lambda c, t: 1,
            edit=lambda c, m, t: None,
            session_key="readonly",
            resolve_bot=True,
        )

    cmd = captured["cmd"]
    env = captured["env"]
    # The rift is still the workspace location (--rift + KYREX_FS_ROOT)...
    assert "--rift" in cmd
    assert cmd[cmd.index("--rift") + 1] == rift
    assert env is not None
    assert env.get("KYREX_FS_ROOT") == rift
    # ...but it is NOT writable: rift alone grants nothing.
    assert "--read-only" in cmd
    assert env.get("KYREX_READ_ONLY_REPO") == "1"


def test_telegram_bot_path_cannot_become_writable_from_rift_alone(
    tmp_path, monkeypatch
):
    # Mirrors telegram_bot.py: resolve_bot_prefix() sets session_key == bot id,
    # and launch()/serve.run_task() resolve the Bot with resolve_bot=True. A
    # Bot that only has an unrelated capability (cal:create) must NOT become
    # writable on the repo executor just because it has a rift.
    rift = _rift(tmp_path)
    _register(monkeypatch, tmp_path, "dev", rift, {"cal:create": 1})

    captured = {}
    with patch("serve.subprocess.Popen", _capture_popen(captured)):
        serve.run_task(
            chat_id=123456789,
            repo_url="https://github.com/other/other.git",  # external
            task_text="repo: fix the thing",
            executor_prefix="repo",
            send=lambda c, t: 1,
            edit=lambda c, m, t: None,
            session_key="dev",
            resolve_bot=True,  # default on the Telegram @bot path
        )

    cmd = captured["cmd"]
    env = captured["env"]
    assert "--rift" in cmd
    assert cmd[cmd.index("--rift") + 1] == rift
    assert "--read-only" in cmd
    assert env is not None
    assert env.get("KYREX_READ_ONLY_REPO") == "1"


def test_serve_run_task_unbound_external_repo_still_readonly(
    tmp_path, monkeypatch
):
    # Empty registry: no Bot bound.
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    bots.save_bots({})

    captured = {}
    with patch("serve.subprocess.Popen", _capture_popen(captured)):
        serve.run_task(
            chat_id="alice",
            repo_url="https://github.com/other/other.git",  # external
            task_text="do the thing",
            executor_prefix="repo",
            send=lambda c, t: 1,
            edit=lambda c, m, t: None,
            session_key="unbound-session",
            resolve_bot=True,
        )

    cmd = captured["cmd"]
    env = captured["env"]
    assert "--rift" not in cmd
    assert "--read-only" in cmd
    assert env is not None
    assert env.get("KYREX_READ_ONLY_REPO") == "1"


# ── 7. approval gate unchanged ─────────────────────────────────────

def test_headless_approval_gate_unchanged():
    import headless_agent
    # Phase 0 headless driver: file edits are accepted, deletions fail closed.
    assert headless_agent.auto_approve_gate("edit") is True
    assert headless_agent.auto_approve_gate("confirm") is True
    assert headless_agent.auto_approve_gate("deletion") is False
