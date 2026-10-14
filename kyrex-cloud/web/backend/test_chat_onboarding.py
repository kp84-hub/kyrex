import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import onboarding_api as api
import chat_onboarding as setup
import provider_profiles
import bot_provider
import bots
import serve


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'test-only-setup-encryption-secret')
    monkeypatch.setenv('KYREX_API_KEY', 'test-only-host-provider-key')
    monkeypatch.setenv('KYREX_MODEL', 'host-model')
    monkeypatch.setattr(bots, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(bots, 'BOTS_FILE', str(tmp_path / 'bots.json'))
    def owner(request):
        if not request.headers.get('x-owner'): raise HTTPException(401)
        return request.headers['x-owner']
    monkeypatch.setattr(api.chat_api, '_require_user', owner)
    app = FastAPI(); app.include_router(api.router)
    return TestClient(app)


def post(client, action, data=None, owner='alice'):
    return client.post('/api/chat/onboarding/' + action, headers={'x-owner': owner}, json=data or {})


def configure(client, provider='opencode'):
    response = post(client, 'provider', {'provider': provider, 'api_key': 'test-only-user-provider-key-1234'})
    assert response.status_code == 200, response.text
    return response.json()


class Response:
    def __init__(self, value, code=200):
        self.raw = json.dumps(value).encode(); self.status_code = code; self.closed = False
    def iter_content(self, size): return iter([self.raw])
    def close(self): self.closed = True


@pytest.fixture
def transport(monkeypatch):
    calls = []; replies = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        value = {'data': [{'id': 'custom/model:free'}, {'id': 'mimo-v2.6-flash'}]} if method == 'GET' else (
            {'object': 'response', 'status': 'completed'} if url.endswith('/responses') else
            {'content': [{'type': 'text', 'text': 'OK'}]} if url.endswith('/messages') else
            {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]})
        response = Response(value); replies.append(response); return response
    monkeypatch.setattr(setup.requests, 'request', request)
    return calls, replies


def test_first_run_needs_owner_credentials_without_automatic_model_calls(client, monkeypatch):
    monkeypatch.setattr(setup.requests, 'request', lambda *a, **k: pytest.fail('Status/save must not call an LLM'))
    value = client.get('/api/chat/onboarding', headers={'x-owner': 'alice'}).json()
    assert value['required'] and value['step'] == 'provider' and value['profiles'] == []
    assert post(client, 'complete').status_code == 400
    state = configure(client)
    assert state['step'] == 'model' and not state['verified']
    assert 'test-only-user-provider-key-1234' not in json.dumps(state)
    profile = provider_profiles.get_profile('alice', state['profile_id'])
    assert profile['api_key'] == 'test-only-user-provider-key-1234' and profile['models'] == []
    assert b'test-only-user-provider-key-1234' not in provider_profiles._path('alice').read_bytes()
    assert client.get('/api/chat/onboarding', headers={'x-owner': 'bob'}).json()['step'] == 'provider'
    assert not bots.load_bots()


@pytest.mark.parametrize('provider,model,suffix', [('opencode', 'gpt-6-luna', '/responses'),
    ('opencode', 'mimo-v2.6-flash', '/chat/completions'),
    ('opencode', 'claude-haiku-5-5', '/messages'), ('opencode', 'qwen3.8-max', '/messages'), ('openrouter', 'custom/model:free', '/chat/completions'),
    ('openai', 'gpt-test', '/chat/completions'), ('anthropic', 'claude-test', '/v1/messages')])
def test_test_uses_owner_profile_and_runtime_protocol(client, transport, provider, model, suffix):
    state = configure(client, provider)
    assert 'custom/model:free' in client.get('/api/chat/onboarding/models', headers={'x-owner': 'alice'}).json()['models']
    tested = post(client, 'test', {'model': model})
    assert tested.status_code == 200, tested.text
    assert tested.json()['verified'] and tested.json()['step'] == 'finish'
    calls, replies = transport; method, url, kwargs = calls[-1]
    assert method == 'POST' and url == ('https://opencode.ai/inference/go/v1' if provider == 'opencode' else setup.PROVIDERS[provider]['base_url']) + suffix
    assert kwargs['json']['model'] == model and kwargs['allow_redirects'] is False
    assert 'tools' not in kwargs['json'] and 'host-model' not in json.dumps(kwargs)
    assert 'test-only-host-provider-key' not in json.dumps(kwargs)
    if provider == 'opencode': assert kwargs['headers']['x-opencode-session'].endswith(state['profile_id'])
    assert all(response.closed for response in replies)
    assert not bots.load_bots()


