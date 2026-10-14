import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import chat_api
import provider_profiles

KEY = 'test-only-saved-opencode-provider-key'
SECRET_HEADER = 'test-only-private-header-value'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'test-only-profile-edit-encryption-secret')
    profile = {'id': 'opencode', 'name': 'OpenCode', 'provider': 'opencode',
        'base_url': 'https://opencode.ai/zen/go/v1', 'api_key': KEY,
        'models': ['deepseek-v4.1-flash'], 'headers': {'Key': SECRET_HEADER, 'Value': 'another-private-header'}}
    provider_profiles.save_profile('alice', profile)
    def owner(request):
        user = request.headers.get('x-owner')
        if not user: raise HTTPException(401)
        return user
    monkeypatch.setattr(chat_api, '_require_user', owner)
    app = FastAPI(); app.include_router(chat_api.router)
    return TestClient(app), profile


def save(client, profile, owner='alice', **changes):
    data = {key: value for key, value in profile.items() if key not in {'api_key', 'headers'}}
    data.update(api_key='', **changes)
    return client.post('/api/chat/provider-profiles', headers={'x-owner': owner}, json=data)


def test_edit_preserves_saved_key_and_omitted_header_values(client):
    http, profile = client
    result = save(http, profile, name='Renamed OpenCode', base_url='https://opencode.ai/inference/go/v1')
    assert result.status_code == 200, result.text
    saved = provider_profiles.get_profile('alice', 'opencode')
    assert saved['api_key'] == KEY and saved['headers'] == profile['headers']
    assert saved['name'] == 'Renamed OpenCode' and saved['base_url'].endswith('/inference/go/v1')
    assert result.json()['id'] == 'opencode'
    assert KEY not in result.text and SECRET_HEADER not in result.text


def test_explicit_empty_headers_clear_bad_values_without_changing_key_or_identity(client):
    http, profile = client
    result = save(http, profile, headers={})
    assert result.status_code == 200
    saved = provider_profiles.get_profile('alice', 'opencode')
    assert saved == {**profile, 'headers': {}}
    assert result.json()['header_names'] == [] and result.json()['has_api_key']
    assert KEY not in result.text and SECRET_HEADER not in result.text
    assert SECRET_HEADER not in json.dumps(saved)


def test_nonempty_headers_replace_all_saved_headers(client):
    http, profile = client
    result = save(http, profile, headers={'X-Label': 'test-only-new-value'})
    assert result.status_code == 200
    assert provider_profiles.get_profile('alice', 'opencode')['headers'] == {'X-Label': 'test-only-new-value'}
    assert result.json()['header_names'] == ['X-Label'] and 'test-only-new-value' not in result.text


def test_null_headers_preserve_legacy_update_behavior(client):
    http, profile = client
    assert save(http, profile, headers=None).status_code == 200
    assert provider_profiles.get_profile('alice', 'opencode')['headers'] == profile['headers']


def test_bad_header_update_does_not_overwrite_stored_profile(client):
    http, profile = client
    assert save(http, profile, headers={'Bad Header': 'test-only-invalid-value'}).status_code == 400
    assert provider_profiles.get_profile('alice', 'opencode') == profile


def test_foreign_owner_cannot_reuse_or_edit_another_owners_saved_key(client):
    http, profile = client
    result = save(http, profile, owner='bob', headers={})
    assert result.status_code == 400 and KEY not in result.text
    assert provider_profiles.get_profile('alice', 'opencode') == profile
    assert provider_profiles.get_profile('bob', 'opencode') is None


