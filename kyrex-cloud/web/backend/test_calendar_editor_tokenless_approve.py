"""Token-less T2 approvals get a host-generated token (end-to-end).

The Calendar Editor executor raises its mandatory T2 approval WITHOUT a token,
so the persisted record carried token ``""`` and the exact-token resolver could
never be satisfied by a non-empty reply. This proves the central host-side fix:

  * ``serve.run_task`` -- the single approval gate every executor funnels
    through -- generates a cryptographically random token whenever a T2
    approval arrives without one, so the LIVE in-memory entry, the operator
    prompt, and the durable approval record all carry the SAME token;
  * that makes a token-less Calendar Editor approval APPROVABLE through
    ``POST /api/task/{task_id}/approve`` (the delegated host-side action);
  * the executor's OWN ``APPROVED`` gate still stands: nothing is deleted
    before approval, and the executor only proceeds once the host resolves the
    approval with the exact token;
  * the token is never returned by the approve API or the delegated view, and
    never appears in a lifecycle/result event (it is written only to the
    approval record and the operator prompt);
  * a T1 approval is UNCHANGED (still token-less);
  * ``serve.handle_approval_reply``'s exact-token validation is unchanged.

Run: python3 -m pytest test_calendar_editor_tokenless_approve.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
_TMP = tempfile.mkdtemp(prefix="kyrex_cal_editor_tokenless_")
os.environ.setdefault("KYREX_DATA_DIR", _TMP)
os.environ.setdefault("KYREX_PROVIDER", "openai")
os.environ.setdefault("KYREX_MODEL", "gpt-test")
os.environ.setdefault("KYREX_API_KEY", "sk-test")
os.environ.setdefault("WEB_SESSION_SECRET", "cal-editor-tokenless-secret")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main  # noqa: E402
import chat_service  # noqa: E402
import chat_api  # noqa: E402
import bots  # noqa: E402
import serve  # noqa: E402
import delegation  # noqa: E402
import connectors  # noqa: E402
from task_store import CloudTaskStore, TaskWorker  # noqa: E402

CANON = "Remove this from calendar Level 6 Workout: Lower Body Pyramid Sets"
L6_TITLE = "Level 6 Workout: Lower Body Pyramid Sets"
L6_ID = "l6evtABC12345.xyz"
EDITOR_POLICY = {"cal:delete": 2}
COORD_POLICY = {"fs:read": 0, "bot:delegate": 0}


class Request:
    def __init__(self, cookies=None):
        self.headers = {}
        self.cookies = cookies or {}

    async def json(self):
        return {}


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = CloudTaskStore(db_path=tmp_path / "tokenless.db")
    monkeypatch.setattr(main, "store", s)
    monkeypatch.setattr(chat_service, "_task_store", lambda: s)
    monkeypatch.setattr(delegation, "CloudTaskStore", lambda *a, **k: s)
    monkeypatch.setattr(
        serve, "_bot_llm_config",
        lambda bot: {"provider": "openai", "api_key": "x", "model": "m"})
    main.sessions.clear()
    serve.pending_approvals.clear()
    yield s
    serve.pending_approvals.clear()


def _bot(monkeypatch, tmp_path, bot_id, *, owner="alice", policy):
    monkeypatch.setattr(bots, "BOTS_FILE", str(tmp_path / "bots.json"))
    try:
        return bots.get_bot(bot_id)
    except KeyError:
        rift = tmp_path / f"rift-{bot_id}"
        rift.mkdir(parents=True, exist_ok=True)
        return bots.add_bot(
            bot_id, f"Bot {bot_id}", "test:model", str(rift), policy=policy,
            status="running", owner=owner, provider_profile_id="p1")


def _cookie(user):
    tok = f"sess-{user}"
    main.sessions[tok] = user
    return Request(cookies={"session": tok})


def _wait(predicate, timeout=25.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _events(store, task_id, etype):
    return [e for e in store.get_events(task_id) if e["type"] == etype]


# A deterministic connector stand-in: the delete PREFLIGHT (host-side, in this
# process) resolves the title to ONE event; the executor's own delete call runs
# in a SUBPROCESS against the real (unconnected) connector, so a post-gate
# failure there is exactly what proves the executor cleared its T2 gate.
class _FakeCal:
    def __init__(self, events):
        self._events = events

    def events(self, **kw):
        return self._events


class _FakeStore:
    def __init__(self, events):
        self._events = events

    def preferred_calendar(self, owner, provider="google"):
        return "primary"

    def calendar(self, owner):
        return _FakeCal(self._events)


# ── 1. token-less Calendar Editor approval becomes approvable, e2e ─────

def test_tokenless_calendar_editor_approval_is_approvable_via_route(
        store, monkeypatch, tmp_path):
    monkeypatch.setattr(connectors, "default_store", lambda: _FakeStore(
        [{"id": L6_ID, "summary": L6_TITLE,
          "start": {"dateTime": "2026-01-06T09:00:00"}}]))
    chief = _bot(monkeypatch, tmp_path, "chief", policy=COORD_POLICY)
    _bot(monkeypatch, tmp_path, "editor", policy=EDITOR_POLICY)

    conv = chat_service.create_conversation("alice", title="Chief of Staff")
    cid = conv["conversation_id"]
    view = delegation.submit_delegation(
        "alice", chief, "editor", CANON, store=store,
        parent_conversation_id=cid)
    task_id = view["task_id"]
    assert store.get(task_id)["executor_prefix"] == "cal_edit"

    worker = TaskWorker(store, worker_id="tok-w", executor=serve.run_task,
                        max_workers=1)
    runner = threading.Thread(target=lambda: worker.execute_task(store.get(task_id)))
    runner.start()

    pending = _wait(lambda: store.get_pending_approval(task_id))
    assert pending is not None, "the Calendar Editor did not raise an approval"
    assert pending["tier"] == 2
    # The executor emitted NO token -- the HOST generated one.
    assert pending["token"], "a token-less T2 approval must get a token"
    token = pending["token"]

    # The executor's OWN gate: nothing has been attempted before approval.
    assert _events(store, task_id, "result") == [], "no delete before approval"

    # The token never reaches the delegated view or the approve response.
    synced = chat_service.sync_delegated_work("alice", cid)
    assert token not in json.dumps(synced)
    row = next(r for r in synced["delegations"] if r["task_id"] == task_id)
    assert "token" not in (row.get("approval") or {})

    out = chat_api.approve_delegated_task(task_id, _cookie("alice"))
    assert out == {"approved": True, "task_id": task_id}
    assert token not in json.dumps(out)
    assert store.get_pending_approval(task_id)["operator_reply"] == token

    # Deliver to the live gate -> the host writes APPROVED -> sole delete call.
    assert store.deliver_operator_replies() == 1
    runner.join(30)
    assert not runner.is_alive(), "the editor task did not finish"

    resolved = _events(store, task_id, "approval_resolved")
    assert resolved and resolved[-1]["payload"]["decision"] == "APPROVED"

    results = _events(store, task_id, "result")
    assert results, "the executor emitted no result"
    errors = results[-1]["payload"].get("errors") or []
    joined = " ".join(errors)
    # It got PAST the T2 gate (it attempted the real delete) rather than being
    # stopped by it.
    assert "not approved" not in joined, joined
    assert "calendar delete failed" in joined, joined
    assert token not in json.dumps(results[-1]["payload"])
    assert token not in json.dumps(resolved[-1]["payload"])


# ── 2. central generation for ANY token-less T2; T1 untouched ──────────

_T2_FAKE = '''
import sys, json
def emit(k, o):
    sys.stdout.write("KYREX_" + k + ":" + json.dumps(o) + "\\n"); sys.stdout.flush()
emit("APPROVAL", {"tier": 2, "summary": "confirm write", "detail": "destructive"})
d = sys.stdin.readline().strip()
emit("RESULT_JSON", {"status": "done", "final_response": "dec=" + d, "errors": []})
'''
_T1_FAKE = '''
import sys, json
def emit(k, o):
    sys.stdout.write("KYREX_" + k + ":" + json.dumps(o) + "\\n"); sys.stdout.flush()
emit("APPROVAL", {"tier": 1, "summary": "list the files", "detail": ""})
d = sys.stdin.readline().strip()
emit("RESULT_JSON", {"status": "done", "final_response": "dec=" + d, "errors": []})
'''


def _run_fake(store, monkeypatch, tmp_path, prefix, source, reply_of_pending):
    script = tmp_path / f"{prefix}.py"
    script.write_text(source)
    monkeypatch.setitem(serve.EXECUTORS, prefix, str(script))
    task_id = store.submit(session_key="webuser", task_text="do it",
                           executor_prefix=prefix)
    worker = TaskWorker(store, worker_id=f"w-{prefix}", executor=serve.run_task,
                        max_workers=1)
    runner = threading.Thread(target=lambda: worker.execute_task(store.get(task_id)))
    runner.start()
    pending = _wait(lambda: store.get_pending_approval(task_id))
    assert pending is not None, f"{prefix}: no approval raised"
    # Read the token BEFORE replying, then resolve with what the caller chooses.
    reply = reply_of_pending(pending)
    if reply is not None:
        assert store.record_operator_reply(task_id, reply) is True
        store.deliver_operator_replies()
    runner.join(30)
    assert not runner.is_alive(), f"{prefix}: task did not finish"
    return task_id, pending


def _final_response(store, task_id):
    result = _events(store, task_id, "result")[-1]
    return result["payload"].get("final_response")


def test_any_tokenless_t2_gets_a_generated_token(store, monkeypatch, tmp_path):
    # Resolve with the TOKEN THAT WAS GENERATED (never supplied by the executor).
    task_id, pending = _run_fake(
        store, monkeypatch, tmp_path, "fake_t2", _T2_FAKE,
        lambda p: p["token"])
    assert pending["tier"] == 2
    assert pending["token"], "a token-less T2 approval must get a token"
    assert _final_response(store, task_id) == "dec=APPROVED"


def test_t1_approval_stays_tokenless(store, monkeypatch, tmp_path):
    task_id, pending = _run_fake(
        store, monkeypatch, tmp_path, "fake_t1", _T1_FAKE, lambda p: "y")
    assert pending["tier"] == 1
    assert pending["token"] == "", "T1 approvals must NOT be given a token"
    assert _final_response(store, task_id) == "dec=APPROVED"


# ── 3. exact-token validation unchanged ───────────────────────────────

def test_handle_approval_reply_still_requires_the_exact_token():
    evt = threading.Event()
    serve.pending_approvals[("skey", "m1")] = {
        "event": evt, "chat_id": "alice", "tier": 2,
        "token": "TOK-exact", "result": None}
    # A wrong token is refused and does NOT resolve the approval.
    assert serve.handle_approval_reply(
        "alice", "TOK-wrong", reply_to_id="m1", session_key="skey") is False
    assert evt.is_set() is False
    # The exact token resolves it.
    assert serve.handle_approval_reply(
        "alice", "TOK-exact", reply_to_id="m1", session_key="skey") is True
    assert evt.is_set() is True
    assert serve.pending_approvals[("skey", "m1")]["result"] == "APPROVED"

    # T1 y/n behaviour is unchanged.
    evt1 = threading.Event()
    serve.pending_approvals[("s", "m2")] = {
        "event": evt1, "chat_id": "bob", "tier": 1, "token": "", "result": None}
    assert serve.handle_approval_reply(
        "bob", "n", reply_to_id="m2", session_key="s") is True
    assert serve.pending_approvals[("s", "m2")]["result"] == "DENIED"