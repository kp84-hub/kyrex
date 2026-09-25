"""Shared connected tools across the owner's Bots.

Regressions for the capability-routing refactor: connected account tools are
OWNER-scoped and SHARED across every Bot the owner owns, instead of being
locked to a mutually exclusive capability-role preset. Bot routing/personas
stay independent of tool availability.

This original slice proves Gmail sharing and coordinator isolation. Calendar
sharing is covered by ``test_shared_calendar_tools.py``. The legacy Calendar
preset policy remains narrow on disk for backward compatibility; that raw
policy is no longer the authority for owner-connected Calendar execution.

Run: python3 -m pytest test_bot_shared_tools.py
"""

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("WEB_SESSION_SECRET", "shared-tools-test")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))      # web/backend
_ROOT = os.path.dirname(os.path.dirname(_BACKEND))         # kyrex-cloud/
for _p in (_BACKEND, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bot_capabilities     # noqa: E402
import bots                 # noqa: E402
import chat_service         # noqa: E402
import connectors           # noqa: E402
import dev_bot              # noqa: E402
import provider_profiles    # noqa: E402
import serve                # noqa: E402
import task_store           # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402

OWNER = "alice"
_PROFILE_ID = "shared-tools-test-profile"

#: The four user-facing roles that must all share the ONE connected Gmail tool.
ROLE_POLICIES = {
    "developer": serve.developer_preset_policy,
    "calendar": serve.calendar_preset_policy,
    "browser": serve.browser_preset_policy,
    "chief-of-staff": serve.coordinator_preset_policy,
}


# ── seams ──────────────────────────────────────────────────────────────

class _FakeGmailRead:
    """A stand-in for ``connectors.GmailRead`` at the owner-scoped seam."""

    def search(self, *, query=None, max_results=10, page_token=None):
        return {"owner": OWNER,
                "messages": [{"owner": OWNER, "id": "m1", "thread_id": "t1"}],
                "next_page_token": ""}

    def message(self, message_id):
        return {"owner": OWNER, "id": str(message_id), "thread_id": "t1",
                "snippet": "hi", "headers": {
                    "Subject": "Shared inbox", "From": "randy@example.com",
                    "Date": "Mon, 1 Jan 2024 00:00:00 +0000"}}


class _FakeStore:
    """The ``connectors.default_store()`` seam: availability + reader."""

    def __init__(self, *, available):
        self._available = bool(available)

    def gmail(self, owner, **kwargs):
        self.owner = owner
        return _FakeGmailRead()

    def gmail_read_available(self, owner, **kwargs):
        self.owner = owner
        return self._available


class _RecordingEngine:
    """A no-op engine session so a non-routed turn completes without a spawn."""

    def __init__(self, user, conversation_id, workspace, bot_cfg=None):
        self.workspace = workspace

    def run_turn(self, text, on_token, cancel_check=None):
        on_token("bot answer")
        return "bot answer", None

    def interrupt(self):
        pass

    def close(self):
        pass


class _RecordingStore:
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
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    raw = CloudTaskStore()
    store = _RecordingStore(raw)
    monkeypatch.setattr(chat_service, "_task_store_instance", store,
                        raising=False)
    chat_service._engine_sessions.clear()
    yield {"store": store, "raw": raw, "tmp": tmp_path}
    chat_service._engine_sessions.clear()


def _ensure_profile(owner):
    existing = provider_profiles.get_profile(owner, _PROFILE_ID)
    models = list(existing["models"]) if existing else ["x"]
    if "x" not in models:
        models.append("x")
    provider_profiles.save_profile(owner, {
        "id": _PROFILE_ID,
        "name": "Shared Tools Test Profile",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": os.environ.get("KYREX_API_KEY") or "sk-test",
        "models": models,
    })
    return _PROFILE_ID


def _register(tmp_path, role, *, status="running"):
    return bots.add_bot(
        f"bot-{role}", f"Bot {role}", "openai:x", str(tmp_path),
        owner=OWNER, status=status, policy=ROLE_POLICIES[role](),
        provider_profile_id=_ensure_profile(OWNER))


async def _frames(agen):
    return [frame async for frame in agen]


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


# ═════════════════════════════════════════════════════════════════════════
# 1. every role shares the SAME connected Gmail read (owner connected)
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("role", list(ROLE_POLICIES))
def test_every_role_routes_gmail_when_owner_connected(rig, monkeypatch, role):
    bot = _register(rig["tmp"], role)
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _FakeStore(available=True))
    assert dev_bot.gmail_route_ready(bot) is True


