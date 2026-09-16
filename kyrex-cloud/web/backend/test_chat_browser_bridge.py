"""Chat -> Browser Bot bridge tests.

Proves the three-way selected-Bot route added to chat_service:

  repo Developer Bot  -> the EXISTING repo executor path (unchanged)
  bound Browser Bot   -> a deliberate `read <url>` / `browse <url>` UX that
                         submits ONLY a bounded structured navigate/read
                         operation list as an ordinary durable browser task
                         through dev_bot.submit_browser_task ->
                         CloudTaskStore -> TaskWorker ->
                         serve.run_task(executor_prefix="browser") ->
                         the explicitly bound Browser Host channel
  every other Bot     -> the ordinary read-only engine session (unchanged)

Asserted negatively: no browser tool enters the raw engine, no CDP access,
no local browser fallback (missing binding refuses before submission,
offline host fails closed), and no task is ever submitted on a guess —
ambiguous free-form gets a short usage message and no task row.

Run: python3 -m pytest test_chat_browser_bridge.py
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("WEB_SESSION_SECRET", "chat-browser-bridge-test")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BACKEND)                       # kyrex-cloud/
for _p in (_BACKEND, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_HOST = os.path.join(os.path.dirname(_ROOT), "browser-host")
if os.path.isdir(_HOST) and _HOST not in sys.path:
    sys.path.insert(0, _HOST)

import chat_service        # noqa: E402
import dev_bot             # noqa: E402
import main                # noqa: E402
import serve               # noqa: E402
import bots                # noqa: E402
import browser_hosts as bh  # noqa: E402
from test_browser_host_channel import Rig, NAV, BOT, HOST, OWNER  # noqa: E402

_OK_RESULT = {"kind": "result",
              "result": {"status": "no_changes",
                         "final_response": "example.com"}}

_STEPS = [{"action": "navigate", "url": "https://example.com/"},
          {"action": "read"}]


async def _frames(agen):
    out = []
    async for f in agen:
        out.append(f)
    return out


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


def _last_assistant(user, conversation_id):
    conv = chat_service.get_conversation(user, conversation_id) or {}
    msgs = [m.get("content", "") for m in conv.get("messages", [])
            if m.get("role") == "assistant"]
    return msgs[-1] if msgs else ""


class _RecordingStore:
    """Wraps a CloudTaskStore, recording every submit before delegating."""

    def __init__(self, store):
        self._store = store
        self.submissions = []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        return self._store.submit(**kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """One isolated environment per test: registry, data dir, task store."""
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    from task_store import CloudTaskStore
    raw = CloudTaskStore(db_path=tmp_path / "tasks.db")
    store = _RecordingStore(raw)
    monkeypatch.setattr(chat_service, "_task_store_instance", store,
                        raising=False)
    chat_service._engine_sessions.clear()
    main.sessions["sess"] = OWNER
    yield {"store": store, "tmp": tmp_path,
           "raw": raw, "monkeypatch": monkeypatch}
    chat_service._engine_sessions.clear()


_PROVIDER_ID = "bridge-test-profile"


def _ensure_profile(owner, model):
    import provider_profiles
    provider = "anthropic" if str(model).startswith("anthropic") else "openai"
    bare = str(model).split(":", 1)[1] if ":" in str(model) else str(model)
    existing = provider_profiles.get_profile(owner, _PROVIDER_ID)
    models = list(existing["models"]) if existing else []
    if bare not in models:
        models.append(bare)
    provider_profiles.save_profile(owner, {
        "id": _PROVIDER_ID,
        "name": "Bridge Test Profile",
        "provider": provider,
        "base_url": ("https://api.anthropic.com" if provider == "anthropic"
                     else "https://api.openai.com/v1"),
        "api_key": os.environ.get("KYREX_API_KEY") or "sk-test",
        "models": models,
    })
    return _PROVIDER_ID


def _browser_bot(tmp, *, bot_id=BOT, allowlist=("example.com",),
                 status="running"):
    return bots.add_bot(
        bot_id, "Browser Bot", "anthropic:x", str(tmp),
        owner=OWNER, browser_allowlist=list(allowlist),
        policy={"browser:*": 0}, status=status,
        provider_profile_id=_ensure_profile(OWNER, "anthropic:x"))


def _bind(bot_id=BOT):
    """Enroll the test host (idempotent) and bind the Bot to it."""
    try:
        bh.enroll_host(OWNER, HOST, allowlist=["example.com"])
    except bh.HostError:
        pass
    bh.bind_bot(OWNER, bot_id, HOST)


def _git_rift():
    rift = tempfile.mkdtemp(prefix="kyrex-bridge-rift-")
    subprocess.run(["git", "init", "-q", rift], check=True)
    subprocess.run(["git", "-C", rift, "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", rift, "config", "user.name", "t"],
                   check=True)
    with open(os.path.join(rift, "README.md"), "w") as fh:
        fh.write("x\n")
    subprocess.run(["git", "-C", rift, "add", "-A"], check=True)
    subprocess.run(["git", "-C", rift, "commit", "-qm", "init"], check=True)
    return rift


class _RecordingEngine:
    def __init__(self, user, conversation_id, workspace, bot_cfg=None):
        self.workspace = workspace
        self.bot_id = (bot_cfg or {}).get("bot_id")

    def run_turn(self, text, on_token, cancel_check=None):
        on_token("bot answer")
        return "bot answer", None

    def interrupt(self):
        pass

    def close(self):
        pass


def _must_not_request_engine(*args, **kwargs):
    raise AssertionError("engine session must NOT be requested")


# ═════════════════════════════════════════════════════════════════════════
# 1. correct routing — existing paths byte-identical
# ═════════════════════════════════════════════════════════════════════════

def test_developer_bot_still_routes_to_repo_task(rig, monkeypatch):
    bots.add_bot("dev", "Dev", "anthropic:x", _git_rift(), owner=OWNER,
                 policy=dev_bot.developer_preset_policy(), status="running",
                 provider_profile_id=_ensure_profile(OWNER, "anthropic:x"))
    recv = chat_service.create_conversation(OWNER, bot_id="dev")

    routed = []

    async def fake_stream(user, conv_, bot_, text, cid, cancel, steps=None):
        routed.append((user, bot_.get("id"), cid, steps))
        yield {"type": "status", "status": "complete", "content": "done"}

    monkeypatch.setattr(chat_service, "_stream_writable_bot_task",
                        fake_stream)
    monkeypatch.setattr(chat_service, "_get_engine_session",
                        _must_not_request_engine)

    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], "fix it")))

    assert routed == [(OWNER, "dev", recv["conversation_id"], None)]
    assert _terminal(frames)["status"] == "complete"


def test_ordinary_bot_still_uses_engine(rig, monkeypatch):
    """A read-only Bot with no browser allowlist keeps the engine path."""
    _browser_bot(rig["tmp"], bot_id="plain", allowlist=())
    recv = chat_service.create_conversation(OWNER, bot_id="plain")
    monkeypatch.setattr(chat_service, "_get_engine_session",
                        _RecordingEngine)
    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], "hello")))
    assert _terminal(frames)["status"] == "complete"
    assert _terminal(frames)["content"] == "bot answer"
    assert rig["store"].submissions == []


def test_browser_bot_turn_never_requests_the_engine(rig):
    """A bound Browser Bot NEVER spawns the (browserless) engine session;
    an ambiguous turn answers with usage and submits nothing."""
    _browser_bot(rig["tmp"])
    _bind()
    with patch("chat_service._get_engine_session",
               _must_not_request_engine):
        recv = chat_service.create_conversation(OWNER, bot_id=BOT)
        frames = asyncio.run(_frames(chat_service.stream_chat(
            OWNER, recv["conversation_id"], "whatever you like")))
    terminal = _terminal(frames)
    assert terminal["status"] == "complete", terminal
    assert "`read <url>`" in (terminal["content"] or "")
    assert rig["store"].submissions == []


# ═════════════════════════════════════════════════════════════════════════
# 2. real bound-host read success + durable relay
# ═════════════════════════════════════════════════════════════════════════

def test_read_submits_durable_task_and_relays_result(rig, monkeypatch):
    """`read https://example.com/status` end to end: exactly one durable
    browser task through the REAL bound host agent, result streamed into
    the conversation and appended to the transcript once."""
    from task_store import TaskWorker
    import browser_host_channel as ch

    host_rig = Rig(rig["tmp"], [NAV, _OK_RESULT], register_bot=False)
    host_rig.connect()
    monkeypatch.setattr(ch, "default_manager", lambda: host_rig.manager)
    _browser_bot(rig["tmp"])
    _bind()

    worker = TaskWorker(rig["raw"], worker_id="bridge-test")
    worker.start()
    try:
        recv = chat_service.create_conversation(OWNER, bot_id=BOT)
        frames = asyncio.run(_frames(chat_service.stream_chat(
            OWNER, recv["conversation_id"],
            "read https://example.com/status")))
    finally:
        worker.stop()

    subs = rig["store"].submissions
    assert len(subs) == 1
    sub = subs[0]
    assert sub["executor_prefix"] == "browser"
    assert sub["session_key"] == BOT
    assert sub["repo_url"] is None
    # Compiled text is ONLY the structured navigate/read protocol.
    assert json.loads(sub["task_text"])["actions"] == [
        {"action": "navigate", "url": "https://example.com/status"},
        {"action": "read"},
    ]

    terminal = _terminal(frames)
    assert terminal["status"] == "complete", terminal
    assert "example.com" in (terminal["content"] or "")
    assert "example.com" in _last_assistant(OWNER, recv["conversation_id"])
    # The REAL host executor ran exactly once, ALLOWed as tier-0 read-only.
    assert len(host_rig.created) == 1
    assert host_rig.created[0].decisions == ["ALLOW"]
    assert "browser.task" in host_rig.audit.ops()


def test_browse_parses_and_normalises_bare_host(rig):
    _browser_bot(rig["tmp"])
    _bind()
    steps = dev_bot.parse_browser_request("browse example.com")
    assert [s["action"] for s in steps] == ["navigate", "read"]
    assert steps[0]["url"] == "https://example.com"
    assert bh.binding_for(OWNER, BOT) == HOST


# ═════════════════════════════════════════════════════════════════════════
# 3. structured validation + rejections BEFORE task submission
# ═════════════════════════════════════════════════════════════════════════

def _no_task(rig, text):
    """A turn that must answer with usage and never submit a task."""
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], text)))
    terminal = _terminal(frames)
    assert terminal["status"] == "complete", terminal
    content = terminal["content"] or ""
    assert "read" in content and "browse" in content, content
    assert "not supported" in content, content
    assert _last_assistant(OWNER, recv["conversation_id"]) == content
    assert rig["store"].submissions == [], "no task may be submitted"
    return content


def _browser_only():
    _browser_bot(rig["tmp"])
    _bind()


def test_ambiguous_free_form_gets_usage_and_no_task(rig):
    _browser_bot(rig["tmp"])
    _bind()
    content = _no_task(rig, "go to example.com and log in please")
    assert "not supported" in content


def test_unknown_verb_gets_usage_and_no_task(rig):
    _browser_bot(rig["tmp"])
    _bind()
    assert _no_task(rig, "navigate to example.com").startswith("ambiguous")


def test_url_with_credentials_rejected(rig):
    _browser_bot(rig["tmp"])
    _bind()
    content = _no_task(rig, "read https://user:pass@example.com/")
    assert "credentials" in content


def test_non_http_scheme_rejected(rig):
    _browser_bot(rig["tmp"])
    _bind()
    content = _no_task(rig, "read file:///etc/passwd")
    assert "http(s)" in content


def test_off_allowlist_domain_rejected_before_submission(rig):
    _browser_bot(rig["tmp"])
    _bind()
    with pytest.raises(dev_bot.DevBotError, match="allowlist"):
        dev_bot.submit_browser_task(
            OWNER, chat_service.resolve_bot_for_user(OWNER, BOT),
            [{"action": "navigate", "url": "https://evil.example.org/"},
             {"action": "read"}], store=rig["store"])
    assert rig["store"].submissions == []


def test_empty_allowlist_refused(rig):
    _browser_bot(rig["tmp"], bot_id="noallow", allowlist=())
    _bind("noallow")
    with pytest.raises(dev_bot.DevBotError, match="empty browser allowlist"):
        dev_bot.submit_browser_task(OWNER, bots.get_bot("noallow"),
                                    _STEPS, store=rig["store"])


def test_write_capable_bot_refused_on_browser_path(rig):
    bots.add_bot("devw", "DevW", "anthropic:x", _git_rift(), owner=OWNER,
                 browser_allowlist=["example.com"],
                 policy=dev_bot.developer_preset_policy(),
                 status="running",
                 provider_profile_id=_ensure_profile(OWNER, "anthropic:x"))
    _bind("devw")
    with pytest.raises(dev_bot.DevBotError, match="repo"):
        dev_bot.submit_browser_task(OWNER, bots.get_bot("devw"),
                                    _STEPS, store=rig["store"])


def test_unsupported_action_rejected(rig):
    _browser_bot(rig["tmp"])
    _bind()
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_browser_task(
            OWNER, chat_service.resolve_bot_for_user(OWNER, BOT),
            [{"action": "click", "target": "#login"}],
            store=rig["store"])
    assert rig["store"].submissions == []


def test_paused_bot_refused(rig):
    _browser_bot(rig["tmp"], bot_id="paused", status="stopped")
    with pytest.raises(dev_bot.DevBotError, match="stopped"):
        dev_bot.submit_browser_task(OWNER, bots.get_bot("paused"),
                                    _STEPS, store=rig["store"])


def test_steps_over_limit_rejected(rig):
    _browser_bot(rig["tmp"])
    _bind()
    steps = [_STEPS[0]] + [dict(_STEPS[1])] * 40
    with pytest.raises(dev_bot.DevBotError, match="at most"):
        dev_bot.submit_browser_task(
            OWNER, chat_service.resolve_bot_for_user(OWNER, BOT),
            steps, store=rig["store"])


# ═════════════════════════════════════════════════════════════════════════
# 4. no local fallback: missing binding, offline host, no Popen
# ═════════════════════════════════════════════════════════════════════════

def test_unbound_bot_rejects_before_submission(rig):
    _browser_bot(rig["tmp"])
    Rig(rig["tmp"], [NAV, _OK_RESULT], register_bot=False)
    bh.unbind_bot(OWNER, BOT)
    with pytest.raises(dev_bot.DevBotError, match="no Browser Host"):
        dev_bot.submit_browser_task(
            OWNER, chat_service.resolve_bot_for_user(OWNER, BOT),
            _STEPS, store=rig["store"])
    assert rig["store"].submissions == []


def test_offline_host_fails_closed_never_local(rig, monkeypatch):
    """Bound + allowlisted but the host never connected: the durable task is
    still created (an ordinary task), execution fails closed, and it NEVER
    reaches subprocess.Popen or any host."""
    from task_store import TaskWorker

    Rig(rig["tmp"], [NAV, _OK_RESULT], register_bot=False)
    _browser_bot(rig["tmp"])
    _bind()

    booms = []

    def _boom(*args, **kwargs):
        booms.append(args)
        raise AssertionError("a local browser executor was spawned")

    monkeypatch.setattr("subprocess.Popen", _boom)

    worker = TaskWorker(rig["raw"], worker_id="offline-test")
    worker.start()
    try:
        recv = chat_service.create_conversation(OWNER, bot_id=BOT)
        frames = asyncio.run(_frames(chat_service.stream_chat(
            OWNER, recv["conversation_id"],
            "read https://example.com/status")))
    finally:
        worker.stop()

    assert booms == []
    # Submission itself happened (a durable record); nothing ran anywhere.
    subs = rig["store"].submissions
    assert len(subs) == 1
    assert subs[0]["executor_prefix"] == "browser"

    # Regression: Chat surfaces the SAFE, real fail-closed reason — never the
    # generic "no result produced by executor" placeholder.
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "error", frames
    message = terminal.get("message") or ""
    assert "no result produced by executor" not in message
    assert "failed closed" in message and "offline" in message


# ═════════════════════════════════════════════════════════════════════════
# 5. the raw engine stays browser-free (negative spec pinned)
# ═════════════════════════════════════════════════════════════════════════

def test_chat_engine_tools_never_gain_browser_surface():
    from bot_capabilities import CHAT_HOST_BASE_TOOLS, HOST_GRANTED_TOOLS
    for name in list(CHAT_HOST_BASE_TOOLS) + list(HOST_GRANTED_TOOLS):
        assert "browser" not in name and "browse" not in name
    # Browser executability stays exactly the host tier table it always was.
    assert serve.OPERATION_TIERS.get("browser:navigate") == 0
    assert serve.OPERATION_TIERS.get("browser:read") == 0
    assert serve.OPERATION_TIERS.get("browser:download") == 1
    assert serve.OPERATION_TIERS.get("browser:submit") == 2
    # A production browser task can never reach a local spawn: run_task's
    # browser branch dispatches to the bound host or fails closed BEFORE any
    # executor script path (pinned end to end by test_browser_host_dispatch).
    src = __import__("inspect").getsource(serve.run_task)
    assert "browser_preflight_block" in src
    assert "browser_host_dispatch" in src