def test_consolidate_moves_owner_bots_keeps_chat_alias_and_saved_key(client, monkeypatch):
    import bots
    import bot_provider
    import chat_service
    http, profile = client
    source = {**profile, 'id': 'opencode_gpt', 'name': 'OpenCode GPT',
              'base_url': 'https://opencode.ai/zen/go/v1/responses', 'models': ['gpt-6-luna'], 'headers': {}}
    provider_profiles.save_profile('alice', source)
    registry = {'a': {'id': 'a', 'owner': 'alice', 'provider_profile_id': source['id'], 'model': 'gpt-6-luna'},
                'b': {'id': 'b', 'owner': 'bob', 'provider_profile_id': source['id'], 'model': 'gpt-6-luna'}}
    monkeypatch.setattr(bots, 'load_bots', lambda: registry)
    monkeypatch.setattr(bots, 'save_bots', lambda values: None)
    endpoint = '/api/chat/provider-profiles/opencode_gpt/consolidate'
    result = http.post(endpoint, headers={'x-owner': 'alice'}, json={'target_id': 'opencode'})
    assert result.status_code == 200, result.text
    assert KEY not in result.text and SECRET_HEADER not in result.text
    assert [p['id'] for p in provider_profiles.list_profiles('alice')] == ['opencode']
    saved = provider_profiles.get_profile('alice', 'opencode')
    assert saved['api_key'] == KEY and saved['headers'] == profile['headers']
    assert saved['aliases'] == ['opencode_gpt'] and saved['models'] == ['deepseek-v4.1-flash', 'gpt-6-luna']
    assert saved['base_url'] == 'https://opencode.ai/inference/go/v1'
    assert registry['a']['provider_profile_id'] == 'opencode' and registry['a']['model'] == 'gpt-6-luna'
    assert registry['b']['provider_profile_id'] == 'opencode_gpt'
    cfg = bot_provider.resolve_bot_provider('alice', registry['a'])
    assert cfg['api_key'] == KEY and cfg['profile_id'] == 'opencode'
    # Saved non-Bot conversations keep working without rewriting message data.
    legacy = chat_service._resolve_provider('opencode_gpt', 'gpt-6-luna', user='alice')
    assert legacy['profile'] == 'opencode' and legacy['api_key'] == KEY
    assert provider_profiles.get_profile('alice', 'opencode_gpt') == saved
    assert provider_profiles.get_profile('bob', 'opencode_gpt') is None
    assert http.post(endpoint, headers={'x-owner': 'alice'}, json={'target_id': 'opencode'}).status_code == 200
    assert save(http, profile, headers={}).status_code == 200
    assert provider_profiles.get_profile('alice', 'opencode')['aliases'] == ['opencode_gpt']


@pytest.mark.parametrize('change', [{'api_key': 'different-owner-key'}, {'base_url': 'https://openrouter.ai/api/v1'}])
def test_consolidation_rejects_different_keys_or_gateways_without_changes(client, change):
    http, profile = client
    provider_profiles.save_profile('alice', {**profile, 'id': 'opencode_gpt', **change})
    before = provider_profiles._read('alice')
    result = http.post('/api/chat/provider-profiles/opencode_gpt/consolidate', headers={'x-owner': 'alice'}, json={'target_id': 'opencode'})
    assert result.status_code == 400
    assert provider_profiles._read('alice') == before
    assert KEY not in result.text


def test_consolidation_cannot_read_or_change_another_owner(client):
    http, profile = client
    provider_profiles.save_profile('alice', {**profile, 'id': 'opencode_gpt'})
    before = provider_profiles._read('alice')
    result = http.post('/api/chat/provider-profiles/opencode_gpt/consolidate', headers={'x-owner': 'bob'}, json={'target_id': 'opencode'})
    assert result.status_code == 400 and KEY not in result.text
    assert provider_profiles._read('alice') == before


def test_one_go_profile_suggests_all_models_without_becoming_an_allowlist(client):
    import bot_provider
    http, profile = client
    public = http.get('/api/chat/provider-profiles', headers={'x-owner': 'alice'}).json()['profiles'][0]
    assert {'deepseek-v4.1-flash', 'gpt-6-luna', 'gemini-4-argon-contributor', 'claude-haiku-5-5', 'qwen3.8-max'} <= set(public['models'])
    cfg = bot_provider.resolve_bot_provider('alice', {'provider_profile_id': 'opencode', 'model': 'new-go-model'})
    assert cfg['model'] == 'new-go-model'
    prefixed = bot_provider.resolve_bot_provider('alice', {'provider_profile_id': 'opencode', 'model': 'opencode-go/gpt-6-luna'})
    assert prefixed['model'] == 'gpt-6-luna'
