"""Focused tests for configuring a Chat Bot as a writable Developer Bot.

Proves the smallest safe path end to end:

  1. A default Bot (no policy) stays READ-ONLY — it keeps the read-only engine
     path, is not writable, and its effective permissions deny everything.
  2. The named Developer preset routes a Bot-bound Chat turn to the EXISTING
     executor path (submit_bot_task -> CloudTaskStore -> serve.run_task) and
     never requests the read-only engine session; the preset's effective
     permissions are exactly fs:read/repo:read T0, fs:write/repo:pr T1,
     fs:delete/repo:push denied.
  3. Write-class operations still require the EXISTING approval flow: a
     developer Bot's fs.write operation is answered APPROVE by the host (never
     ALLOW, never DENY) and audited as approval_required.
  4. A missing or non-repo Rift fails closed — the configuration is rejected
     (409) and the Bot's policy is left unchanged (still read-only).
  5. Non-owners (another user, or an operator-created Bot) cannot configure a
     Bot (403); anonymous callers get 401.

Run: python3 -m pytest test_bot_dev_config.py
"""

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bot-dev-config-tests")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402  (after env setup)
import audit  # noqa: E402
import chat_service  # noqa: E402
import bots  # noqa: E402
import dev_bot  # noqa: E402
import policy  # noqa: E402
import serve  # noqa: E402


DEVELOPER_POLICY = {"fs:read": 0, "repo:read": 0, "fs:write": 1, "repo:pr": 1}


# ── helpers ────────────────────────────────────────────────────────

def _reset():
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    bots.save_bots({})
    chat_service._engine_sessions.clear()


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"


def teardown_function():
    _reset()


def _plain_dir() -> str:
    """An existing directory that is NOT a git repository."""
    return tempfile.mkdtemp(prefix="kyrex-nonrepo-rift-")


def _git_rift() -> str:
    """A real git repository workspace (a valid Developer Bot Rift)."""
    d = tempfile.mkdtemp(prefix="kyrex-git-rift-")
    subprocess.run(["git", "init", "-q", d], check=True,
                   capture_output=True, text=True)
    return d


def _bot(bot_id="dev", owner="alice", rift=None, policy=None):
    return bots.add_bot(
        bot_id, f"Bot {bot_id}", "anthropic:claude-test",
        rift or _git_rift(), policy=policy, status="stopped", owner=owner,
    )


def _client(user="alice"):
    from fastapi.testclient import TestClient
    return TestClient(main.app, cookies={"session": f"sess-{user}"})


async def _frames(agen):
    out = []
    async for f in agen:
        out.append(f)
    return out


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


# ── 1. default Bot stays read-only ─────────────────────────────────

def test_created_bot_defaults_to_read_only_and_stays_on_engine_path():
    r = _client("alice").post("/api/bots", json={
        "id": "devbot", "name": "DevBot", "model": "gpt-test"})
    assert r.status_code == 200, r.text
    bot = bots.get_bot("devbot")
    assert bot["policy"] == {}                       # no policy was set
    assert dev_bot.is_writable_bot_policy(bot["policy"]) is False
    assert all(v == "deny"
               for v in serve.effective_permissions(bot["policy"]).values())

    conv = chat_service.create_conversation("alice", bot_id="devbot")

    class FakeEngine:
        def __init__(self, user, conversation_id, workspace, bot_cfg=None):
            self.workspace = Path(workspace)

        def run_turn(self, text, on_token, cancel_check=None):
            on_token("read-only answer")
            return "read-only answer", None

        def interrupt(self):
            pass

        def close(self):
            pass

    with patch.object(chat_service, "_get_engine_session", side_effect=FakeEngine), \
         patch.object(chat_service, "_stream_writable_bot_task",
                      side_effect=AssertionError("executor must not be requested")):
        frames = asyncio.run(_frames(chat_service.stream_chat(
            "alice", conv["conversation_id"], "hello")))

    assert _terminal(frames)["status"] == "complete"
    assert _terminal(frames)["content"] == "read-only answer"


