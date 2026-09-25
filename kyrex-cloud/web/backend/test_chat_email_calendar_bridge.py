"""End-to-end regressions for the bounded email -> calendar handoff.

Proves the selected-Bot route added to chat_service:

  * a bounded Gmail READ persists the SELECTED message + its extracted event
    facts in the conversation ("gmail_selected"), and a fresh search clears it;
  * a bounded PRONOUN request ("add that to my calendar") resolves "that" to
    the selected email, validates the required facts, asks ONLY for genuinely
    missing/conflicting details, and -- when complete -- submits ONE
    owner-scoped ``email_calendar`` create;
  * the create runs through the OWNER's existing Calendar create path with a
    mandatory confirmation gate and NO special Calendar Bot role;
  * a missing write scope, an absent selection, and a foreign owner all fail
    closed with NO task.

Run: python3 -m pytest test_chat_email_calendar_bridge.py
"""

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from unittest.mock import patch

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("WEB_SESSION_SECRET", "chat-email-calendar-bridge-test")
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
BOT = "mail-bot"
_PROFILE_ID = "email-calendar-bridge-profile"

_BODY = (
    "Dear Parents,\n"
    "The 4th grade field trip to the museum will be held on "
    "November 14, 2025.\n"
    "It runs from 9:00 am to 2:00 pm.\n"
    "Location: Raleigh Museum of Natural Sciences\n"
)


async def _frames(agen):
    return [frame async for frame in agen]


def _terminal(frames):
    status = [f for f in frames if f.get("type") == "status"]
    return status[-1] if status else None


