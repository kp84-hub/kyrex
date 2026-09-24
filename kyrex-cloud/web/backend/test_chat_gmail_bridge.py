"""Chat -> Gmail Reader bridge tests.

Proves the selected-Bot route added to chat_service for the bounded, READ-ONLY
Gmail mail surface:

    running, non-write-capable Bot holding the EXACT host ``mail:read`` grant
      + natural-language mail-shaped text ("find emails from Randy",
        "search my email for Tesla", "show the subject/from/date of this
        message")
      -> ONE of the two canonical commands, mapped DETERMINISTICALLY
         (``gmail: search [<query>]`` / ``gmail: message <id>``)
      -> the durable gmail task
         (dev_bot.submit_gmail_task -> CloudTaskStore -> TaskWorker ->
          serve.run_task(executor_prefix="gmail"))
      -> the in-process reader against the OWNER-SCOPED encrypted connector
         store (``connectors.default_store().gmail(owner)``) which re-checks
         the granted ``gmail.readonly`` scope
      -> the safe Subject/From/Date projection (or the explicit fail-closed
         error) relayed into Kyrex Chat.

Asserted negatively: the gmail path NEVER submits a writable repo/browser/
calendar task; a mail ACTION (send/delete/archive/label), a request with no
mail object, a foreign owner, a stopped Bot, a missing/wildcard grant, and an
unbounded/malformed request all fail closed (no task at all, and the ordinary
engine path is used where a route is simply absent). No token, raw body, or
non-whitelisted header ever reaches the relayed text.

Run: python3 -m pytest test_chat_gmail_bridge.py
"""

import asyncio
import inspect
import os
import sys
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("WEB_SESSION_SECRET", "chat-gmail-bridge-test")
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")

_BACKEND = os.path.dirname(os.path.abspath(__file__))      # web/backend
_ROOT = os.path.dirname(os.path.dirname(_BACKEND))         # kyrex-cloud/
for _p in (_BACKEND, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bots              # noqa: E402
import chat_service      # noqa: E402
import connectors        # noqa: E402
import dev_bot           # noqa: E402
import provider_profiles  # noqa: E402
import serve             # noqa: E402
import task_store        # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402

OWNER = "alice"
BOT = "gmail-bot"
_PROFILE_ID = "gmail-bridge-test-profile"


async def _frames(agen):
    return [frame async for frame in agen]


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


def _last_assistant(user, conversation_id):
    conv = chat_service.get_conversation(user, conversation_id) or {}
    msgs = [m.get("content", "") for m in conv.get("messages", [])
            if m.get("role") == "assistant"]
    return msgs[-1] if msgs else ""


def _assert_only_gmail_submissions(store):
    for sub in store.submissions:
        assert sub.get("executor_prefix") == "gmail", sub


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


class _FakeGmailRead:
    """A stand-in for ``connectors.GmailRead`` at the owner-scoped seam.

    Returns the connector's already-redacted projection (id/thread_id +
    Subject/From/Date + snippet); ``error`` raises at the seam to model a
    missing/expired scope or a Calendar-only token.
    """

    def __init__(self, *, hits=None, messages=None, error=None):
        self._hits = list(hits or [])
        self._messages = dict(messages or {})
        self._error = error
        self.searches = []
        self.fetched = []

    def search(self, *, query=None, max_results=10):
        self.searches.append({"query": query, "max_results": max_results})
        if self._error is not None:
            raise self._error
        return list(self._hits)

    def message(self, message_id):
        self.fetched.append(str(message_id))
        if self._error is not None:
            raise self._error
        return dict(self._messages[str(message_id)])


class _FakeStore:
    """A minimal ``connectors.default_store()`` seam for the reader."""

    def __init__(self, gmail):
        self._gmail = gmail

    def gmail(self, owner, **kwargs):
        self.owner = owner
        return self._gmail


def _message(mid, *, subject, sender, date, snippet="", extra=None):
    headers = {"Subject": subject, "From": sender, "Date": date}
    if extra:
        headers.update(extra)
    return {"owner": OWNER, "id": mid, "thread_id": "t-" + mid,
            "snippet": snippet, "headers": headers}


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """One isolated environment per test: registry, data dir, task store."""
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    raw = CloudTaskStore()                       # DATA_DIR/cloud_tasks.db
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
        "name": "Gmail Bridge Test Profile",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": os.environ.get("KYREX_API_KEY") or "sk-test",
        "models": models,
    })
    return _PROFILE_ID


