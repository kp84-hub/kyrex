"""Real Chat SSE and durable rules for notification setup and follow-ups."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import automation_rules
import bots
import chat_api
import chat_service
import connectors
import provider_profiles
import serve
from task_store import CloudTaskStore


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'test-only-email-notify-encryption')
    monkeypatch.setenv('KYREX_AUTOMATION_ENABLED', '1')
    monkeypatch.setenv('KYREX_AUTOMATION_OWNER', 'alice')
    monkeypatch.setenv('KYREX_AUTOMATION_TOKEN', 'test-only-email-watcher-' + 'x' * 40)
    monkeypatch.setattr(bots, 'BOTS_FILE', str(tmp_path / 'bots.json'))
    store = CloudTaskStore(db_path=tmp_path / 'tasks.sqlite3')
    monkeypatch.setattr(chat_service, '_task_store', lambda: store)
    monkeypatch.setattr(chat_service.chat_privacy, 'settings', lambda user: {'share_saved_memory': False})
    def forbidden(*a, **k): pytest.fail('Notification setup must not invoke an LLM or task executor')
    monkeypatch.setattr(chat_service, '_get_engine_session', forbidden)
    monkeypatch.setattr(chat_service, 'get_provider', forbidden)
    monkeypatch.setattr(connectors, 'default_store', lambda: SimpleNamespace(gmail_read_available=lambda owner: owner == 'alice'))
    provider_profiles.save_profile('alice', {'id': 'p', 'name': 'Test', 'provider': 'openai',
        'api_key': 'test-only-provider-credential', 'base_url': 'https://api.openai.com/v1', 'models': ['m']})
    for bid, name, policy in [('chief', 'The Overwatcher', serve.coordinator_preset_policy()),
                              ('email', 'Email Bot', serve.gmail_reader_preset_policy())]:
        rift = tmp_path / bid; rift.mkdir()
        bots.add_bot(bid, name, 'm', str(rift), owner='alice', policy=policy,
                     status='running', provider_profile_id='p')
    conv = chat_service.create_conversation('alice', bot_id='chief')
    def owner(request):
        if request.headers.get('x-owner') != 'alice': raise HTTPException(401)
        return 'alice'
    monkeypatch.setattr(chat_api, '_require_user', owner)
    app = FastAPI(); app.include_router(chat_api.router)
    with TestClient(app, headers={'x-owner': 'alice'}) as client:
        yield client, store, conv
    store.close()


def send(setup, text, cid=None, request_id='notify-turn'):
    client, _, conv = setup
    response = client.post('/api/chat', json={'conversation_id': cid or conv['conversation_id'],
        'message': text, 'request_id': request_id})
    assert response.status_code == 200
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    terminal = [f for f in frames if f['type'] in ('done', 'error', 'cancelled')]
    assert len(terminal) == 1 and terminal[0]['type'] == 'done', frames
    return terminal[0]['content']


def test_screenshot_followup_creates_one_rule_from_owner_context(setup):
    _, store, conv = setup
    chat_service._append_message('alice', conv, 'user', 'Can I get an email alert from school@example.com?')
    chat_service._append_message('alice', conv, 'assistant', 'Notify only or calendar too? Use invented@example.com.')
    chat_service._write('alice', conv)
    text = 'Can you delegate to the email bot just notify me'
    content = send(setup, text)
    assert 'school@example.com' in content and 'Notification only' in content
    assert 'invented@example.com' not in content
    rule = automation_rules.list_rules('alice')[0]
    destination = chat_service.get_conversation('alice', rule['conversation_id'])
    assert destination['bot_id'] == 'email' and destination['title'] == 'Email alerts'
    assert not store.tasks_for_conversation(conv['conversation_id'], 'alice')
    assert not store.tasks_for_conversation(destination['conversation_id'], 'alice')
    assert not automation_rules.list_rules('bob')
    assert send(setup, text) == content
    assert len(automation_rules.list_rules('alice')) == 1
    assert len([c for c in chat_service.list_conversations('alice') if c.get('bot_id') == 'email']) == 1


def test_explicit_natural_request_and_existing_destination(setup):
    existing = chat_service.create_conversation('alice', title='School mail', bot_id='email')
    content = send(setup, 'Notify me whenever I get an email from school@example.com')
    assert 'School mail' in content
    assert automation_rules.list_rules('alice')[0]['conversation_id'] == existing['conversation_id']


def test_email_bot_can_set_up_notification_directly(setup):
    existing = chat_service.create_conversation('alice', bot_id='email')
    send(setup, 'Notify me whenever I get an email from school@example.com', cid=existing['conversation_id'])
    assert automation_rules.list_rules('alice')[0]['bot_id'] == 'email'


@pytest.mark.parametrize('prior', ['', 'What is my calendar?', 'Email from one@example.com and two@example.com'])
def test_missing_stale_or_ambiguous_sender_asks_without_changes(setup, prior):
    conv = setup[2]
    if prior: chat_service._append_message('alice', conv, 'user', prior)
    chat_service._append_message('alice', conv, 'assistant', 'Alert from invented@example.com')
    chat_service._write('alice', conv)
    assert 'exact sender' in send(setup, 'Can you delegate to the Email Bot just notify me')
    assert not automation_rules.list_rules('alice')
    assert not [c for c in chat_service.list_conversations('alice') if c.get('bot_id') == 'email']


def test_missing_watcher_reports_real_blocker_without_creating_chat(setup, monkeypatch):
    monkeypatch.delenv('KYREX_AUTOMATION_ENABLED')
    content = send(setup, 'Notify me whenever I get an email from school@example.com')
    assert "isn't configured for your account" in content
    assert not automation_rules.list_rules('alice')
    assert not [c for c in chat_service.list_conversations('alice') if c.get('bot_id') == 'email']


def test_concurrent_setup_reuses_new_destination(setup):
    import email_automation_chat
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: email_automation_chat.add_rule('alice', 'school@example.com'), range(2)))
    assert results[0][0]['rule_id'] == results[1][0]['rule_id']
    assert len([c for c in chat_service.list_conversations('alice') if c.get('bot_id') == 'email']) == 1


@pytest.mark.parametrize('change', [{'status': 'paused'}, {'owner': 'bob'}])
def test_unavailable_or_foreign_email_bot_is_not_substituted(setup, monkeypatch, change):
    registry = bots.load_bots(); registry['email'].update(change)
    monkeypatch.setattr(bots, 'load_bots', lambda: registry)
    content = send(setup, 'Notify me whenever I get an email from school@example.com')
    assert 'exactly one running Email Bot owned by you' in content
    assert not automation_rules.list_rules('alice')
    assert not [c for c in chat_service.list_conversations('alice') if c.get('bot_id') == 'email']