class _RecordingStore:
    def __init__(self, store):
        self._store = store
        self.submissions = []

    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        return self._store.submit(**kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


def _read_message(mid, *, subject, sender, date, body, snippet="",
                  truncated=False):
    return {
        "owner": OWNER, "id": mid, "thread_id": "t-" + mid,
        "snippet": snippet,
        "headers": {"Subject": subject, "From": sender, "Date": date},
        "body": body, "body_type": "text", "truncated": truncated,
    }


class _FakeGmailRead:
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
        self.searches.append({"query": query, "page_token": page_token})
        if self._error is not None:
            raise self._error
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


class _FakeWriter:
    def __init__(self, created=None, error=None):
        self.created = dict(created or {"id": "evt-1"})
        self._error = error
        self.events = []

    def create_event(self, event):
        self.events.append(dict(event))
        if self._error is not None:
            raise self._error
        return dict(self.created)


class _FakeStore:
    """A combined owner-scoped connector seam for gmail read + calendar write."""

    def __init__(self, gmail=None, *, read_available=True,
                 write_available=True, writer=None):
        self._gmail = gmail or _FakeGmailRead()
        self._read_available = bool(read_available)
        self._write_available = bool(write_available)
        self._writer = writer or _FakeWriter()
        self.owner = None

    def gmail(self, owner, **kwargs):
        self.owner = owner
        return self._gmail

    def gmail_read_available(self, owner, **kwargs):
        self.owner = owner
        return self._read_available

    def calendar_write_available(self, owner, **kwargs):
        self.owner = owner
        return self._write_available

    def calendar_writer(self, owner, **kwargs):
        self.owner = owner
        return self._writer


def _ensure_profile(owner):
    existing = provider_profiles.get_profile(owner, _PROFILE_ID)
    models = list(existing["models"]) if existing else ["x"]
    if "x" not in models:
        models.append("x")
    provider_profiles.save_profile(owner, {
        "id": _PROFILE_ID,
        "name": "Email Calendar Bridge Profile",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": os.environ.get("KYREX_API_KEY") or "sk-test",
        "models": models,
    })
    return _PROFILE_ID


def _mail_bot(tmp, *, bot_id=BOT, owner=OWNER, status="running", policy=None):
    return bots.add_bot(
        bot_id, "Mail Reader", "openai:x", str(tmp),
        owner=owner, status=status,
        policy=serve.gmail_reader_preset_policy() if policy is None else policy,
        provider_profile_id=_ensure_profile(owner))


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
    fake = _FakeStore()
    monkeypatch.setattr(connectors, "default_store", lambda: fake)
    yield {"store": store, "raw": raw, "tmp": tmp_path, "fake": fake}
    chat_service._engine_sessions.clear()


def _auto_send(bot_id, *, approve=True):
    """A worker `send` that resolves a T1 approval so the gate never blocks."""
    sent = []

    def send(chat_id, text):
        mid = f"msg-{len(sent)}"
        sent.append({"chat_id": chat_id, "text": text, "mid": mid})
        if text.startswith("\u26a0\ufe0f") and "T1:" in text:
            def _resolve():
                for _ in range(500):
                    if (str(bot_id), mid) in serve.pending_approvals:
                        serve.handle_approval_reply(
                            chat_id, "y" if approve else "n",
                            reply_to_id=mid, session_key=str(bot_id))
                        return
                    time.sleep(0.01)
            threading.Thread(target=_resolve, daemon=True).start()
        return mid

    return send, sent


def _run(rig, text, *, cid=None, send=None, bot_id=BOT):
    worker = TaskWorker(rig["raw"], worker_id="ec-" + uuid.uuid4().hex[:6],
                        idle_sleep=0.01, heartbeat_interval=0.01, send=send)
    worker.start()
    try:
        if cid is None:
            cid = chat_service.create_conversation(
                OWNER, bot_id=bot_id)["conversation_id"]
        frames = asyncio.run(_frames(chat_service.stream_chat(OWNER, cid, text)))
    finally:
        worker.stop()
    return frames, cid


def _selected(conversation_id):
    conv = chat_service.get_conversation(OWNER, conversation_id) or {}
    return conv.get("gmail_selected")


# ═════════════════════════════════════════════════════════════════════════
# 1. the selected email + extracted facts are persisted in conversation state
# ═════════════════════════════════════════════════════════════════════════

def test_read_persists_selected_email_and_facts(rig):
    _mail_bot(rig["tmp"])
    rig["fake"]._gmail = _FakeGmailRead(
        hits=[{"owner": OWNER, "id": "m1", "thread_id": "t1"}],
        reads={"m1": _read_message(
            "m1", subject="4th Grade Field Trip",
            sender="Wake Christian Academy <office@wakechristian.org>",
            date="Mon, 1 Sep 2025 00:00:00 +0000", body=_BODY)})
    frames, cid = _run(
        rig, "read the email from Wake Christian about the 4th grade field trip")

    subs = rig["store"].submissions
    assert len(subs) == 1 and subs[0]["executor_prefix"] == "gmail", subs
    assert subs[0]["task_text"] == (
        'gmail: read from:"Wake Christian" "the 4th grade field trip"')
    assert rig["fake"]._gmail.searches[0]["query"] == (
        'from:"Wake Christian" "the 4th grade field trip"')
    assert _terminal(frames)["status"] == "complete"

    selected = _selected(cid)
    assert selected is not None and selected["id"] == "m1"
    facts = selected["facts"]
    assert facts["date"] == "2025-11-14"
    assert facts["start"] == "09:00" and facts["end"] == "14:00"
    assert facts["location"] == "Raleigh Museum of Natural Sciences"
    assert facts["needs"] == []


def test_fresh_search_clears_a_stale_selection(rig):
    _mail_bot(rig["tmp"])
    rig["fake"]._gmail = _FakeGmailRead(
        hits=[{"owner": OWNER, "id": "m1", "thread_id": "t1"}],
        reads={"m1": _read_message(
            "m1", subject="Field Trip", sender="a@b.org",
            date="Mon, 1 Sep 2025 00:00:00 +0000", body=_BODY)})
    _, cid = _run(rig, "read the email id 18f2ab9c3d")
    _, cid = _run(rig, "find emails from Randy", cid=cid)
    assert _selected(cid) is None


# ═════════════════════════════════════════════════════════════════════════
# 2. the pronoun handoff resolves "that" to the selected email
# ═════════════════════════════════════════════════════════════════════════

def test_handoff_without_a_selection_fails_closed(rig):
    _mail_bot(rig["tmp"])
    frames, cid = _run(rig, "add that to my calendar")
    assert rig["store"].submissions == []
    terminal = _terminal(frames)
    assert terminal["status"] == "complete"
    assert "selected email" in terminal["content"]


def test_handoff_completes_and_creates_event(rig):
    _mail_bot(rig["tmp"])
    rig["fake"]._gmail = _FakeGmailRead(
        hits=[{"owner": OWNER, "id": "m1", "thread_id": "t1"}],
        reads={"m1": _read_message(
            "m1", subject="4th Grade Field Trip",
            sender="Wake Christian Academy <office@wakechristian.org>",
            date="Mon, 1 Sep 2025 00:00:00 +0000", body=_BODY)})
    _, cid = _run(
        rig, "read the email from Wake Christian about the 4th grade field trip")
    send, sent = _auto_send(BOT)
    frames, cid = _run(rig, "add that to my calendar", cid=cid, send=send)

    subs = rig["store"].submissions
    assert len(subs) == 2, subs
    create = subs[-1]
    assert create["executor_prefix"] == "email_calendar"
    intent = json.loads(create["task_text"])
    assert intent == {
        "title": "4th Grade Field Trip", "start": "2025-11-14T09:00:00",
        "end": "2025-11-14T14:00:00", "all_day": False}
    # The event was created through the owner's existing create path.
    assert rig["fake"]._writer.events == [{
        "summary": "4th Grade Field Trip",
        "start": {"dateTime": "2025-11-14T09:00:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2025-11-14T14:00:00", "timeZone": "America/New_York"},
    }]
    terminal = _terminal(frames)
    assert terminal["status"] == "complete"
    assert "Created" in terminal["content"]
    # The T1 confirmation gate ran before any provider call.
    assert any("T1:" in s["text"] for s in sent)


def test_handoff_denied_at_gate_creates_nothing(rig):
    _mail_bot(rig["tmp"])
    rig["fake"]._gmail = _FakeGmailRead(
        hits=[{"owner": OWNER, "id": "m1", "thread_id": "t1"}],
        reads={"m1": _read_message(
            "m1", subject="4th Grade Field Trip",
            sender="Wake Christian Academy <office@wakechristian.org>",
            date="Mon, 1 Sep 2025 00:00:00 +0000", body=_BODY)})
    _, cid = _run(
        rig, "read the email from Wake Christian about the 4th grade field trip")
    send, _ = _auto_send(BOT, approve=False)
    _run(rig, "add that to my calendar", cid=cid, send=send)
    assert rig["fake"]._writer.events == []


# ═════════════════════════════════════════════════════════════════════════
# 3. missing / ambiguous details ask, and create NOTHING
# ═════════════════════════════════════════════════════════════════════════

def _seed_selected(cid, *, facts):
    conv = chat_service.get_conversation(OWNER, cid)
    conv["gmail_selected"] = {
        "id": "m1", "subject": facts.get("title") or "",
        "from": "office@wakechristian.org", "date": "Mon, 1 Sep 2025 00:00:00 +0000",
        "snippet": "", "facts": facts,
    }
    chat_service._write(OWNER, conv)


def test_missing_time_asks_and_creates_nothing(rig):
    _mail_bot(rig["tmp"])
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    cid = recv["conversation_id"]
    _seed_selected(cid, facts={
        "title": "Field Trip", "date": "2025-05-03", "start": None, "end": None,
        "all_day": False, "location": None, "missing": ["time"],
        "ambiguous": [], "needs": ["time"], "date_ambiguous": False,
        "time_ambiguous": False})
    frames, _ = _run(rig, "add that to my calendar", cid=cid)
    assert rig["store"].submissions == []
    terminal = _terminal(frames)
    assert terminal["status"] == "complete"
    assert "start and end time" in terminal["content"]


def test_ambiguous_date_asks_and_creates_nothing(rig):
    _mail_bot(rig["tmp"])
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    cid = recv["conversation_id"]
    _seed_selected(cid, facts={
        "title": "Field Trip", "date": None, "start": "09:00", "end": "14:00",
        "all_day": False, "location": None, "missing": [], "ambiguous": ["date"],
        "needs": ["date"], "date_ambiguous": True, "time_ambiguous": False})
    frames, _ = _run(rig, "add that to my calendar", cid=cid)
    assert rig["store"].submissions == []
    assert "which date" in _terminal(frames)["content"]


# ═════════════════════════════════════════════════════════════════════════
# 4. owner scope, no special Calendar Bot role, write-scope fail closed
# ═════════════════════════════════════════════════════════════════════════

def test_handoff_needs_no_calendar_bot_role(rig):
    # An ordinary Mail Reader Bot (NOT a Calendar Bot / Calendar Writer) can
    # hand an email to the calendar: route readiness is owner-scoped, NOT
    # policy-gated.
    bot = _mail_bot(rig["tmp"], policy=serve.gmail_reader_preset_policy())
    assert dev_bot.email_calendar_route_ready(bot) is True
    assert serve.is_calendar_writer_policy(bot.get("policy")) is False
    assert serve.calendar_bot_granted(bot) is False


def test_submit_email_calendar_surface_is_pinned(rig):
    import inspect
    params = set(inspect.signature(
        dev_bot.submit_email_calendar_task).parameters)
    assert params == {"user", "bot", "task_text", "store", "conversation_id"}


def test_submit_rejects_a_non_intent(rig):
    _mail_bot(rig["tmp"])
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_email_calendar_task(
            OWNER, bots.get_bot(BOT), "not a create intent",
            store=rig["store"])
    assert rig["store"].submissions == []


def test_foreign_owner_submission_fails_closed(rig):
    _mail_bot(rig["tmp"])
    with pytest.raises(dev_bot.DevBotError, match="another owner"):
        dev_bot.submit_email_calendar_task(
            "bob", bots.get_bot(BOT),
            json.dumps({"title": "X", "start": "2025-01-01T09:00:00",
                        "end": "2025-01-01T10:00:00", "all_day": False}),
            store=rig["store"])
    assert rig["store"].submissions == []


# ═════════════════════════════════════════════════════════════════════════
# 5. a NUMBERED selection keeps the ORIGINAL topic: "add that to my calendar"
#    extracts from the focused field-trip section, not the whole newsletter
# ═════════════════════════════════════════════════════════════════════════

_BULLETIN = (
    "BULLDOG BULLETIN - Volume 12, Issue 4\n"
    "\n"
    "A note from the principal: thank you for a wonderful start to the "
    "school year. Please review the lunch menu and the spirit-wear store, "
    "and check the carpool schedule before the weather turns.\n"
    "\n"
    "The 4th grade field trip is scheduled for November 14, 2025. It runs "
    "from 9:00 am to 2:00 pm. Permission slips are due one week before the "
    "trip; parents are welcome to chaperone.\n"
    "Location: Raleigh Museum of Natural Sciences\n"
    "\n"
    "Save the date for the fall festival on the second Saturday of "
    "November. Volunteers are always appreciated, and the book fair runs "
    "all week in the media center.\n"
    "\n"
    "The lunch menu rotates every two weeks. Spirit wear and yearbooks can "
    "be ordered online through the school store at any time.\n"
    "\n"
    "You received this email because you are subscribed to the Bulldog "
    "Bulletin. Unsubscribe | Manage preferences | View in browser\n"
    "\n"
    "(c) 2025 Bulldog Academy. All rights reserved. Follow us on social "
    "media.\n"
)


def test_numbered_read_keeps_topic_for_calendar_extraction(rig):
    _mail_bot(rig["tmp"])
    rig["fake"]._gmail = _FakeGmailRead(
        hits=[{"owner": OWNER, "id": f"m{i}", "thread_id": f"t{i}"}
              for i in range(1, 3)],
        messages={f"m{i}": _read_message(
            f"m{i}", subject=f"Bulldog Bulletin {i}",
            sender="office@wakechristian.org",
            date=f"Mon, {i} Oct 2025 00:00:00 +0000", body=_BULLETIN)
            for i in range(1, 3)},
        reads={"m1": _read_message(
            "m1", subject="Bulldog Bulletin", sender="office@wakechristian.org",
            date="Mon, 6 Oct 2025 00:00:00 +0000", body=_BULLETIN)})

    # Turn 1: a topic-only search lists the numbered candidates.
    _, cid = _run(rig, "read my email and find the 4th grade field trip in Oct")
    # Turn 2: "read number 1" reads the selected message with the topic anchor.
    _, cid = _run(rig, "read number 1", cid=cid)
    assert rig["store"].submissions[-1]["task_text"] == (
        'gmail: read id m1 focus "4th grade field trip Oct"')

    selected = _selected(cid)
    assert selected is not None and selected["id"] == "m1"
    facts = selected["facts"]
    # The facts were extracted from the FOCUSED section, not the whole bulletin.
    assert facts["date"] == "2025-11-14"
    assert facts["start"] == "09:00" and facts["end"] == "14:00"
    assert facts["location"] == "Raleigh Museum of Natural Sciences"
    assert "Unsubscribe" not in facts["text"]
    assert "All rights reserved" not in facts["text"]
    assert facts["needs"] == []

    # Turn 3: "add that to my calendar" creates the event from that section.
    send, _ = _auto_send(BOT)
    frames, _ = _run(rig, "add that to my calendar", cid=cid, send=send)
    create = rig["store"].submissions[-1]
    assert create["executor_prefix"] == "email_calendar"
    intent = json.loads(create["task_text"])
    assert intent == {
        "title": "Bulldog Bulletin", "start": "2025-11-14T09:00:00",
        "end": "2025-11-14T14:00:00", "all_day": False}
    assert rig["fake"]._writer.events == [{
        "summary": "Bulldog Bulletin",
        "start": {"dateTime": "2025-11-14T09:00:00",
                  "timeZone": "America/New_York"},
        "end": {"dateTime": "2025-11-14T14:00:00",
                "timeZone": "America/New_York"},
    }]
    assert _terminal(frames)["status"] == "complete"


def test_missing_write_scope_asks_and_creates_nothing(rig):
    _mail_bot(rig["tmp"])
    rig["fake"]._write_available = False
    recv = chat_service.create_conversation(OWNER, bot_id=BOT)
    cid = recv["conversation_id"]
    _seed_selected(cid, facts={
        "title": "Field Trip", "date": "2025-05-03", "start": "09:00",
        "end": "14:00", "all_day": False, "location": None, "missing": [],
        "ambiguous": [], "needs": [], "date_ambiguous": False,
        "time_ambiguous": False})
    frames, _ = _run(rig, "add that to my calendar", cid=cid)
    assert rig["store"].submissions == []
    assert "write" in _terminal(frames)["content"].lower()