# ── 2. Developer preset: effective permissions + executor routing ──

def test_presets_endpoint_exposes_developer_effective_permissions():
    r = _client("alice").get("/api/bots/presets")
    assert r.status_code == 200, r.text
    dev = next(p for p in r.json()["presets"] if p["id"] == "developer")
    perms = dev["permissions"]
    assert perms["fs:read"] == 0
    assert perms["repo:read"] == 0
    assert perms["fs:write"] == 1
    assert perms["repo:pr"] == 1
    # The unsafe operations are denied by default (absent from the policy).
    assert perms["fs:delete"] == "deny"
    assert perms["repo:push"] == "deny"
    # Anonymous callers cannot read the preset surface.
    from fastapi.testclient import TestClient
    assert TestClient(main.app).get("/api/bots/presets").status_code == 401


def test_developer_preset_routes_bot_chat_to_executor():
    _bot("dev", owner="alice", rift=_git_rift())

    r = _client("alice").post("/api/bots/dev/configure",
                              json={"preset": "developer"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["writable"] is True
    assert body["policy"] == DEVELOPER_POLICY
    assert body["permissions"]["fs:write"] == 1
    assert body["permissions"]["fs:delete"] == "deny"

    conv = chat_service.create_conversation("alice", bot_id="dev")

    routed = []

    async def fake_stream(user, conv_, bot_, text, cid, cancel):
        routed.append((user, bot_.get("id"), text))
        yield {"type": "task", "task_id": "task-1", "status": "queued"}
        yield {"type": "status", "status": "complete", "content": "done"}

    with patch.object(chat_service, "_stream_writable_bot_task",
                      side_effect=fake_stream), \
         patch.object(chat_service, "_get_engine_session",
                      side_effect=AssertionError("engine must not be requested")):
        frames = asyncio.run(_frames(chat_service.stream_chat(
            "alice", conv["conversation_id"], "fix it")))

    assert routed == [("alice", "dev", "fix it")]
    assert any(f["type"] == "task" for f in frames)
    assert _terminal(frames)["status"] == "complete"


# ── 3. writes still require the existing approval flow ─────────────

class _FakeProc:
    """Minimal Popen stand-in: canned stdout, captured stdin, instant exit."""

    def __init__(self, stdout_lines):
        self.stdout = list(stdout_lines)
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


def test_developer_write_requires_approval_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    bots.add_bot("dev", "Dev", "test:model", _git_rift(),
                 policy=dev_bot.developer_preset_policy(),
                 status="stopped", owner="alice")

    op_line = json.dumps({"op": "fs.write", "target": "app.py",
                          "summary": "write app.py (42 bytes)"})
    proc = _FakeProc([
        f"KYREX_OPERATION:{op_line}\n",
        'KYREX_RESULT_JSON:{"status":"ok","final_response":"written"}\n',
    ])

    def fake_popen(cmd, **kwargs):
        proc.cmd = list(cmd)
        proc.env = kwargs.get("env")
        return proc

    original_mode = policy.MODE
    policy.MODE = "enforce"
    try:
        with tempfile.TemporaryDirectory() as td:
            audit.AUDIT_FILE = os.path.join(td, "audit.jsonl")
            with patch("serve.subprocess.Popen", side_effect=fake_popen):
                serve.session_lock("dev").acquire()
                try:
                    serve.run_task(
                        "chat", None, "write app.py",
                        executor_prefix="repo",
                        send=lambda *a: 1, edit=lambda *a: None,
                        session_key="dev")
                finally:
                    if serve.session_lock("dev").locked():
                        serve.session_lock("dev").release()
            entries = audit.read_entries()
    finally:
        policy.MODE = original_mode

    written = proc.stdin.getvalue()
    # The host answers APPROVE — the operation routes to a human decision,
    # never an automatic ALLOW and never a flat DENY.
    assert "APPROVE\n" in written, written
    assert "ALLOW\n" not in written, written
    assert "DENY" not in written, written
    # And it is audited as approval-required (the pending human decision).
    assert len(entries) == 1
    assert entries[0]["decision"] == "approval_required"
    assert entries[0]["operation"] == "fs.write"


# ── 4. missing / non-repo Rift fails closed ────────────────────────

def test_developer_preset_requires_a_real_repo_rift():
    # Existing directory that is not a git repository.
    _bot("dev", owner="alice", rift=_plain_dir())
    r = _client("alice").post("/api/bots/dev/configure",
                              json={"preset": "developer"})
    assert r.status_code == 409, r.text
    assert "repository" in r.json()["detail"].lower()
    # The Bot is left untouched: still read-only, never silently writable.
    assert bots.get_bot("dev")["policy"] == {}
    assert dev_bot.is_writable_bot_policy(bots.get_bot("dev")["policy"]) is False

    # A Rift path that does not exist at all.
    missing = str(Path(tempfile.mkdtemp(prefix="kyrex-missing-")) / "gone")
    _bot("lost", owner="alice", rift=missing)
    r2 = _client("alice").post("/api/bots/lost/configure",
                               json={"preset": "developer"})
    assert r2.status_code == 409, r2.text
    assert bots.get_bot("lost")["policy"] == {}


def test_write_policy_on_non_repo_rift_fails_closed():
    _bot("ro", owner="alice", rift=_plain_dir())
    c = _client("alice")

    # A read-only explicit policy is accepted (it does not make the Bot
    # writable, so no repository is required).
    ok = c.post("/api/bots/ro/configure", json={"policy": {"fs:read": 0}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["writable"] is False

    # Granting fs:write explicitly on a non-repo Rift is refused, whatever
    # path the write capability came from.
    bad = c.post("/api/bots/ro/configure", json={"policy": {"fs:write": 1}})
    assert bad.status_code == 409, bad.text
    assert bots.get_bot("ro")["policy"] == {"fs:read": 0}


# ── 5. non-owners cannot configure ─────────────────────────────────

def test_non_owner_cannot_configure_bot():
    _bot("bobs", owner="bob", rift=_git_rift())
    _bot("opbot", owner="", rift=_git_rift())
    c = _client("alice")

    assert c.post("/api/bots/bobs/configure",
                  json={"preset": "developer"}).status_code == 403
    assert c.post("/api/bots/opbot/configure",
                  json={"preset": "developer"}).status_code == 403
    # Neither Bot was modified.
    assert bots.get_bot("bobs")["policy"] == {}
    assert bots.get_bot("opbot")["policy"] == {}


def test_anonymous_cannot_configure_bot():
    _bot("dev", owner="alice", rift=_git_rift())
    from fastapi.testclient import TestClient
    assert TestClient(main.app).post(
        "/api/bots/dev/configure", json={"preset": "developer"}).status_code == 401


# ── 6. explicit configuration validation ───────────────────────────

def test_configure_input_validation():
    _bot("dev", owner="alice", rift=_git_rift())
    c = _client("alice")
    # preset and explicit policy are mutually exclusive.
    assert c.post("/api/bots/dev/configure",
                  json={"preset": "developer", "policy": {}}).status_code == 400
    # unknown preset
    assert c.post("/api/bots/dev/configure",
                  json={"preset": "root"}).status_code == 400
    # malformed policy fails closed
    assert c.post("/api/bots/dev/configure",
                  json={"policy": {"fs:write": "maybe"}}).status_code == 400
    # nothing supplied
    assert c.post("/api/bots/dev/configure", json={}).status_code == 400


def test_configure_sets_prompt_and_model():
    _bot("dev", owner="alice", rift=_git_rift())
    r = _client("alice").post("/api/bots/dev/configure", json={
        "policy": {"fs:read": 0},
        "model": "anthropic:claude-x",
        "system_prompt": "Be terse.",
    })
    assert r.status_code == 200, r.text
    stored = bots.get_bot("dev")
    assert stored["model"] == "anthropic:claude-x"
    assert stored["system_prompt"] == "Be terse."
    assert stored["policy"] == {"fs:read": 0}
