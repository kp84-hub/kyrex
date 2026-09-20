"""Owner-scoped Delete bot (explicit name confirmation) - focused coverage.

Run: python3 -m pytest test_bot_rework_delete.py
"""
import os
import sys
import tempfile
import uuid

import pytest

os.environ.setdefault("GITHUB_CLIENT_ID", "test-client")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-secret")
os.environ.setdefault("WEB_ALLOWED_GITHUB_USERNAME", "allowed-user")
os.environ.setdefault("KYREX_DATA_DIR", "/tmp/kyrex-bot-rework-delete-tests")
os.environ.setdefault("WEB_SESSION_SECRET", "bot-rework-delete-test-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "client-secret-value")
os.environ.setdefault("GOOGLE_REDIRECT_URI",
                      "https://kyrex.example/api/connections/google/callback")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_CLOUD = os.path.dirname(os.path.dirname(_BACKEND))
for _p in (_BACKEND, _CLOUD):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import main
import bots
import chat_service
import connectors
import provider_profiles
import browser_hosts as bh

_SECRET = "sk-secret-delete-1234"
_PROFILE_ID = "del-prof"
_HOST_ID = "ovh-ny-01"
_PRESERVED = ["conversations", "task_history", "google_authorization",
              "provider_profiles", "calendar_events"]


def _reset():
    bots.save_bots({})
    root = chat_service._chat_root()
    for p in root.rglob("*.json"):
        p.unlink()
    for p in root.rglob("*.json.tmp"):
        p.unlink()
    chat_service._engine_sessions.clear()
    # The shared worker task store lives in the (persistent) data dir; clear
    # its task rows so a task left behind by an earlier run cannot block a
    # later delete with a stale in-flight conflict.
    try:
        store = main.store
        with store._lock:
            for table in ("task_events", "approval_requests", "tasks"):
                store._conn.execute(f"DELETE FROM {table}")
            store._conn.commit()
    except Exception:
        pass
    try:
        connectors.default_store().path.unlink(missing_ok=True)
    except Exception:
        pass
    for user in ("alice", "bob"):
        try:
            provider_profiles._path(user).unlink(missing_ok=True)
        except Exception:
            pass
    try:
        p = bh._registry_path()
        if p.exists():
            p.unlink()
    except Exception:
        pass


def setup_function():
    _reset()
    main.sessions["sess-alice"] = "alice"
    main.sessions["sess-bob"] = "bob"
    bh.enroll_host("alice", _HOST_ID, name="NY", allowlist=["example.com"])


def teardown_function():
    _reset()


def _client(user=None):
    from fastapi.testclient import TestClient
    cookies = {"session": f"sess-{user}"} if user else {}
    return TestClient(main.app, cookies=cookies)


def _profile(owner="alice"):
    provider_profiles.save_profile(owner, {
        "id": _PROFILE_ID, "name": "Delete Test Profile",
        "provider": "openai", "base_url": "https://api.openai.com/v1",
        "api_key": _SECRET, "models": ["gpt-test"]})
    return _PROFILE_ID


def _bot(bot_id="qa", owner="alice", name=None, status="running",
         allowlist=None):
    return bots.add_bot(
        bot_id, name or f"Bot {bot_id}", "openai:gpt-test",
        tempfile.mkdtemp(prefix="kx-del-rift-"), status=status, owner=owner,
        browser_allowlist=(allowlist if allowlist is not None
                           else ["example.com"]),
        provider_profile_id=_profile(owner))


def _submit_task(bot, *, status="queued"):
    task_id = f"del-task-{uuid.uuid4().hex}"
    main.store.submit(bot["owner"], "do something", bot_id=bot["id"],
                      rift=bot["rift"], task_id=task_id, resolve_bot=False)
    if status != "queued":
        main.store.set_status(task_id, status)
    return task_id


def _connect_google(owner="alice"):
    store = connectors.default_store()
    begin = store.begin_oauth(owner)
    store.complete_oauth(
        owner, begin["state"], "auth-code",
        exchange=lambda code, redirect, client: {
            "access_token": "ya29.SECRET-ACCESS",
            "refresh_token": "1//SECRET-REFRESH", "expires_in": 3600})
    return store


def _delete(client, bot_id, body):
    return client.post(f"/api/bots/{bot_id}/delete", json=body)


def test_exact_name_confirmation_deletes_only_that_bot():
    _bot("qa", name="Bot qa")
    _bot("dev", name="Bot dev")
    r = _delete(_client("alice"), "qa", {"confirm_name": "Bot qa"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == "qa"
    assert body["name"] == "Bot qa"
    assert body["preserved"] == _PRESERVED
    assert "qa" not in bots.load_bots()
    assert "dev" in bots.load_bots()


@pytest.mark.parametrize("body", [
    {}, {"confirm_name": ""}, {"confirm_name": "Bot qb"},
    {"confirm_name": "bot qa"}])
def test_missing_or_wrong_name_is_rejected_and_changes_nothing(body):
    _bot("qa", name="Bot qa")
    bh.bind_bot("alice", "qa", _HOST_ID)
    r = _delete(_client("alice"), "qa", body)
    assert r.status_code == 400, r.text
    assert "confirm" in r.json()["detail"].lower()
    assert "qa" in bots.load_bots()
    assert bh.binding_for("alice", "qa") == _HOST_ID


def test_anonymous_caller_is_denied():
    _bot("qa", name="Bot qa")
    r = _delete(_client(), "qa", {"confirm_name": "Bot qa"})
    assert r.status_code == 401, r.text
    assert "qa" in bots.load_bots()


def test_cross_owner_delete_is_denied():
    _bot("qa", owner="alice", name="Bot qa")
    r = _delete(_client("bob"), "qa", {"confirm_name": "Bot qa"})
    assert r.status_code == 403, r.text
    assert "qa" in bots.load_bots()


def test_unknown_bot_is_not_found():
    r = _delete(_client("alice"), "ghost", {"confirm_name": "ghost"})
    assert r.status_code == 404, r.text


def test_repeated_deletion_returns_404():
    _bot("qa", name="Bot qa")
    c = _client("alice")
    assert _delete(c, "qa", {"confirm_name": "Bot qa"}).status_code == 200
    assert "qa" not in bots.load_bots()
    assert _delete(c, "qa", {"confirm_name": "Bot qa"}).status_code == 404


def test_delete_removes_registry_and_configuration_entry():
    _bot("qa", name="Bot qa", allowlist=["example.com", "docs.example.com"])
    r = _delete(_client("alice"), "qa", {"confirm_name": "Bot qa"})
    assert r.status_code == 200, r.text
    with pytest.raises(KeyError):
        bots.get_bot("qa")
    assert "qa" not in bots.load_bots()


def test_delete_drops_binding_but_keeps_the_host():
    _bot("qa", name="Bot qa")
    bh.bind_bot("alice", "qa", _HOST_ID)
    assert bh.binding_for("alice", "qa") == _HOST_ID
    r = _delete(_client("alice"), "qa", {"confirm_name": "Bot qa"})
    assert r.status_code == 200, r.text
    assert bh.binding_for("alice", "qa") == ""
    assert bh._record(_HOST_ID) is not None
    assert _HOST_ID in {h["host_id"] for h in bh.list_hosts("alice")}


@pytest.mark.parametrize("task_status", ["queued", "running",
                                         "awaiting_approval"])
def test_delete_is_refused_while_work_is_in_flight(task_status):
    bot = _bot("busy", name="Bot busy")
    bh.bind_bot("alice", "busy", _HOST_ID)
    task_id = _submit_task(bot, status=task_status)
    r = _delete(_client("alice"), "busy", {"confirm_name": "Bot busy"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert task_id not in detail
    assert task_status not in detail
    assert "busy" in bots.load_bots()
    assert bh.binding_for("alice", "busy") == _HOST_ID
    assert main.store.get(task_id) is not None


def test_stop_failure_is_sanitized_and_atomic(monkeypatch):
    _bot("qa", name="Bot qa", status="running")
    bh.bind_bot("alice", "qa", _HOST_ID)
    real_set_status = chat_service.bots.set_status

    def boom(bot_id, status):
        if status == chat_service.bots.STATUS_STOPPED:
            raise RuntimeError("SECRET-STOP-DETAIL")
        return real_set_status(bot_id, status)

    monkeypatch.setattr(chat_service.bots, "set_status", boom)
    r = _delete(_client("alice"), "qa", {"confirm_name": "Bot qa"})
    assert r.status_code == 500, r.text
    assert r.json()["detail"] == "could not delete bot"
    assert "SECRET-STOP-DETAIL" not in r.text
    assert "qa" in bots.load_bots()
    assert bots.get_bot("qa")["status"] == "running"
    assert bh.binding_for("alice", "qa") == _HOST_ID


def test_registry_removal_failure_is_sanitized_and_atomic(monkeypatch):
    _bot("qa", name="Bot qa", status="running")
    bh.bind_bot("alice", "qa", _HOST_ID)

    def boom(bot_id):
        raise RuntimeError("SECRET-REGISTRY-DETAIL")

    monkeypatch.setattr(chat_service.bots, "remove_bot", boom)
    r = _delete(_client("alice"), "qa", {"confirm_name": "Bot qa"})
    assert r.status_code == 500, r.text
    assert r.json()["detail"] == "could not delete bot"
    assert "SECRET-REGISTRY-DETAIL" not in r.text
    assert "qa" in bots.load_bots()
    assert bots.get_bot("qa")["status"] == "running"
    assert bh.binding_for("alice", "qa") == _HOST_ID


def test_delete_preserves_conversations_tasks_oauth_profiles_and_events():
    bot = _bot("qa", name="Bot qa")
    owner = "alice"
    conv = chat_service.create_conversation(owner, bot_id="qa")
    conv_id = conv["conversation_id"]
    task_id = _submit_task(bot, status="done")
    store = _connect_google(owner)
    assert store.status(owner)["status"] == "connected"
    connectors_before = store.path.read_bytes()
    assert provider_profiles.get_profile(owner, _PROFILE_ID) is not None
    r = _delete(_client(owner), "qa", {"confirm_name": "Bot qa"})
    assert r.status_code == 200, r.text
    assert r.json()["preserved"] == _PRESERVED
    assert "qa" not in bots.load_bots()
    assert conv_id in {c["conversation_id"]
                       for c in chat_service.list_conversations(owner)}
    stored = chat_service.get_conversation(owner, conv_id)
    assert stored is not None and stored["bot_id"] == "qa"
    task = main.store.get(task_id)
    assert task is not None and task.get("bot_id") == "qa"
    assert connectors.default_store().status(owner)["status"] == "connected"
    assert store.path.read_bytes() == connectors_before
    assert provider_profiles.get_profile(owner, _PROFILE_ID) is not None