def _gmail_bot(tmp, *, bot_id=BOT, owner=OWNER, status="running", policy=None):
    return bots.add_bot(
        bot_id, "Gmail Reader", "openai:x", str(tmp),
        owner=owner, status=status,
        policy=serve.gmail_reader_preset_policy() if policy is None else policy,
        provider_profile_id=_ensure_profile(owner))


class _RecordingEngine:
    def __init__(self, user, conversation_id, workspace, bot_cfg=None):
        self.workspace = workspace

    def run_turn(self, text, on_token, cancel_check=None):
        on_token("bot answer")
        return "bot answer", None

    def interrupt(self):
        pass

    def close(self):
        pass


def _write_conv_for(user, *, bot_id=None, cid="cid-foreign"):
    now = chat_service._now_iso()
    conv = {
        "conversation_id": cid,
        "title": "t",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    if bot_id:
        conv["bot_id"] = bot_id
    chat_service._write(user, conv)
    return conv


# ═════════════════════════════════════════════════════════════════════════
# 1. natural-language -> ONE bounded canonical command (pure mapping)
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("find emails from Randy", "gmail: search from:Randy"),
    ("search my email for Tesla", "gmail: search Tesla"),
    ("search my gmail for invoice", "gmail: search invoice"),
    ("show the subject/from/date of this message", "gmail: search"),
    ("find emails about the Tesla recall", "gmail: search the Tesla recall"),
    ("show the date of this message", "gmail: search"),
    ("show email id 18f2ab9c3d", "gmail: message 18f2ab9c3d"),
    ("show the message id 18f2ab9c3d", "gmail: message 18f2ab9c3d"),
])
def test_natural_gmail_command_maps_read_requests(text, expected):
    assert serve.natural_gmail_command(text) == expected


@pytest.mark.parametrize("text", [
    "send an email to Randy",            # implicit send
    "reply to this message",
    "forward this email to Bob",
    "delete this email",
    "remove this message",
    "archive all emails",
    "label this message important",
    "mark this message as read",
    "unsubscribe from this mailing list",
    "find the config file",              # no mail object
    "show me the build log",             # no mail object
    "",                                  # empty
    "   ",                               # whitespace
])
def test_natural_gmail_command_rejects_actions_and_non_mail(text):
    assert serve.natural_gmail_command(text) is None


def test_natural_gmail_command_query_is_bounded():
    text = "find emails from " + ("a" * 500)
    out = serve.natural_gmail_command(text)
    assert out is not None and out.startswith("gmail: search ")
    assert len(out) <= len("gmail: search ") + serve._GMAIL_QUERY_MAX


# ═════════════════════════════════════════════════════════════════════════
# 2. the canonical submission surface accepts ONLY the two bounded forms
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("gmail: search", "gmail: search"),
    ("gmail: search from:Randy", "gmail: search from:Randy"),
    ("gmail: message 18f2ab9c3d", "gmail: message 18f2ab9c3d"),
])
def test_canonical_gmail_task_accepts_bounded_forms(text, expected):
    assert serve.canonical_gmail_task(text) == expected


@pytest.mark.parametrize("text", [
    "gmail: send hello",                    # a write, never a read
    "gmail: search a\nb",                   # newline-bearing query
    "gmail: search " + ("x" * 300),         # over the query ceiling
    "gmail: message " + ("x" * 600),        # over the id ceiling
    "gmail: message",                       # no id
    "gmail: message a b",                   # whitespace inside the id
    "gmail: delete 18f2ab9c3d",
    "gmail:",
    "search from:Randy",                    # not canonical
])
def test_canonical_gmail_task_rejects_anything_else(text):
    assert serve.canonical_gmail_task(text) is None


# ═════════════════════════════════════════════════════════════════════════
# 3. route readiness — exact mail:read grant only, never write-capable
# ═════════════════════════════════════════════════════════════════════════

def test_gmail_route_ready_predicate():
    policy = serve.gmail_reader_preset_policy()
    running = {"id": BOT, "owner": OWNER, "status": "running", "policy": policy}
    assert dev_bot.gmail_route_ready(running) is True
    assert dev_bot.gmail_route_ready(dict(running, status="stopped")) is False
    assert dev_bot.gmail_route_ready(dict(running, policy={})) is False
    assert dev_bot.gmail_route_ready(
        dict(running, policy=dict(policy, **{"fs:write": 1}))) is False
    assert dev_bot.gmail_route_ready(
        dict(running, policy={"mail:*": 0, "*": 0})) is False