@pytest.mark.parametrize("role", list(ROLE_POLICIES))
def test_every_role_submits_the_same_gmail_read(rig, monkeypatch, role):
    bot = _register(rig["tmp"], role)
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _FakeStore(available=True))
    dev_bot.submit_gmail_task(OWNER, bot, "gmail: search from:Randy",
                              store=rig["store"])
    subs = rig["store"].submissions
    assert len(subs) == 1, subs
    assert subs[0]["executor_prefix"] == "gmail"
    assert subs[0]["repo_url"] is None
    assert subs[0]["task_text"] == "gmail: search from:Randy"


def test_developer_bot_reads_mail_end_to_end(rig, monkeypatch):
    _register(rig["tmp"], "developer")
    fake_store = _FakeStore(available=True)
    worker = TaskWorker(rig["raw"], worker_id="shared-tools",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(connectors, "default_store", lambda: fake_store):
            recv = chat_service.create_conversation(OWNER, bot_id="bot-developer")
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], "find emails from Randy")))
    finally:
        worker.stop()

    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["executor_prefix"] == "gmail", subs
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    content = terminal["content"] or ""
    assert "Shared inbox" in content
    assert "I found 1 recent email from Randy:" in content


# ═════════════════════════════════════════════════════════════════════════
# 2. every role fails closed when Gmail OAuth is absent
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("role", list(ROLE_POLICIES))
def test_every_role_fails_closed_without_gmail_oauth(rig, monkeypatch, role):
    bot = _register(rig["tmp"], role)
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _FakeStore(available=False))
    assert dev_bot.gmail_route_ready(bot) is False


def test_no_route_without_oauth_submits_nothing(rig, monkeypatch):
    _register(rig["tmp"], "chief-of-staff")
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _FakeStore(available=False))
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    recv = chat_service.create_conversation(OWNER,
                                            bot_id="bot-chief-of-staff")
    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], "find emails from Randy")))
    assert rig["store"].submissions == []
    assert _terminal(frames)["status"] == "complete"


# ═════════════════════════════════════════════════════════════════════════
# 3. coordinator authority remains explicit bot:delegate
# ═════════════════════════════════════════════════════════════════════════

def test_only_chief_of_staff_receives_delegate_tools():
    for role, factory in ROLE_POLICIES.items():
        caps = bot_capabilities.derive_bot_capabilities(factory())
        tools = set(caps["tools"])
        dec = caps["decisions"]["delegate_task"]
        if role == "chief-of-staff":
            assert "delegate_task" in tools
            assert "delegation_status" in tools
            assert dec["effective_tier"] == 0
            assert dec["allowed"] is True
        else:
            assert "delegate_task" not in tools, role
            assert "delegation_status" not in tools, role
            assert dec["allowed"] is False, role


def test_only_chief_of_staff_policy_is_coordinator_capable():
    for role, factory in ROLE_POLICIES.items():
        assert serve.is_coordinator_policy(factory()) is (
            role == "chief-of-staff"), role
        assert serve.coordinator_granted({"policy": factory()}) is (
            role == "chief-of-staff"), role


# ═════════════════════════════════════════════════════════════════════════
# 4. legacy stored Calendar presets remain narrow, but are not connector auth
# ═════════════════════════════════════════════════════════════════════════

def test_legacy_calendar_policy_stays_narrow_for_backcompat():
    # The owner-connected runtime overlay deliberately does NOT mutate stored
    # Bot policies. This keeps old preset detection/migration truthful while
    # ``test_shared_calendar_tools.py`` proves the live Calendar connection is
    # shared independently across running Bots.
    assert serve.cal_list_granted(serve.developer_preset_policy()) is False
    assert serve.cal_list_granted(serve.browser_preset_policy()) is False
    assert serve.cal_list_granted(serve.coordinator_preset_policy()) is False
    assert serve.cal_list_granted(serve.calendar_preset_policy()) is True
