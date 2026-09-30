"""Exercise the HTTP gateway with real durable task and conversation storage."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parent.parent):
    sys.path.insert(0, str(path))

import automation_api as api
import automation_rules
from task_store import CloudTaskStore

TOKEN = "automation-test-credential-" + "x" * 40
CID = "c" * 32


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("KYREX_DATA_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(
        require_user=lambda request: "alice"))
    for name, value in {
        "KYREX_AUTOMATION_ENABLED": "1", "KYREX_AUTOMATION_OWNER": "alice",
        "KYREX_AUTOMATION_TOKEN": TOKEN,
    }.items():
        monkeypatch.setenv(name, value)
    bot = {"id": "barrett", "name": "Email Bot", "owner": "alice", "status": "running", "rift": ""}
    monkeypatch.setattr(api.bots, "load_bots", lambda: {"barrett": bot})
    monkeypatch.setattr(api.chat_service, "_chat_root", lambda: tmp_path / "chat")
    conv = {"conversation_id": CID, "bot_id": "barrett", "messages": [],
            "title": "Barrett", "created_at": "2026-09-30T00:00:00+00:00"}
    path = api.chat_service._conv_path("alice", CID)
    path.write_text(json.dumps(conv))
    rule = automation_rules.create_rule("alice", "school@example.com", "barrett", CID)
    store = CloudTaskStore(db_path=tmp_path / "tasks.sqlite3")
    monkeypatch.setattr(api.chat_service, "_task_store", lambda: store)
    state = {"connected": True, "sender": "School <school@example.com>", "calls": []}

    def search(**kw):
        state["calls"].append(kw)
        return {"messages": [{"id": "mail123"}], "next_page_token": "next"}

    def message(mid):
        return {"owner": "alice", "id": mid, "headers": {"From": state["sender"]}}

    reader = SimpleNamespace(search=search, message=message)
    connector = SimpleNamespace(gmail_read_available=lambda owner: state["connected"],
                                gmail=lambda owner: reader)
    monkeypatch.setattr(api.connectors, "default_store", lambda: connector)
    app = FastAPI()
    app.include_router(api.router)
    client = TestClient(app, headers={"Authorization": "Bearer " + TOKEN})
    version = client.get("/api/automations/email/rules").json()["rules"][0]["version"]
    yield client, store, bot, state, version, path, rule["rule_id"]
    store.close()


def post(setup, **extra):
    client, _, _, _, version, _, rule_id = setup
    return client.post(f"/api/automations/email/{rule_id}/events",
                       json={"version": version, "message_id": "mail123", **extra})


def test_disabled_and_wrong_credential_fail_closed(setup, monkeypatch):
    client = setup[0]
    assert client.get("/api/automations/email/rules", headers={"Authorization": "Bearer bad"}).status_code == 401
    monkeypatch.delenv("KYREX_AUTOMATION_ENABLED")
    assert client.get("/api/automations/email/rules").status_code == 404
    assert post(setup).status_code == 404


def test_corrupt_persisted_rules_fail_closed(setup):
    db = automation_rules._connect()
    db.execute("UPDATE email_automation_rules SET sender=?", ("bad OR from:anyone",))
    db.commit()
    db.close()
    assert setup[0].get("/api/automations/email/rules").status_code == 503


@pytest.mark.parametrize("change", [{"status": "paused"}, {"owner": "mallory"}])
def test_unavailable_or_foreign_bot_never_routes_elsewhere(setup, change):
    setup[2].update(change)
    assert post(setup).status_code == 409
    assert setup[1].get("missing") is None
    assert setup[0].get(f"/api/automations/email/{setup[6]}/candidates",
                        params={"version": setup[4]}).status_code == 409


def test_deleted_or_rebound_conversation_rejected(setup):
    setup[5].write_text(json.dumps({"conversation_id": CID, "bot_id": "other"}))
    assert post(setup).status_code == 409
    setup[5].unlink()
    assert post(setup).status_code == 409


def test_gmail_scope_required(setup):
    setup[3]["connected"] = False
    assert post(setup).status_code == 409


@pytest.mark.parametrize("sender", ["School <attacker@example.com>",
                                     "school@example.com, attacker@example.com", ""])
def test_actual_header_must_match_sender(setup, sender):
    setup[3]["sender"] = sender
    assert post(setup).status_code == 409


def test_client_cannot_choose_owner_task_destination_or_approval(setup):
    for field in ("owner", "bot_id", "conversation_id", "task_text", "approved"):
        assert post(setup, **{field: "injected"}).status_code == 422
    assert post(setup, message_id="mail123; delete").status_code == 422
    assert post(setup, version="0" * 64).status_code == 409


def test_candidates_are_bounded_sender_search_with_no_email_content(setup):
    response = setup[0].get(f"/api/automations/email/{setup[6]}/candidates",
                            params={"version": setup[4], "page_token": "opaque"})
    assert response.json() == {"message_ids": ["mail123"], "next_page_token": "next"}
    assert setup[3]["calls"] == [{"query": "in:inbox from:school@example.com newer_than:7d",
                                  "max_results": 50, "page_token": "opaque"}]


def test_retries_queue_once_and_result_waits_in_existing_bot_chat(setup):
    response = post(setup)
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    again = post(setup).json()
    assert again == {"accepted": True, "duplicate": True, "task_id": task_id}
    task = setup[1].get(task_id)
    assert task["chat_id"] == "alice"
    assert task["bot_id"] == "barrett"
    assert task["conversation_id"] == CID
    assert task["task_text"] == "gmail: read id mail123"
    assert task["executor_prefix"] == "gmail"
    assert task["repo_url"] is None
    # The gateway has not written any chat message or required a browser SSE.
    assert json.loads(setup[5].read_text())["messages"] == []
    setup[1].claim_next("worker")
    setup[1].complete(task_id, {"status": "ok", "final_response": "New school email: Field trip reminder."})
    recovered = api.chat_service.get_conversation("alice", CID)
    assert any("Field trip reminder" in m["content"] for m in recovered["messages"])
    assert api.chat_service.get_conversation("alice", CID)["messages"] == recovered["messages"]



def test_chat_settings_lists_existing_email_bot_chat_and_service_state(setup):
    response = setup[0].get("/api/automations/email/managed")
    assert response.status_code == 200
    data = response.json()
    assert data["service_ready"] is True
    assert data["rules"][0]["bot_name"] == "Email Bot"
    assert data["rules"][0]["conversation_title"] == "Barrett"
    assert data["rules"][0]["sender"] == "school@example.com"


def test_settings_can_create_pause_resume_and_remove_rule(setup):
    client = setup[0]
    created = client.post("/api/automations/email/managed", json={
        "sender": "store+123@shopifyemail.com", "bot_id": "barrett",
        "conversation_id": CID,
    })
    assert created.status_code == 200, created.text
    rule_id = created.json()["rule_id"]
    rules = client.get("/api/automations/email/rules").json()["rules"]
    assert {row["id"] for row in rules} == {setup[6], rule_id}
    assert client.patch(f"/api/automations/email/managed/{rule_id}",
                        json={"enabled": False}).status_code == 200
    assert {row["id"] for row in client.get("/api/automations/email/rules").json()["rules"]} == {setup[6]}
    assert client.patch(f"/api/automations/email/managed/{rule_id}",
                        json={"enabled": True}).status_code == 200
    assert client.delete(f"/api/automations/email/managed/{rule_id}").status_code == 200
    assert len(client.get("/api/automations/email/rules").json()["rules"]) == 1


def test_settings_reject_foreign_user_invalid_sender_or_wrong_chat(setup, monkeypatch):
    client = setup[0]
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(require_user=lambda request: "bob"))
    response = client.post("/api/automations/email/managed", json={
        "sender": "sender@example.com", "bot_id": "barrett", "conversation_id": CID,
    })
    assert response.status_code == 403
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(require_user=lambda request: "alice"))
    bad_sender = client.post("/api/automations/email/managed", json={
        "sender": "sender@example.com OR from:anyone", "bot_id": "barrett", "conversation_id": CID,
    })
    assert bad_sender.status_code == 422
    bad_chat = client.post("/api/automations/email/managed", json={
        "sender": "sender@example.com", "bot_id": "barrett", "conversation_id": "d" * 32,
    })
    assert bad_chat.status_code == 409
