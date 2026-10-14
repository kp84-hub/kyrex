import copy
import json
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import bot_model_check as checks
import chat_api as api
import provider_profiles

MODEL = 'gemini-4-argon-contributor'
KEY = 'opaque-test-only-provider-credential'


@pytest.fixture
def wire(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'test-only-model-check-encryption-secret')
    provider_profiles.save_profile('alice', {'id': 'go', 'name': 'Go', 'provider': 'opencode',
        'base_url': 'https://opencode.ai/zen/go/v1', 'api_key': KEY, 'models': ['deepseek-v4.1-flash'],
        'headers': {'X-Tenant': 'opaque-private-tenant-value'}})
    bot = {'id': 'chief', 'owner': 'alice', 'provider_profile_id': 'go', 'model': 'deepseek-v4.1-flash'}
    def owner(request):
        user = request.headers.get('x-owner')
        if not user: raise HTTPException(401)
        return user
    def owned(user, bot_id):
        if user != 'alice' or bot_id != 'chief': raise HTTPException(404)
        return bot
    monkeypatch.setattr(api, '_require_user', owner)
    monkeypatch.setattr(api, '_owned_bot', owned)
    monkeypatch.setattr(api.chat_service, '_get_engine_session', lambda *a, **k: pytest.fail('A connection check cannot start an engine'))
    monkeypatch.setattr(api.chat_service, 'create_conversation', lambda *a, **k: pytest.fail('A connection check cannot create a conversation'))
    # Use the real OpenCode adapter and HTTP payload. Its unused SDK client
    # needs no proxy transport; the native transport is intercepted below.
    import kyrex.providers.openai_ as openai_module
    async def close(): pass
    monkeypatch.setattr(openai_module, 'AsyncOpenAI', lambda **kwargs: NS(close=close))
    calls, replies = [], []
    def handle(request):
        calls.append(request)
        return replies.pop(0)
    original = httpx.AsyncClient
    monkeypatch.setattr(checks.httpx, 'AsyncClient',
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handle)))
    app = FastAPI(); app.include_router(api.router)
    return TestClient(app), bot, calls, replies


def post(client, *, owner='alice', data=None, **kwargs):
    return client.post('/api/bots/chief/llm/check', headers={'x-owner': owner, **kwargs},
                       json=data if data is not None else {'provider_profile_id': 'go', 'model': MODEL})


def good(replies, listed=True):
    replies.extend([httpx.Response(200, json={'providers': {'opencode-go': {
        'settings': {'apiKey': KEY}, 'models': {MODEL: {}} if listed else {}}}}),
        httpx.Response(200, content='data: ' + json.dumps({'candidates': [{
            'content': {'parts': [{'text': 'OK'}]}, 'finishReason': 'STOP'}]}) + '\n\n')])


def test_saved_draft_selection_checks_config_and_native_model_without_changes(wire):
    client, bot, calls, replies = wire
    before = copy.deepcopy(bot)
    good(replies)
    response = post(client)
    assert response.status_code == 200, response.text
    assert response.json()['ok'] and response.json()['workspace'] == {'available': True, 'model_listed': True}
    assert bot == before and provider_profiles.list_profiles('alice')[0]['models'] == ['deepseek-v4.1-flash']
    assert len(calls) == 2
    assert str(calls[0].url) == 'https://opencode.ai/console/api/v2/config'
    assert str(calls[1].url) == f'https://opencode.ai/inference/go/v1/models/{MODEL}:streamGenerateContent?alt=sse'
    body = json.loads(calls[1].content)
    assert body['contents'] == [{'role': 'user', 'parts': [{'text': 'Reply briefly with OK.'}]}]
    assert 'tools' not in body and 'systemInstruction' not in body
    assert 'x-tenant' not in calls[0].headers and calls[1].headers['x-tenant'] == 'opaque-private-tenant-value'
    for request in calls:
        assert request.headers['authorization'] == 'Bearer ' + KEY
        assert request.headers['x-opencode-session'].startswith('kyrex-model-check-')
        assert request.headers['user-agent'] == 'kyrex/0.1'
    assert calls[0].headers['x-opencode-session'] == calls[1].headers['x-opencode-session']
    assert KEY not in response.text and KEY not in calls[1].content.decode()
    assert 'opaque-private-tenant' not in response.text


