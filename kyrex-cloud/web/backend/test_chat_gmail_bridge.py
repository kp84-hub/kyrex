"""Chat -> Gmail Reader bridge tests.

Proves the selected-Bot route added to chat_service for the bounded, READ-ONLY
Gmail mail surface:

    any running Bot the owner owns, when the OWNER's Google connection carries
      the ``gmail.readonly`` scope (an owner-scoped CONNECTED TOOL shared
      across every Bot -- NOT gated on the Bot's policy, write-capability, or
      role/persona)
      + natural-language mail-shaped text ("find emails from Randy",
        "search my email for Tesla", "show the subject/from/date of this
        message")
      -> ONE of the two canonical commands, mapped DETERMINISTICALLY
         (``gmail: search [<query>]`` / ``gmail: message <id>``)
      -> the durable gmail task
         (dev_bot.submit_gmail_task -> CloudTaskStore -> TaskWorker ->
          serve.run_task(executor_prefix="gmail"))
      -> the in-process reader against the OWNER-SCOPED encrypted connector
         store (``connectors.default_store().gmail(owner)``) which is
         authoritative and re-checks the granted ``gmail.readonly`` scope
      -> the safe Subject/From/Date projection (or the explicit fail-closed
         error) relayed into Kyrex Chat.

Asserted negatively: the gmail path NEVER submits a writable repo/browser/
calendar task; a mail ACTION (send/delete/archive/label), a request with no
mail object, a foreign owner, a stopped Bot, an owner with NO connected Gmail
scope, and an unbounded/malformed request all fail closed (no task at all, and
the ordinary engine path is used where a route is simply absent). No token, raw
body, or non-whitelisted header ever reaches the relayed text.

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

    def __init__(self, *, hits=None, messages=None, reads=None, error=None,
                 next_page_token=""):
        self._hits = list(hits or [])
        self._messages = dict(messages or {})
        self._reads = dict(reads or {})
        self._error = error
        self._next_page_token = str(next_page_token or "")
        self.searches = []
        self.fetched = []
        self.reads = []

    def search(self, *, query=None, max_results=10, page_token=None):
        self.searches.append({"query": query, "max_results": max_results,
                              "page_token": page_token})
        if self._error is not None:
            raise self._error
        # Model the connector's bound: only the requested page size is returned.
        limit = max(1, min(int(max_results or 10), 50))
        return {"owner": OWNER, "messages": list(self._hits)[:limit],
                "next_page_token": self._next_page_token}

    def message(self, message_id):
        self.fetched.append(str(message_id))
        if self._error is not None:
            raise self._error
        return dict(self._messages[str(message_id)])

    def read_message(self, message_id):
        self.reads.append(str(message_id))
        if self._error is not None:
            raise self._error
        return dict(self._reads[str(message_id)])


class _FakeStore:
    """A minimal ``connectors.default_store()`` seam for the reader.

    ``available`` models the OWNER's Gmail readonly grant: the route gate for
    the shared, owner-scoped Gmail tool (``gmail_read_available``), while
    ``gmail(owner)`` is the authoritative reader. The two are deliberately
    separate so a test can show "connected" routing and a fail-closed read.
    """

    def __init__(self, gmail, *, available=True):
        self._gmail = gmail
        self._available = bool(available)

    def gmail(self, owner, **kwargs):
        self.owner = owner
        return self._gmail

    def gmail_read_available(self, owner, **kwargs):
        self.owner = owner
        return self._available


def _message(mid, *, subject, sender, date, snippet="", extra=None):
    headers = {"Subject": subject, "From": sender, "Date": date}
    if extra:
        headers.update(extra)
    return {"owner": OWNER, "id": mid, "thread_id": "t-" + mid,
            "snippet": snippet, "headers": headers}


def _read_message(mid, *, subject, sender, date, body, snippet="",
                  truncated=False, extra=None):
    """A full-message projection: headers + a bounded, readable body."""
    out = _message(mid, subject=subject, sender=sender, date=date,
                   snippet=snippet, extra=extra)
    out.update({"body": body, "body_type": "text", "truncated": truncated})
    return out


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
# 3. route readiness — an OWNER-scoped connected tool, independent of Bot role
# ═════════════════════════════════════════════════════════════════════════

def test_gmail_route_ready_is_owner_scoped_not_role_gated(monkeypatch):
    # Gmail read is an OWNER-scoped CONNECTED TOOL shared across every Bot the
    # owner owns: readiness depends on the OWNER's Google grant, NOT the Bot's
    # policy, write-capability, or role/persona.
    running = {"id": BOT, "owner": OWNER, "status": "running", "policy": {}}
    connected = _FakeStore(_FakeGmailRead(), available=True)
    monkeypatch.setattr(connectors, "default_store", lambda: connected)

    assert dev_bot.gmail_route_ready(running) is True
    # Every user-facing role shares the one connected tool.
    for policy in (serve.developer_preset_policy(),
                   serve.calendar_preset_policy(),
                   serve.browser_preset_policy(),
                   serve.coordinator_preset_policy()):
        assert dev_bot.gmail_route_ready(dict(running, policy=policy)) is True
    # A write-capable Bot (Developer) is no longer excluded.
    assert dev_bot.gmail_route_ready(
        dict(running, policy=serve.developer_preset_policy())) is True

    # Lifecycle and ownership still gate.
    assert dev_bot.gmail_route_ready(dict(running, status="stopped")) is False
    assert dev_bot.gmail_route_ready(dict(running, owner="")) is False

    # OAuth absent -> fail closed for EVERY role (no route).
    disconnected = _FakeStore(_FakeGmailRead(), available=False)
    monkeypatch.setattr(connectors, "default_store", lambda: disconnected)
    assert dev_bot.gmail_route_ready(running) is False
    for policy in (serve.developer_preset_policy(),
                   serve.calendar_preset_policy(),
                   serve.browser_preset_policy(),
                   serve.coordinator_preset_policy()):
        assert dev_bot.gmail_route_ready(dict(running, policy=policy)) is False

    # Any fault in the connector seam is "not ready" (fail closed).
    def _boom():
        raise RuntimeError("connector down")

    monkeypatch.setattr(connectors, "default_store", _boom)
    assert dev_bot.gmail_route_ready(running) is False


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

def _run_with_worker(rig, text, *, hits=None, messages=None, reads=None,
                     error=None, bot_id=BOT, next_page_token="",
                     conversation_id=None):
    gmail = _FakeGmailRead(hits=hits, messages=messages, reads=reads,
                           error=error, next_page_token=next_page_token)
    fake_store = _FakeStore(gmail)
    worker = TaskWorker(rig["raw"], worker_id="gmail-bridge",
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()
    try:
        with patch.object(connectors, "default_store", lambda: fake_store):
            if conversation_id:
                cid = conversation_id
            else:
                cid = chat_service.create_conversation(
                    OWNER, bot_id=bot_id)["conversation_id"]
            frames = asyncio.run(_frames(chat_service.stream_chat(
                OWNER, cid, text)))
    finally:
        worker.stop()
    return frames, gmail, fake_store, cid


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
    assert gmail.searches[0]["max_results"] == 5      # bounded page size
    assert gmail.searches[0]["page_token"] is None    # first page
    assert gmail.fetched == ["m1"]
    assert fake_store.owner == OWNER                # owner-scoped store

    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    content = terminal["content"] or ""
    # A natural, compact response -- not the raw connector projection.
    assert "I found 1 recent email from Randy:" in content, content
    assert "Tesla update" in content
    assert "Mon, 1 Jan 2024" in content
    assert "randy@example.com" not in content          # sender heads the reply
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


def test_no_owner_gmail_scope_does_not_route(rig, monkeypatch):
    # Gmail read is OWNER-scoped: with NO connected Gmail grant the natural
    # request stays on the ordinary engine path and submits NO task. The Bot's
    # (empty) policy is irrelevant -- the OWNER's connection is the gate.
    _gmail_bot(rig["tmp"], policy={})
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _FakeStore(_FakeGmailRead(), available=False))
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], "find emails from Randy")))
    assert rig["store"].submissions == []
    assert _terminal(frames)["status"] == "complete"


def test_policy_less_bot_can_share_gmail_when_owner_connected(rig):
    # A policy-less Bot shares the owner's connected Gmail read: NO Bot policy
    # grant is required (the connector, not the policy, is authoritative).
    _gmail_bot(rig["tmp"], policy={})
    dev_bot.submit_gmail_task(OWNER, bots.get_bot(BOT),
                              "gmail: search from:Randy", store=rig["store"])
    assert [s["executor_prefix"] for s in rig["store"].submissions] == ["gmail"]


def test_wildcard_policy_does_not_gate_gmail_sharing(rig):
    # A wildcard/non-grant policy is irrelevant to the owner-scoped Gmail tool:
    # the canonical read still submits (the connector re-checks the scope).
    _gmail_bot(rig["tmp"], policy={"mail:*": 0, "*": 0})
    dev_bot.submit_gmail_task(OWNER, bots.get_bot(BOT),
                              "gmail: search from:Randy", store=rig["store"])
    assert [s["executor_prefix"] for s in rig["store"].submissions] == ["gmail"]


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


def test_write_capable_bot_can_share_gmail_read(rig, monkeypatch):
    # A write-capable Developer Bot is no longer excluded from Gmail: the read
    # is an owner-scoped connected tool shared across every Bot the owner owns.
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _FakeStore(_FakeGmailRead(), available=True))
    bot = _gmail_bot(rig["tmp"], policy=serve.developer_preset_policy())
    assert dev_bot.gmail_route_ready(bot) is True
    dev_bot.submit_gmail_task(OWNER, bot, "gmail: search from:Randy",
                              store=rig["store"])
    assert [s["executor_prefix"] for s in rig["store"].submissions] == ["gmail"]


# ═════════════════════════════════════════════════════════════════════════
# 7. explicit canonical commands pass through; invalid ones fail closed
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text", [
    "gmail: search",
    "gmail: search from:See.Randy@principal.com",
    "gmail: search from:Randy has:attachment",
    "gmail: message 18f2ab9c3d",
    "gmail: more TOK123 from:Randy",
    "gmail: more TOK123",
])
def test_explicit_canonical_command_is_a_fixed_point(text):
    # An EXPLICIT canonical command is already the bounded form: it is passed
    # through UNCHANGED (never re-derived or re-normalised).
    assert serve.canonical_gmail_task(text) == text
    assert serve.natural_gmail_command(text) == text


def test_canonical_passthrough_preserves_the_from_operator():
    # The regression: "gmail: search from:See.Randy@principal.com" must NOT be
    # mangled into a bare-term search ("gmail: search See.Randy@principal.com").
    text = "gmail: search from:See.Randy@principal.com"
    assert serve.natural_gmail_command(text) == text
    assert "from:See.Randy@principal.com" in serve.natural_gmail_command(text)


@pytest.mark.parametrize("text,expected", [
    ("show 5 more", True),
    ("show me 5 more", True),
    ("show more", True),
    ("show 5 more emails", True),
    ("5 more", True),
    ("next 5 more", True),
    ("more", False),                 # a bare "more" is never hijacked
    ("send more emails", False),     # a mail write is never a continuation
    ("find emails from Randy", False),
    ("gmail: search from:Randy", False),
    ("", False),
])
def test_natural_gmail_more_detection(text, expected):
    assert serve.natural_gmail_more(text) is expected


@pytest.mark.parametrize("text", [
    "gmail: more",                    # no token
    "gmail: more   ",                 # only a token-less remainder
    "gmail: more " + ("x" * 5000),    # over the token ceiling
    "gmail: search a\nb",             # newline-bearing query
    "gmail: send hello",              # a write, never a read
])
def test_canonical_gmail_task_rejects_malformed_more(text):
    assert serve.canonical_gmail_task(text) is None


def test_explicit_canonical_command_routes_unchanged(rig):
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(
        rig, "gmail: search from:See.Randy@principal.com",
        hits=[{"owner": OWNER, "id": "m1", "thread_id": "t1"}],
        messages={"m1": _message(
            "m1", subject="Principal notice", sender="See.Randy@principal.com",
            date="Thu, 4 Jan 2024 00:00:00 +0000")})

    subs = rig["store"].submissions
    assert len(subs) == 1, subs
    # Passed through byte-for-byte -- the from: operator is intact.
    assert subs[0]["task_text"] == "gmail: search from:See.Randy@principal.com"
    assert gmail.searches[0]["query"] == "from:See.Randy@principal.com"
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    assert "I found 1 recent email from See.Randy@principal.com" in (
        terminal["content"] or "")


def test_invalid_gmail_namespace_fails_closed(rig, monkeypatch):
    # A message in the reserved ``gmail:`` namespace that is NOT a bounded
    # canonical read fails closed with usage -- NEVER the LLM/repo path, and
    # NO task is created.
    _gmail_bot(rig["tmp"])
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _FakeStore(_FakeGmailRead(), available=True))
    for text in ("gmail: send hello", "gmail: delete 18f2ab9c3d",
                 "gmail: more", "gmail: nonsense"):
        recv = chat_service.create_conversation(OWNER, bot_id=BOT)
        frames = asyncio.run(_frames(chat_service.stream_chat(
            OWNER, recv["conversation_id"], text)))
        terminal = _terminal(frames)
        assert terminal is not None and terminal["status"] == "complete", (
            text, frames)
        assert "Unsupported Gmail command" in (terminal["content"] or ""), text
    assert rig["store"].submissions == []


def test_unconnected_owner_gmail_namespace_fails_closed(rig, monkeypatch):
    # The reserved namespace still fails closed when the OWNER's connection
    # lacks the gmail.readonly scope: an explicit canonical command is answered
    # with usage and NO task (never the LLM, never a raw forward).
    _gmail_bot(rig["tmp"])
    monkeypatch.setattr(chat_service, "_get_engine_session", _RecordingEngine)
    monkeypatch.setattr(connectors, "default_store",
                        lambda: _FakeStore(_FakeGmailRead(), available=False))
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    frames = asyncio.run(_frames(chat_service.stream_chat(
        OWNER, recv["conversation_id"], "gmail: search from:Randy")))
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    assert "Unsupported Gmail command" in (terminal["content"] or "")
    assert rig["store"].submissions == []


# ═════════════════════════════════════════════════════════════════════════
# 8. bounded pagination: "show 5 more" continues via nextPageToken
# ═════════════════════════════════════════════════════════════════════════

def _page_messages(prefix, n):
    return {f"{prefix}{i}": _message(
        f"{prefix}{i}", subject=f"{prefix} subject {i}",
        sender="randy@example.com",
        date=f"Mon, {i} Jan 2024 00:00:00 +0000") for i in range(1, n + 1)}


def test_show_five_more_continues_the_same_search(rig):
    _gmail_bot(rig["tmp"])
    # Turn 1: a fresh search returns 5 hits + a continuation token.
    hits1 = [{"owner": OWNER, "id": f"p{i}", "thread_id": f"t{i}"}
             for i in range(1, 6)]
    frames1, gmail1, _, cid = _run_with_worker(
        rig, "find emails from Randy", hits=hits1,
        messages=_page_messages("p", 5), next_page_token="NEXT-TOKEN")
    t1 = _terminal(frames1)
    assert t1 is not None and t1["status"] == "complete", frames1
    assert "I found 5 recent emails from Randy:" in (t1["content"] or "")
    assert 'Reply "show 5 more" for the next 5.' in (t1["content"] or "")
    assert gmail1.searches[0]["page_token"] is None
    # The continuation is stored on the conversation (query + nextPageToken).
    conv = chat_service.get_conversation(OWNER, cid)
    assert conv.get("gmail_page") == {"query": "from:Randy",
                                      "next_page_token": "NEXT-TOKEN"}

    # Turn 2: "show 5 more" resolves to the bounded next page of the SAME query.
    hits2 = [{"owner": OWNER, "id": f"q{i}", "thread_id": f"u{i}"}
             for i in range(1, 3)]
    frames2, gmail2, _, _ = _run_with_worker(
        rig, "show 5 more", hits=hits2, messages=_page_messages("q", 2),
        conversation_id=cid)
    t2 = _terminal(frames2)
    assert t2 is not None and t2["status"] == "complete", frames2
    assert "I found 2 recent emails from Randy:" in (t2["content"] or "")
    assert gmail2.searches[0]["query"] == "from:Randy"
    assert gmail2.searches[0]["page_token"] == "NEXT-TOKEN"
    assert gmail2.searches[0]["max_results"] == 5
    # The continuation is submitted as the bounded canonical form.
    assert rig["store"].submissions[-1]["task_text"] == (
        "gmail: more NEXT-TOKEN from:Randy")
    # The exhausted page clears the continuation.
    conv = chat_service.get_conversation(OWNER, cid)
    assert "gmail_page" not in conv


def test_show_more_without_a_continuation_fails_closed(rig):
    _gmail_bot(rig["tmp"])
    fake_store = _FakeStore(_FakeGmailRead(), available=True)
    with patch.object(connectors, "default_store", lambda: fake_store):
        recv = chat_service.create_conversation(OWNER, bot_id=BOT)
        frames = asyncio.run(_frames(chat_service.stream_chat(
            OWNER, recv["conversation_id"], "show 5 more")))
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    assert "no Gmail search to continue" in (terminal["content"] or "")
    assert rig["store"].submissions == []


# ═════════════════════════════════════════════════════════════════════════
# 9. read-only regression: metadata headers only, never a body; bounded
# ═════════════════════════════════════════════════════════════════════════

def test_search_is_bounded_and_fetches_headers_only(rig):
    _gmail_bot(rig["tmp"])
    hits = [{"owner": OWNER, "id": f"m{i}", "thread_id": f"t{i}"}
            for i in range(1, 7)]
    frames, gmail, _, _ = _run_with_worker(
        rig, "search my email for Tesla", hits=hits,
        messages={f"m{i}": _message(
            f"m{i}", subject=f"Tesla {i}", sender="r@example.com",
            date=f"Tue, {i} Jan 2024 00:00:00 +0000",
            snippet="body text that must never surface") for i in range(1, 7)})
    # The search asks for exactly the bounded page size (5), not the whole set.
    assert gmail.searches[0]["max_results"] == 5
    # Each hit is enriched with the metadata projection only.
    assert gmail.fetched == ["m1", "m2", "m3", "m4", "m5"]
    terminal = _terminal(frames)
    content = terminal["content"] or ""
    assert 'I found 5 recent emails matching "Tesla":' in content, content
    assert "body text that must never surface" not in content   # no body
    assert "Preview" not in content                             # no snippet
    assert "Tesla 1" in content and "Tesla 5" in content
    assert "Tesla 6" not in content                             # page bound


def test_gmail_message_read_is_unchanged(rig):
    # The single-message read still renders the detailed Subject/From/Date
    # projection -- the compact search rendering is search-only.
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(
        rig, "show the subject and from of message id 18f2ab9c3d",
        messages={"18f2ab9c3d": _message(
            "18f2ab9c3d", subject="Quarterly report",
            sender="cfo@example.com", date="Tue, 2 Jan 2024 00:00:00 +0000")})
    assert gmail.searches == []
    assert gmail.fetched == ["18f2ab9c3d"]
    content = _terminal(frames)["content"] or ""
    assert "Subject: Quarterly report" in content
    assert "From: cfo@example.com" in content
    assert "Date: Tue, 2 Jan 2024" in content


# ═════════════════════════════════════════════════════════════════════════
# 10. bounded full-message reading: resolve ONE message, then read its body
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("read the latest email from Randy", "gmail: latest from:Randy"),
    ("read the latest email", "gmail: latest"),
    ("read my email", "gmail: latest"),
    ("read the email from Randy", "gmail: read from:Randy"),
    ("open the email from Randy", "gmail: read from:Randy"),
    ("view the message about Tesla", "gmail: read Tesla"),
    ("read email id 18f2ab9c3d", "gmail: read id 18f2ab9c3d"),
])
def test_natural_gmail_command_maps_body_reads(text, expected):
    assert serve.natural_gmail_command(text) == expected


@pytest.mark.parametrize("text", [
    "read number 2",            # a numbered selection -> resolved by the caller
    "open #3",
    "read 4",
])
def test_natural_gmail_command_leaves_selection_to_caller(text):
    # A numbered selection is never a search/read command; the caller resolves
    # it against the conversation's stored hits.
    assert serve.natural_gmail_command(text) is None


@pytest.mark.parametrize("text", [
    "read the subject of the email from Randy",  # header-only -> search path
    "show the subject of this message",          # header-only -> search path
])
def test_natural_gmail_headers_stay_on_search_path(text):
    out = serve.natural_gmail_command(text)
    assert out == "gmail: search" or out.startswith("gmail: search "), out


def test_natural_gmail_command_rejects_a_mail_action():
    assert serve.natural_gmail_command("send the email to Randy") is None


@pytest.mark.parametrize("text,expected", [
    ("read number 2", 2),
    ("open #2", 2),
    ("read 2", 2),
    ("show email 3", 3),
    ("please read number 12", 12),
    ("read the latest email from Randy", None),
    ("read the email from Randy", None),
    ("show 5 more", None),
    ("read number 0", None),
    ("", None),
])
def test_natural_gmail_select(text, expected):
    assert serve.natural_gmail_select(text) == expected


@pytest.mark.parametrize("text", [
    "gmail: read from:Randy",
    "gmail: read id 18f2ab9c3d",
    "gmail: latest",
    "gmail: latest from:Randy",
])
def test_canonical_gmail_task_accepts_read_forms(text):
    assert serve.canonical_gmail_task(text) == text


@pytest.mark.parametrize("text", [
    "gmail: read",                        # no payload
    "gmail: read id a b",                 # whitespace in the id
    "gmail: latest " + ("x" * 300),       # over the query ceiling
    "gmail: read a\nb",                   # newline-bearing query
])
def test_canonical_gmail_task_rejects_malformed_reads(text):
    assert serve.canonical_gmail_task(text) is None


def test_read_the_latest_email_resolves_and_reads_one_body(rig):
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(
        rig, "read the latest email from Randy",
        hits=[{"owner": OWNER, "id": "m1", "thread_id": "t1"}],
        reads={"m1": _read_message(
            "m1", subject="Tesla update", sender="randy@example.com",
            date="Mon, 1 Jan 2024 00:00:00 +0000",
            body="The full readable body of the latest message.")})

    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["executor_prefix"] == "gmail"
    assert subs[0]["task_text"] == "gmail: latest from:Randy"
    _assert_only_gmail_submissions(rig["store"])
    # Resolved through the SAME bounded search, then ONE body read.
    assert gmail.searches[0]["query"] == "from:Randy"
    assert gmail.searches[0]["max_results"] == 5
    assert gmail.reads == ["m1"]
    terminal = _terminal(frames)
    assert terminal is not None and terminal["status"] == "complete", frames
    content = terminal["content"] or ""
    assert "Subject: Tesla update" in content
    assert "From: randy@example.com" in content
    assert "The full readable body of the latest message." in content


def test_read_number_2_selects_the_stored_hit(rig):
    _gmail_bot(rig["tmp"])
    cid = chat_service.create_conversation(OWNER, bot_id=BOT)[
        "conversation_id"]
    # Turn 1: a search populates the conversation's ordered hit ids.
    frames1, gmail1, _, cid = _run_with_worker(
        rig, "find emails from Randy",
        hits=[{"owner": OWNER, "id": f"m{i}", "thread_id": f"t{i}"}
              for i in range(1, 4)],
        messages={f"m{i}": _message(
            f"m{i}", subject=f"Randy {i}", sender="randy@example.com",
            date=f"Mon, {i} Jan 2024 00:00:00 +0000") for i in range(1, 4)},
        conversation_id=cid)
    assert _terminal(frames1)["status"] == "complete"
    conv = chat_service.get_conversation(OWNER, cid)
    assert conv.get("gmail_results") == ["m1", "m2", "m3"]

    # Turn 2: "read number 2" resolves to hit #2's id, then reads its body.
    frames2, gmail2, _, _ = _run_with_worker(
        rig, "read number 2",
        reads={"m2": _read_message(
            "m2", subject="Randy 2", sender="randy@example.com",
            date="Tue, 2 Jan 2024 00:00:00 +0000", body="Second message body.")},
        conversation_id=cid)
    assert rig["store"].submissions[-1]["task_text"] == "gmail: read id m2"
    assert gmail2.reads == ["m2"]
    assert gmail2.searches == []                     # a direct read, no search
    content = _terminal(frames2)["content"] or ""
    assert "Second message body." in content


def test_read_multi_match_fails_closed_with_candidates(rig):
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(
        rig, "read the email from Randy",
        hits=[{"owner": OWNER, "id": "m1", "thread_id": "t1"},
              {"owner": OWNER, "id": "m2", "thread_id": "t2"}],
        messages={"m1": _message(
            "m1", subject="Randy one", sender="randy@example.com",
            date="Mon, 1 Jan 2024 00:00:00 +0000"),
            "m2": _message(
            "m2", subject="Randy two", sender="randy@example.com",
            date="Tue, 2 Jan 2024 00:00:00 +0000")})
    # An AMBIGUOUS read never guesses: NO body is read, the numbered
    # candidates are returned so the user can pick.
    assert rig["store"].submissions[0]["task_text"] == "gmail: read from:Randy"
    assert gmail.reads == []
    content = _terminal(frames)["content"] or ""
    assert "Which one?" in content and "read number N" in content
    assert "Randy one" in content and "Randy two" in content


def test_read_selection_out_of_range_fails_closed(rig):
    _gmail_bot(rig["tmp"])
    cid = chat_service.create_conversation(OWNER, bot_id=BOT)[
        "conversation_id"]
    _run_with_worker(
        rig, "find emails from Randy",
        hits=[{"owner": OWNER, "id": "m1", "thread_id": "t1"}],
        messages={"m1": _message(
            "m1", subject="Randy 1", sender="randy@example.com",
            date="Mon, 1 Jan 2024 00:00:00 +0000")},
        conversation_id=cid)
    before = len(rig["store"].submissions)
    frames, gmail, _, _ = _run_with_worker(
        rig, "read number 9", conversation_id=cid)
    # Out of range -> a friendly message and NO task, NO read.
    assert len(rig["store"].submissions) == before
    assert gmail.reads == []
    content = _terminal(frames)["content"] or ""
    assert "numbered result" in content


def test_read_number_with_no_search_fails_closed(rig):
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(rig, "read number 2")
    assert rig["store"].submissions == []
    assert gmail.reads == []
    assert "numbered result" in (_terminal(frames)["content"] or "")


def test_gmail_reader_has_no_mutation_surface():
    # The full-message read adds a BODY read only -- there is still no send,
    # delete, archive, or label path anywhere on the connector.
    assert hasattr(connectors.GmailRead, "read_message")
    for method in ("send", "delete", "archive", "label", "modify", "trash",
                   "mark"):
        assert not hasattr(connectors.GmailRead, method), method


# ═════════════════════════════════════════════════════════════════════════
# 11. query-AWARE focus: a numbered selection reads the RELEVANT section
#     around the ORIGINAL topic, not the whole newsletter
# ═════════════════════════════════════════════════════════════════════════

_BULLETIN = (
    "BULLDOG BULLETIN - Volume 12, Issue 4\n"
    "\n"
    "A note from the principal: thank you for a wonderful start to the "
    "school year. Please review the lunch menu and the spirit-wear store, "
    "and remember to check the carpool schedule before the weather turns.\n"
    "\n"
    "The 4th grade field trip to the science museum is scheduled for "
    "October 17, 2025. Buses leave at 8:30 am and return by 2:30 pm. "
    "Permission slips are due by October 10. Parents are welcome to "
    "chaperone; please sign up in the front office if you can help.\n"
    "\n"
    "Save the date for the fall festival on the second Saturday of "
    "November. Volunteers are always appreciated, and the book fair runs "
    "all week in the media center.\n"
    "\n"
    "The lunch menu rotates every two weeks. Spirit wear and yearbooks can "
    "be ordered online through the school store at any time, and the winter "
    "concert rehearsals begin after the break.\n"
    "\n"
    "You received this email because you are subscribed to the Bulldog "
    "Bulletin. Unsubscribe | Manage preferences | View in browser\n"
    "\n"
    "Bull Dog Academy, 123 Main St, Raleigh, NC 27601\n"
    "(c) 2025 Bulldog Academy. All rights reserved. Follow us on social "
    "media.\n"
)


def _newsletter_read(mid="m1", *, subject="Bulldog Bulletin",
                     sender="news@bulldogacademy.org",
                     date="Mon, 6 Oct 2025 00:00:00 +0000", body=None):
    return _read_message(
        mid, subject=subject, sender=sender, date=date,
        body=_BULLETIN if body is None else body)


@pytest.mark.parametrize("query,expected", [
    ('from:"Wake Christian" "4th grade field trip Oct"',
     ["4th", "grade", "field", "trip", "Oct"]),
    ("4th grade field trip Oct", ["4th", "grade", "field", "trip", "Oct"]),
    ("from:Randy", []),                       # sender-only -> no topical terms
    ('from:"Wake Christian"', []),            # sender-only -> no topical terms
    ("", []),
])
def test_gmail_topical_terms_drop_operators_and_sender(query, expected):
    # A sender operator (and its value) is NEVER a topical anchor term.
    assert serve._gmail_topical_terms(query) == expected


def test_gmail_focus_anchor_is_topic_only():
    anchor = serve._gmail_focus_anchor(
        'from:"Wake Christian" "4th grade field trip Oct"')
    assert anchor == "4th grade field trip Oct"
    assert "Wake" not in anchor and "Christian" not in anchor
    assert "from:" not in anchor


@pytest.mark.parametrize("text", [
    'gmail: read id m1 focus "4th grade field trip"',
    'gmail: read the trip focus "field trip Oct"',
])
def test_canonical_gmail_task_accepts_focus_forms(text):
    assert serve.canonical_gmail_task(text) == text


@pytest.mark.parametrize("text", [
    'gmail: read id m1 focus 4th grade',        # unquoted anchor
    'gmail: read id m1 focus ""',               # empty anchor
    'gmail: read id m1 focus "' + ("x" * 300) + '"',   # over the ceiling
])
def test_canonical_gmail_task_rejects_malformed_focus(text):
    assert serve.canonical_gmail_task(text) is None


def test_topic_only_search_then_numbered_read_returns_focused_section(rig):
    # Turn 1: a topic-only SEARCH lists the numbered candidates.
    _gmail_bot(rig["tmp"])
    cid = chat_service.create_conversation(OWNER, bot_id=BOT)[
        "conversation_id"]
    hits = [{"owner": OWNER, "id": f"m{i}", "thread_id": f"t{i}"}
            for i in range(1, 4)]
    frames1, gmail1, _, cid = _run_with_worker(
        rig, "search my email for the 4th grade field trip in Oct",
        hits=hits,
        messages={f"m{i}": _message(
            f"m{i}", subject=f"Bulldog Bulletin {i}",
            sender="news@bulldogacademy.org",
            date=f"Mon, {i} Oct 2025 00:00:00 +0000")
            for i in range(1, 4)},
        conversation_id=cid)
    assert _terminal(frames1)["status"] == "complete"
    assert rig["store"].submissions[-1]["task_text"] == (
        "gmail: search the 4th grade field trip in Oct")
    conv = chat_service.get_conversation(OWNER, cid)
    assert conv.get("gmail_results") == ["m1", "m2", "m3"]
    # The topical anchor is persisted for a later numbered selection.
    assert conv.get("gmail_focus") == "4th grade field trip Oct"

    # Turn 2: "read number 1" carries the ORIGINAL topic as a focus anchor and
    # the OTHER stored hits as bounded enrichment siblings (never a search).
    frames2, gmail2, _, _ = _run_with_worker(
        rig, "read number 1",
        reads={"m1": _newsletter_read(), "m2": _newsletter_read("m2"),
               "m3": _newsletter_read("m3")}, conversation_id=cid)
    sub = rig["store"].submissions[-1]
    assert sub["executor_prefix"] == "gmail", sub
    assert sub["task_text"] == (
        'gmail: read id m1 focus "4th grade field trip Oct" siblings "m2,m3"')
    assert gmail2.reads == ["m1", "m2", "m3"]          # selected + 2 siblings
    assert gmail2.searches == []                       # a direct read, no search

    content = _terminal(frames2)["content"] or ""
    # The relevant section is returned...
    assert "Most relevant section" in content
    assert "4th grade field trip" in content, content
    assert "October 17, 2025" in content
    # ...led by a compact EVENT answer; a missing time/location is stated, not
    # padded with newsletter boilerplate.
    assert "Bulldog Bulletin \u2014 Oct 17, 2025" in content
    assert "Time: not found" in content
    assert "Location: not found" in content
    # ...and the newsletter's boilerplate/footer is NOT dumped.
    assert "Volume 12, Issue 4" not in content
    assert "Unsubscribe" not in content
    assert "All rights reserved" not in content
    assert "Raleigh, NC 27601" not in content
    assert len(content) < len(_BULLETIN)          # a real excerpt, not the whole
    # The focused section is the SAME one persisted for the calendar handoff.
    selected = chat_service.get_conversation(OWNER, cid)["gmail_selected"]
    assert "October 17, 2025" in selected["facts"]["text"]
    assert "Unsubscribe" not in selected["facts"]["text"]


def test_sender_plus_topic_search_then_read_focuses_on_topic(rig):
    # A search that names BOTH a sender and a topic carries only the TOPIC into
    # the selection anchor -- the sender operator is never treated as an anchor.
    _gmail_bot(rig["tmp"])
    cid = chat_service.create_conversation(OWNER, bot_id=BOT)[
        "conversation_id"]
    hits = [{"owner": OWNER, "id": f"m{i}", "thread_id": f"t{i}"}
            for i in range(1, 3)]
    frames1, _, _, cid = _run_with_worker(
        rig, 'find the email from Wake Christian about the 4th grade field trip',
        hits=hits,
        messages={f"m{i}": _message(
            f"m{i}", subject=f"Bulldog Bulletin {i}",
            sender="office@wakechristian.org",
            date=f"Mon, {i} Oct 2025 00:00:00 +0000")
            for i in range(1, 3)},
        conversation_id=cid)
    assert _terminal(frames1)["status"] == "complete"
    conv = chat_service.get_conversation(OWNER, cid)
    assert conv.get("gmail_focus") == "4th grade field trip"
    assert "Wake" not in conv["gmail_focus"]

    frames2, gmail2, _, _ = _run_with_worker(
        rig, "read number 2",
        reads={"m2": _newsletter_read("m2"), "m1": _newsletter_read("m1")},
        conversation_id=cid)
    assert rig["store"].submissions[-1]["task_text"] == (
        'gmail: read id m2 focus "4th grade field trip" siblings "m1"')
    assert gmail2.reads == ["m2", "m1"]
    content = _terminal(frames2)["content"] or ""
    assert "4th grade field trip" in content
    assert "October 17, 2025" in content
    assert "Unsubscribe" not in content
    assert "All rights reserved" not in content


def test_direct_gmail_read_id_keeps_full_body(rig):
    # A DIRECT ``gmail: read id <id>`` with NO originating search keeps the
    # existing full-message behavior: no anchor, no section extraction.
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(
        rig, "gmail: read id m1", reads={"m1": _newsletter_read()})
    assert rig["store"].submissions[-1]["task_text"] == "gmail: read id m1"
    content = _terminal(frames)["content"] or ""
    assert "Volume 12, Issue 4" in content            # the beginning
    assert "All rights reserved" in content           # and the end
    assert "Most relevant section" not in content


def test_gmail_message_read_id_keeps_full_body(rig):
    # The numbered-read focus applies ONLY when an anchor is carried; a plain
    # "read number N" with no originating search still fails closed.
    _gmail_bot(rig["tmp"])
    frames, gmail, _, _ = _run_with_worker(rig, "read number 1")
    assert rig["store"].submissions == []
    assert gmail.reads == []
    assert "numbered result" in (_terminal(frames)["content"] or "")


def test_focused_excerpt_is_bounded(rig):
    # The focused section is bounded -- a huge body never balloons the relay.
    huge = ("The 4th grade field trip is coming up.\n\n"
            + ("filler line about the school store and lunch menu.\n" * 200)
            + "\nThe 4th grade field trip meets in room 12.\n")
    section = serve._gmail_extract_focus_section(huge, "4th grade field trip")
    assert section
    assert len(section) <= serve._GMAIL_FOCUS_EXCERPT_MAX


def test_short_or_offtopic_body_falls_back_to_full(rig):
    # A SHORT body is shown whole (no extraction), an OFF-TOPIC anchor never
    # fabricates a section, and a single weak term-overlap in a long newsletter
    # does NOT collapse the read onto unrelated prose (coverage floor).
    short = "Dear parents, the field trip is on October 17."
    assert serve._gmail_extract_focus_section(short, "field trip") == ""
    assert serve._gmail_extract_focus_section(_BULLETIN, "swimming pool") == ""
    assert serve._gmail_extract_focus_section(_BULLETIN, "concert") == ""


# ═════════════════════════════════════════════════════════════════════════
# 12. bounded event-detail ENRICHMENT across the SAME result set
# ═════════════════════════════════════════════════════════════════════════

_DATE_HDR = "Mon, 6 Oct 2025 00:00:00 +0000"
_SEARCH_TEXT = "search my email for the 4th grade field trip in Oct"


def _event_read(mid, body, *, subject="Bulldog Bulletin"):
    return _read_message(mid, subject=subject,
                         sender="news@bulldogacademy.org",
                         date=_DATE_HDR, body=body)


def _seed_topic_search(rig, n):
    """Turn 1: a topic search stores n ordered hits + the focus anchor."""
    cid = chat_service.create_conversation(OWNER, bot_id=BOT)[
        "conversation_id"]
    _run_with_worker(
        rig, _SEARCH_TEXT,
        hits=[{"owner": OWNER, "id": f"m{i}", "thread_id": f"t{i}"}
              for i in range(1, n + 1)],
        messages={f"m{i}": _message(f"m{i}", subject="Bulldog Bulletin",
                                    sender="news@bulldogacademy.org",
                                    date=_DATE_HDR) for i in range(1, n + 1)},
        conversation_id=cid)
    return cid


@pytest.mark.parametrize("text", [
    'gmail: read id m1 siblings "m2,m3"',
    'gmail: read id m1 focus "field trip" siblings "m2"',
])
def test_canonical_gmail_task_accepts_sibling_forms(text):
    assert serve.canonical_gmail_task(text) == text


@pytest.mark.parametrize("text", [
    'gmail: read id m1 siblings m2',                 # unquoted
    'gmail: read id m1 siblings ""',                 # empty
    'gmail: read id m1 siblings "m2 m3"',            # whitespace in an id
    'gmail: read id m1 siblings "' + ("x" * 600) + '"',   # over the id ceiling
])
def test_canonical_gmail_task_rejects_malformed_siblings(text):
    assert serve.canonical_gmail_task(text) is None


def test_siblings_suffix_is_capped_and_drops_junk():
    suffix = serve._gmail_siblings_suffix(
        [f"m{i}" for i in range(20)] + ["bad id", ""])
    csv = suffix.split('"')[1]
    assert len(csv.split(",")) <= serve._GMAIL_ENRICH_MAX_SIBLINGS
    assert "bad id" not in suffix


def test_focused_read_enriches_a_missing_time_from_a_sibling(rig):
    # "details split across two matching newsletters": the selected email has
    # the date + location, a SAME-EVENT sibling has the TIME -- so the answer
    # (and the persisted facts) fill the time from the sibling.
    _gmail_bot(rig["tmp"])
    cid = _seed_topic_search(rig, 2)
    primary = ("The 4th grade field trip is on October 17, 2025.\n"
               "Location: Science Museum\n")
    sibling = ("The 4th grade field trip runs from 9:00 am to 2:00 pm "
               "on October 17, 2025.\n")
    frames, gmail, _, _ = _run_with_worker(
        rig, "read number 1",
        reads={"m1": _event_read("m1", primary),
               "m2": _event_read("m2", sibling)},
        conversation_id=cid)
    assert gmail.reads == ["m1", "m2"]           # selected + ONE sibling
    assert gmail.searches == []                  # no second search
    content = _terminal(frames)["content"] or ""
    assert "Time: 9:00 am \u2013 2:00 pm" in content
    assert "Location: Science Museum" in content
    selected = chat_service.get_conversation(OWNER, cid)["gmail_selected"]
    assert selected["facts"]["start"] == "09:00"
    assert selected["facts"]["end"] == "14:00"
    assert selected["facts"]["needs"] == []


def test_focused_read_conflicting_times_stay_ambiguous(rig):
    # Two SAME-EVENT siblings disagree on the time: the read must NOT pick one.
    _gmail_bot(rig["tmp"])
    cid = _seed_topic_search(rig, 3)
    primary = "The 4th grade field trip is on October 17, 2025.\n"
    a = ("The 4th grade field trip runs from 9:00 am to 2:00 pm "
         "on October 17, 2025.\n")
    b = ("The 4th grade field trip runs from 10:00 am to 3:00 pm "
         "on October 17, 2025.\n")
    frames, _, _, _ = _run_with_worker(
        rig, "read number 1",
        reads={"m1": _event_read("m1", primary),
               "m2": _event_read("m2", a), "m3": _event_read("m3", b)},
        conversation_id=cid)
    content = _terminal(frames)["content"] or ""
    assert "Time: conflicting across the emails" in content
    selected = chat_service.get_conversation(OWNER, cid)["gmail_selected"]
    assert selected["facts"]["start"] is None
    assert "start" in (selected["facts"].get("conflicts") or [])


def test_focused_read_states_when_no_time_or_location_is_available(rig):
    # A single matching email with a date but no time/location: say so plainly.
    _gmail_bot(rig["tmp"])
    cid = _seed_topic_search(rig, 1)
    frames, gmail, _, _ = _run_with_worker(
        rig, "read number 1",
        reads={"m1": _event_read(
            "m1", "The 4th grade field trip is on October 17, 2025.\n")},
        conversation_id=cid)
    assert gmail.reads == ["m1"]                 # no siblings -> no enrichment
    content = _terminal(frames)["content"] or ""
    assert "Time: not found" in content
    assert "Location: not found" in content
    assert "Unsubscribe" not in content


def test_enrichment_ignores_an_unrelated_nearby_event(rig):
    # A sibling states a DIFFERENT event that happens to share the date: it must
    # not contaminate the target's missing time.
    _gmail_bot(rig["tmp"])
    cid = _seed_topic_search(rig, 2)
    primary = "The 4th grade field trip is on October 17, 2025.\n"
    unrelated = ("The fall festival runs from 6:00 pm to 8:00 pm "
                 "on October 17, 2025.\n")
    frames, _, _, _ = _run_with_worker(
        rig, "read number 1",
        reads={"m1": _event_read("m1", primary),
               "m2": _event_read("m2", unrelated)},
        conversation_id=cid)
    content = _terminal(frames)["content"] or ""
    assert "Time: not found" in content
    assert "6:00 pm" not in content
    selected = chat_service.get_conversation(OWNER, cid)["gmail_selected"]
    assert selected["facts"]["start"] is None


def test_enrichment_is_bounded_in_count_and_body_size(rig):
    # Five matching hits: the read reads the selected + at most the capped
    # number of siblings, and a huge sibling body cannot balloon the reply.
    _gmail_bot(rig["tmp"])
    cid = _seed_topic_search(rig, serve._GMAIL_MAX_SEARCH_RESULTS)
    primary = "The 4th grade field trip is on October 17, 2025.\n"
    huge = ("The 4th grade field trip runs from 9:00 am to 2:00 pm on "
            "October 17, 2025.\n" + ("filler about the school store.\n" * 400))
    reads = {f"m{i}": _event_read(f"m{i}", huge if i > 1 else primary)
             for i in range(1, serve._GMAIL_MAX_SEARCH_RESULTS + 1)}
    frames, gmail, _, _ = _run_with_worker(
        rig, "read number 1", reads=reads, conversation_id=cid)
    assert len(gmail.reads) <= 1 + serve._GMAIL_ENRICH_MAX_SIBLINGS
    content = _terminal(frames)["content"] or ""
    assert len(content) <= serve._GMAIL_RESULT_CHAR_LIMIT + 64