def test_gmail_preset_is_exactly_mail_read():
    policy = serve.gmail_reader_preset_policy()
    assert policy == {"mail:read": 0}
    assert serve.mail_read_granted(policy) is True
    assert serve.is_gmail_reader_policy(policy) is True
    assert serve.is_writable_bot_policy(policy) is False
    # Distinct from the other presets (none widened).
    assert serve.mail_read_granted(serve.calendar_reader_preset_policy()) is False
    assert serve.is_gmail_reader_policy(
        serve.calendar_reader_preset_policy()) is False


def test_submit_gmail_task_surface_is_pinned():
    params = set(inspect.signature(dev_bot.submit_gmail_task).parameters)
    assert params == {"user", "bot", "task_text", "store", "conversation_id"}
    for forbidden in ("url", "date", "branch", "method", "body", "filter",
                      "steps", "repo_url", "executor_prefix", "policy",
                      "query", "message_id"):
        assert forbidden not in params


# ═════════════════════════════════════════════════════════════════════════
# 4. end-to-end: the reader's safe projection is relayed into Chat
# ═════════════════════════════════════════════════════════════════════════

def _run_with_worker(rig, text, *, hits=None, messages=None, error=None,
                     bot_id=BOT):
    gmail = _FakeGmailRead(hits=hits, messages=messages, error=error)
    fake_store = _FakeStore(gmail)
    worker = TaskWorker(rig["raw"], worker_id="gmail-bridge",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(connectors, "default_store", lambda: fake_store):
            recv = chat_service.create_conversation(OWNER, bot_id=bot_id)
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, recv["conversation_id"], text)))
    finally:
        worker.stop()
    return frames, gmail, fake_store, recv["conversation_id"]


def test_natural_search_routes_to_gmail_and_relays_headers(rig):
    _gmail_bot(rig["tmp"])
    frames, gmail, fake_store, cid = _run_with_worker(
        rig, "find emails from Randy",
        hits=[{"owner": OWNER, "id": "m1", "thread_id": "t1"}],
        messages={"m1": _message(
            "m1", subject="Tesla update", sender="randy@example.com",
            date="Mon, 1 Jan 2024 00:00:00 +0000", snippet="hi there",
            extra={"X-Secret": "LEAK-SHOULD-NOT-SURFACE"})})

    subs = rig["store"].submissions
    assert len(subs) == 1, subs
    sub = subs[0]
    assert sub["executor_prefix"] == "gmail"
    assert sub["session_key"] == BOT
    assert sub["bot_id"] == BOT
    assert sub["repo_url"] is None
    assert sub["task_text"] == "gmail: search from:Randy"
    _assert_only_gmail_submissions(rig["store"])

    assert gmail.searches and gmail.searches[0]["query"] == "from:Randy"
    assert gmail.fetched == ["m1"]
    assert fake_store.owner == OWNER                # owner-scoped store

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    content = terminal["content"] or ""
    assert "Tesla update" in content
    assert "randy@example.com" in content
    assert "Mon, 1 Jan 2024" in content
    assert "LEAK-SHOULD-NOT-SURFACE" not in content   # header whitelist
    assert "Tesla update" in _last_assistant(OWNER, cid)


def test_message_id_routes_to_single_message_headers(rig):
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(
        rig, "show the subject and from of message id 18f2ab9c3d",
        messages={"18f2ab9c3d": _message(
            "18f2ab9c3d", subject="Quarterly report",
            sender="cfo@example.com", date="Tue, 2 Jan 2024 00:00:00 +0000")})

    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["executor_prefix"] == "gmail"
    assert subs[0]["task_text"] == "gmail: message 18f2ab9c3d"
    _assert_only_gmail_submissions(rig["store"])
    assert gmail.searches == []                     # a message read, no search
    assert gmail.fetched == ["18f2ab9c3d"]

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    content = terminal["content"] or ""
    assert "Subject: Quarterly report" in content
    assert "From: cfo@example.com" in content
    assert "Date: Tue, 2 Jan 2024" in content


def test_header_only_request_reads_most_recent_mail(rig):
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(
        rig, "show the subject/from/date of this message",
        hits=[{"owner": OWNER, "id": "m9", "thread_id": "t9"}],
        messages={"m9": _message(
            "m9", subject="Latest note", sender="a@example.com",
            date="Wed, 3 Jan 2024 00:00:00 +0000")})

    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["task_text"] == "gmail: search"
    assert gmail.searches and gmail.searches[0]["query"] is None
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    assert "Latest note" in (terminal["content"] or "")