def test_failed_minimal_inference_keeps_error_code_and_never_echoes_config_or_error(wire):
    client, bot, calls, replies = wire
    good(replies)
    replies[-1] = httpx.Response(400, json={'error': {'type': 'inference_failed', 'message': KEY}})
    response = post(client)
    assert response.status_code == 200 and not response.json()['ok']
    assert 'HTTP 400' in response.json()['message'] and 'inference_failed' in response.json()['message']
    assert KEY not in response.text and len(calls) == 2


@pytest.mark.parametrize('config', [httpx.Response(403, content=KEY),
    httpx.Response(200, json={'providers': []}), httpx.Response(200, content=KEY * 50000)])
def test_unavailable_workspace_config_does_not_prevent_minimal_model_test(wire, config):
    client, bot, calls, replies = wire
    good(replies); replies[0] = config
    response = post(client)
    assert response.json()['ok'] and not response.json()['workspace']['available']
    assert KEY not in response.text and len(calls) == 2


def test_missing_model_listing_is_reported_separately_from_actual_request(wire):
    client, bot, calls, replies = wire
    good(replies, listed=False)
    response = post(client)
    assert response.json()['ok'] and response.json()['workspace']['model_listed'] is False


def test_unexpected_tool_call_is_never_executed_or_reported_as_success(wire):
    client, bot, calls, replies = wire
    good(replies)
    replies[-1] = httpx.Response(200, content='data: ' + json.dumps({'candidates': [{
        'content': {'parts': [{'functionCall': {'name': 'run_command', 'args': {'command': 'anything'}},
                              'thoughtSignature': 'opaque-signature'}]}, 'finishReason': 'STOP'}]}) + '\n\n')
    response = post(client)
    assert response.status_code == 200 and not response.json()['ok']
    assert 'usable test reply' in response.json()['message']
    assert 'anything' not in response.text


@pytest.mark.parametrize('data', [
    {'provider_profile_id': 'go', 'model': MODEL, 'api_key': KEY},
    {'provider_profile_id': 'other-user-profile', 'model': MODEL},
    {'provider_profile_id': 'go', 'model': []},
    {'provider_profile_id': 'go', 'model': 'bad\nmodel'},
    {'provider_profile_id': '', 'model': MODEL},
])
def test_invalid_or_foreign_selection_sends_nothing(wire, data):
    client, bot, calls, replies = wire
    response = post(client, data=data)
    assert response.status_code == 400 and not calls and KEY not in response.text


def test_auth_bot_ownership_and_origin_are_checked_before_provider_calls(wire):
    client, bot, calls, replies = wire
    assert client.post('/api/bots/chief/llm/check', json={}).status_code == 401
    assert post(client, owner='bob').status_code == 404
    assert post(client, origin='https://attacker.test').status_code == 403
    assert not calls


def test_owner_model_check_rate_limit_is_shared_across_bots_and_tests(wire):
    client, bot, calls, replies = wire
    for _ in range(5):
        good(replies); assert post(client).json()['ok']
    assert post(client).status_code == 400 and len(calls) == 10


def test_non_opencode_destinations_receive_no_credentials_or_probe(wire):
    client, bot, calls, replies = wire
    profile = provider_profiles.get_profile('alice', 'go')
    provider_profiles.save_profile('alice', {**profile, 'base_url': 'https://opencode.ai.attacker.test/v1'})
    assert post(client).status_code == 400 and not calls


def test_rotated_profile_cannot_receive_a_success_receipt_for_older_credentials(wire, monkeypatch):
    client, bot, calls, replies = wire
    good(replies)
    original = httpx.MockTransport.handle_async_request
    async def rotate(transport, request):
        response = await original(transport, request)
        if request.method == 'POST':
            profile = provider_profiles.get_profile('alice', 'go')
            provider_profiles.save_profile('alice', {**profile, 'api_key': KEY + '-rotated'})
        return response
    monkeypatch.setattr(httpx.MockTransport, 'handle_async_request', rotate)
    response = post(client)
    assert not response.json()['ok'] and 'changed during' in response.json()['message']
    assert KEY not in response.text
