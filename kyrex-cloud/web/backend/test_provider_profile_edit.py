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