def test_completion_is_idempotent_owned_and_seeds_generic_welcome(client, transport):
    configure(client, 'openrouter')
    assert post(client, 'test', {'model': 'custom/model:free'}).status_code == 200
    completed = post(client, 'complete'); assert completed.status_code == 200, completed.text
    state = completed.json(); assert not state['required']
    bot = bots.get_bot(state['bot_id'])
    assert bot['owner'] == 'alice' and bot['model'] == 'custom/model:free'
    assert bot['policy'] == serve.coordinator_preset_policy() and bot['status'] == 'running'
    assert bot_provider.resolve_bot_provider('alice', bot)['model'] == 'custom/model:free'
    assert 'test-only-user-provider-key-1234' not in json.dumps(bot)
    conv = setup.chat_service.get_conversation('alice', state['conversation_id'])
    assert conv['bot_id'] == bot['id'] and conv['messages'][0]['content'] == setup.WELCOME
    assert 'Keith' not in bot['system_prompt'] and 'Harper' not in conv['messages'][0]['content']
    assert setup.chat_service.get_conversation('bob', state['conversation_id']) is None
    count = len(transport[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: setup.complete('alice'), range(2)))
    assert {r['conversation_id'] for r in results} == {state['conversation_id']}
    assert len(bots.load_bots()) == 1 and len(transport[0]) == count


def test_gemini_connection_probe_uses_native_endpoint_without_chat_history(client, monkeypatch):
    state = configure(client)
    calls = []
    response = Response({'candidates': [{'finishReason': 'STOP',
        'content': {'parts': [{'text': 'OK'}]}}]})
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs)); return response
    monkeypatch.setattr(setup.requests, 'request', request)
    value = post(client, 'test', {'model': 'gemini-4-argon-contributor'})
    assert value.status_code == 200 and value.json()['verified']
    assert len(calls) == 1 and response.closed
    method, url, kwargs = calls[0]
    assert method == 'POST'
    assert url == 'https://opencode.ai/inference/go/v1/models/gemini-4-argon-contributor:generateContent'
    assert kwargs['json'] == {'contents': [{'role': 'user', 'parts': [{
        'text': 'Reply briefly with OK to test this coding assistant connection.'}]}]}
    assert kwargs['headers']['Authorization'] == 'Bearer test-only-user-provider-key-1234'
    assert kwargs['headers']['x-opencode-session'].endswith(state['profile_id'])
    assert kwargs['allow_redirects'] is False and not bots.load_bots()


@pytest.mark.parametrize('value', [
    {'candidates': [{'finishReason': 'MAX_TOKENS', 'content': {'parts': [{'text': 'partial'}]}}]},
    {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'thought': True, 'text': 'thinking'}]}}]},
    {'promptFeedback': {'blockReason': 'SAFETY'}},
    {'choices': [{'message': {'content': 'wrong protocol'}}]},
    {'candidates': [{'finishReason': 'STOP', 'content': 'malformed'}]},
    {'candidates': [{'finishReason': 'STOP', 'content': {'parts': {'text': 'malformed'}}}]},
])
def test_gemini_probe_requires_complete_visible_native_reply(client, monkeypatch, value):
    configure(client)
    response = Response(value)
    monkeypatch.setattr(setup.requests, 'request', lambda *a, **k: response)
    assert post(client, 'test', {'model': 'gemini-4-argon-contributor'}).status_code == 400
    assert response.closed and not bots.load_bots()
    assert not client.get('/api/chat/onboarding', headers={'x-owner': 'alice'}).json()['verified']