def test_missing_gmail_scope_fails_closed_in_chat(rig):
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(
        rig, "search my email for Tesla",
        error=connectors.ConnectorUnavailable(
            "gmail read authorization is missing"))

    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["task_text"] == "gmail: search Tesla"
    _assert_only_gmail_submissions(rig["store"])
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    assert "Gmail read unavailable" in (terminal["content"] or "")
    # The read never returned any message content.
    assert gmail.fetched == []


# ═════════════════════════════════════════════════════════════════════════
# 5. no fallthrough: a mail ACTION / non-mail request stays on the engine
#    path and NEVER submits a task
# ═════════════════════════════════════════════════════════════════════════

def test_actions_and_non_mail_never_route(rig, monkeypatch):
    _gmail_bot(rig["tmp"])
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    for text in (
        "send an email to Randy",
        "delete my inbox messages",
        "archive all emails",
        "label this message important",
        "find the config file",
        "show me the build log",
    ):
        recv = chat_service.create_conversation(OWNER, bot_id=BOT)
        frames = asyncio.run(_frames(chat_service.stream_chat(
            OWNER, recv["conversation_id"], text)))
        terminal = _terminal(frames)
        assert terminal is not None and terminal["status"] == "complete", (
            text, frames)
    assert rig["store"].submissions == []


def test_missing_grant_does_not_route_to_gmail(rig, monkeypatch):
    _gmail_bot(rig["tmp"], policy={})
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], "find emails from Randy")))
    assert rig["store"].submissions == []
    assert _terminal(frames)["status"] == "complete"
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_gmail_task(OWNER, bots.get_bot(BOT),
                                  "gmail: search from:Randy",
                                  store=rig["store"])
    assert rig["store"].submissions == []


def test_wildcard_grant_never_counts(rig):
    _gmail_bot(rig["tmp"], policy={"mail:*": 0, "*": 0})
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_gmail_task(OWNER, bots.get_bot(BOT),
                                  "gmail: search from:Randy",
                                  store=rig["store"])
    assert rig["store"].submissions == []


def test_noncanonical_direct_submission_fails_closed(rig):
    _gmail_bot(rig["tmp"])                          # exact grant
    for bad in (
        "gmail: send hello",
        "gmail: delete 18f2ab9c3d",
        "search from:Randy",
        "find emails from Randy",                   # natural text, not canonical
        "gmail: search a\nb",
    ):
        with pytest.raises(dev_bot.DevBotError):
            dev_bot.submit_gmail_task(OWNER, bots.get_bot(BOT), bad,
                                      store=rig["store"])
    assert rig["store"].submissions == []


# ═════════════════════════════════════════════════════════════════════════
# 6. owner / lifecycle isolation
# ═════════════════════════════════════════════════════════════════════════

def test_foreign_owner_fails_closed(rig):
    _gmail_bot(rig["tmp"])                          # owned by alice
    _write_conv_for("bob", bot_id=BOT)
    with pytest.raises(chat_service.ChatUnavailable):
        asyncio.run(_frames(chat_service.stream_chat(
            "bob", "cid-foreign", "find emails from Randy")))
    assert rig["store"].submissions == []


def test_foreign_direct_submission_fails_closed(rig):
    _gmail_bot(rig["tmp"])                          # owned by alice
    with pytest.raises(dev_bot.DevBotError, match="another owner"):
        dev_bot.submit_gmail_task("bob", bots.get_bot(BOT),
                                  "gmail: search from:Randy",
                                  store=rig["store"])
    assert rig["store"].submissions == []


def test_stopped_bot_fails_closed(rig):
    _gmail_bot(rig["tmp"], status="stopped")
    _write_conv_for(OWNER, bot_id=BOT)
    with pytest.raises(chat_service.ChatUnavailable):
        asyncio.run(_frames(chat_service.stream_chat(
            OWNER, "cid-foreign", "find emails from Randy")))
    assert rig["store"].submissions == []


def test_write_capable_bot_never_routes_to_gmail(rig):
    # A write-capable Bot (fs:write) routes to the repo path, never the reader,
    # even if it also held a mail grant.
    bot = _gmail_bot(rig["tmp"], policy={"mail:read": 0, "fs:write": 1})
    assert dev_bot.gmail_route_ready(bot) is False
    assert serve.mail_read_granted(bot["policy"]) is True  # grant is present...
    with pytest.raises(dev_bot.DevBotError, match="write-capable"):
        dev_bot.submit_gmail_task(OWNER, bot, "gmail: search from:Randy",
                                  store=rig["store"])
    assert rig["store"].submissions == []