def test_expiry_and_rotated_credentials_require_another_test(client, transport):
    state = configure(client)
    assert post(client, 'test', {'model': 'mimo-v2.6-flash'}).status_code == 200
    profile = provider_profiles.get_profile('alice', state['profile_id'])
    provider_profiles.save_profile('alice', {**profile, 'api_key': 'test-only-rotated-provider-key'}, allow_empty_models=True)
    assert post(client, 'complete').status_code == 400
    assert post(client, 'test', {'model': 'mimo-v2.6-flash'}).status_code == 200
    with setup.database() as db:
        state = setup.read(db, 'alice'); state['verified_at'] = 0; setup.write(db, 'alice', state)
    assert post(client, 'complete').status_code == 400 and not bots.load_bots()


def test_foreign_profiles_and_origins_cannot_change_setup(client, transport):
    state = configure(client)
    assert post(client, 'provider', {'profile_id': state['profile_id']}, owner='bob').status_code == 400
    assert client.post('/api/chat/onboarding/test', json={'model': 'm1'}).status_code == 401
    assert client.post('/api/chat/onboarding/provider', headers={'x-owner': 'alice', 'Origin': 'https://foreign.example'},
        json={'provider': 'openai', 'api_key': 'test-only-injected-key'}).status_code == 403
    assert not transport[0]
    spoofed = post(client, 'provider', {'provider': 'openai', 'api_key': 'test-only-user-provider-key', 'owner': 'bob', 'base_url': 'http://169.254.169.254'})
    profile = provider_profiles.get_profile('alice', spoofed.json()['profile_id'])
    assert profile['base_url'] == 'https://api.openai.com/v1' and provider_profiles.list_profiles('bob') == []


def test_failed_probe_is_redacted_without_retry_or_bot_creation(client, monkeypatch):
    configure(client); calls = []
    response = Response({'error': {'message': 'test-only-user-provider-key-1234'}}, 401)
    def request(*a, **k): calls.append(a); return response
    monkeypatch.setattr(setup.requests, 'request', request)
    value = post(client, 'test', {'model': 'mimo-v2.6-flash'})
    assert value.status_code == 400 and 'test-only-user-provider-key-1234' not in value.text
    assert len(calls) == 1 and response.closed
    assert post(client, 'complete').status_code == 400


def test_profile_change_during_probe_invalidates_result(client, monkeypatch):
    configure(client)
    def probe(profile, model):
        provider_profiles.save_profile('alice', {**profile, 'api_key': 'test-only-changed-key-during-probe'}, allow_empty_models=True)
    monkeypatch.setattr(setup, 'probe', probe)
    assert post(client, 'test', {'model': 'mimo-v2.6-flash'}).status_code == 400
    assert not client.get('/api/chat/onboarding', headers={'x-owner': 'alice'}).json()['verified']


def test_existing_assistant_is_not_reconfigured_or_restarted(client, monkeypatch, tmp_path):
    state = configure(client)
    bot = bots.add_bot('existing', 'My assistant', 'm1', str(tmp_path), owner='alice',
        policy=serve.coordinator_preset_policy(), status='paused', provider_profile_id=state['profile_id'], system_prompt='Keep my prompt')
    monkeypatch.setattr(setup.requests, 'request', lambda *a, **k: pytest.fail('Existing setup needs no provider call'))
    assert not client.get('/api/chat/onboarding', headers={'x-owner': 'alice'}).json()['required']
    assert post(client, 'complete').status_code == 200 and bots.get_bot('existing') == bot


def test_manual_model_fallback_and_bounded_tests(client, monkeypatch):
    configure(client)
    monkeypatch.setattr(setup, 'request_profile', lambda *a, **k: (_ for _ in ()).throw(setup.SetupError('catalog unavailable')))
    value = client.get('/api/chat/onboarding/models', headers={'x-owner': 'alice'}).json()
    assert 'gpt-6-luna' in value['models'] and 'exact model ID' in value['notice']
    monkeypatch.setattr(setup, 'probe', lambda *a: None)
    for _ in range(5): assert post(client, 'test', {'model': 'manual-model'}).status_code == 200
    assert post(client, 'test', {'model': 'manual-model'}).status_code == 400


@pytest.mark.parametrize('data', [{'provider': []}, {'provider': 'unknown'}, {'provider': 'openai', 'api_key': []}, {'profile_id': []}])
def test_invalid_setup_creates_no_profile(client, data):
    assert post(client, 'provider', data).status_code == 400
    assert provider_profiles.list_profiles('alice') == []
