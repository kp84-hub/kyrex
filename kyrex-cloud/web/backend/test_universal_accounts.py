"""Offline account admission, legacy compatibility and managed boundary checks."""
import asyncio
import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
import account_store as accounts
import account_api as api
import chat_service
import managed_chat
import provider_profiles
from web_sessions import SessionStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'test-only-account-secret')
    monkeypatch.setenv('KYREX_ACCOUNTS_ENABLED', '1')
    monkeypatch.setenv('KYREX_MANAGED_ACCOUNTS_ENABLED', '1')
    monkeypatch.setenv('KYREX_SIGNUP_MODE', 'invite')
    monkeypatch.setenv('KYREX_LOGIN_SMTP_HOST', 'mail.example.test')
    monkeypatch.setenv('KYREX_LOGIN_EMAIL_FROM', 'signin@example.test')
    monkeypatch.setenv('KYREX_LOGIN_GOOGLE_CLIENT_ID', 'test-only-google-client')
    monkeypatch.setenv('KYREX_LOGIN_GOOGLE_CLIENT_SECRET', 'test-only-google-secret')
    monkeypatch.setenv('GITHUB_CLIENT_ID', 'test-client')
    monkeypatch.setenv('GITHUB_CLIENT_SECRET', 'test-secret')
    monkeypatch.setenv('WEB_ALLOWED_GITHUB_USERNAME', 'operator')
    main = importlib.import_module('main')
    monkeypatch.setattr(main, 'ALLOWED_USERNAME', 'operator')
    monkeypatch.setattr(main, 'sessions', SessionStore(secret='test-only-account-secret', path=tmp_path / 'sessions.db'))
    import bots
    monkeypatch.setattr(bots, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(bots, 'BOTS_FILE', str(tmp_path / 'bots.json'))
    from task_store import CloudTaskStore
    monkeypatch.setattr(main, 'store', CloudTaskStore())
    accounts.bootstrap_legacy('operator')
    main.sessions['test-parent-session'] = 'operator'
    with TestClient(main.app, base_url='https://testserver') as client:
        client.cookies.set('session', 'test-parent-session')
        yield main, client, monkeypatch


def post(client, path, data=None, method='POST'):
    return client.request(method, path, json=data or {}, headers={'origin': 'https://testserver'})


def adult(address='person@example.test'):
    accounts.invite(address)
    account = accounts.admit('email', address, verified_email=address)
    return accounts.save_profile(account['owner'], {'name': 'Taylor', 'timezone': 'Europe/Rome', 'adult': True})


def child(client):
    response = post(client, '/api/account/family', {'name': 'Avery', 'age_band': 'under13', 'consent': True})
    assert response.status_code == 201, response.text
    return response.json()['account']


def pair(client, account):
    response = post(client, '/api/account/family/' + account['owner'] + '/pair')
    assert response.status_code == 200, response.text
    return response.json()['pairing_code']


def learner(main, parent_client, account):
    token = pair(parent_client, account)
    client = TestClient(main.app, base_url='https://testserver')
    response = post(client, '/auth/managed/pair', {'code': token})
    assert response.status_code == 200, response.text
    assert 'Secure' in response.headers['set-cookie'] and 'HttpOnly' in response.headers['set-cookie']
    return client


def model(main, parent_client, account, monkeypatch):
    monkeypatch.setenv('KYREX_MANAGED_MODEL_ALLOWLIST', json.dumps([['openai', 'https://api.openai.com/v1', 'learning-test']]))
    provider_profiles.save_profile('operator', {'id': 'learning', 'name': 'Learning', 'provider': 'openai',
        'base_url': 'https://api.openai.com/v1', 'api_key': 'test-only-parent-provider-credential', 'models': ['learning-test']})
    response = post(parent_client, '/api/account/family/' + account['owner'], {
        'provider_profile_id': 'learning', 'model': 'learning-test', 'daily_messages': 25}, method='PATCH')
    assert response.status_code == 200, response.text
    calls = []
    class Provider:
        async def chat(self, **kwargs):
            calls.append(kwargs)
            kwargs['stream_callback']('Here is a small example.')
            return {'content': 'Here is a small example.'}
    def factory(*args, **kwargs):
        calls.append((args, kwargs)); return Provider()
    monkeypatch.setattr(managed_chat, 'get_provider', factory)
    return calls


def test_options_are_configured_and_closed_by_default(env):
    main, client, monkeypatch = env
    value = client.get('/auth/options').json()
    assert value == {'enabled': True, 'github': True, 'google': True, 'email': True, 'signup_mode': 'invite', 'managed': True}
    monkeypatch.delenv('KYREX_LOGIN_GOOGLE_CLIENT_SECRET')
    assert client.get('/auth/options').json()['google'] is False
    monkeypatch.setenv('KYREX_ACCOUNTS_ENABLED', '0')
    value = client.get('/auth/options').json()
    assert not value['enabled'] and not value['google'] and not value['email'] and not value['managed']
    assert client.get('/api/me').json()['username'] == 'operator'


def test_legacy_migration_keeps_owner_paths_and_persistent_identity(env):
    main, client, _ = env
    path = chat_service.create_conversation('operator')['conversation_id']
    first = api.github_owner({'login': 'operator', 'id': 100})
    # Renaming the upstream account does not rename any Kyrex data.
    second = api.github_owner({'login': 'new-upstream-name', 'id': 100})
    assert first == second == 'operator'
    assert chat_service.get_conversation(second, path)
    assert accounts.get('operator')['id'] == client.get('/api/account').json()['account']['id']
    with pytest.raises(main.HTTPException): api.github_owner({'login': 'operator', 'id': 101})


def test_invitation_required_and_verified_provider_subject_is_unique(env):
    with pytest.raises(accounts.AccountError): accounts.admit('email', 'new@example.test', verified_email='new@example.test')
    a = adult()
    assert accounts.isolated(a['owner']) and a['owner'] != 'person@example.test'
    assert accounts.admit('email', 'person@example.test', verified_email='person@example.test')['id'] == a['id']
    with pytest.raises(accounts.AccountError): accounts.admit('google', 'google-subject', verified_email='person@example.test')
    # Matching email alone never links an upstream identity.
    accounts.invite('person@example.test')
    b = accounts.admit('google', 'google-subject', verified_email='person@example.test')
    assert a['id'] != b['id']
    with pytest.raises(accounts.AccountError): accounts.link(a['owner'], 'google', 'google-subject')


def test_concurrent_identity_admission_returns_one_account(env):
    accounts.invite('race@example.test')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: accounts.admit('email', 'race@example.test', verified_email='race@example.test'), range(2)))
    assert results[0]['id'] == results[1]['id']


def test_email_code_is_browser_bound_single_use_and_never_returned(env):
    main, client, monkeypatch = env
    accounts.invite('new@example.test')
    sent = []
    monkeypatch.setattr(api, 'send_code', lambda address, code: sent.append((address, code)))
    response = post(client, '/auth/email/start', {'email': 'new@example.test', 'adult': True})
    challenge = response.json()['challenge']
    assert sent[0][1] not in response.text
    with accounts.database() as db:
        assert sent[0][1] not in '\n'.join(r['value'] for r in db.execute('SELECT value FROM transactions'))
    other = TestClient(main.app, base_url='https://testserver')
    assert post(other, '/auth/email/verify', {'challenge': challenge, 'code': sent[0][1]}).status_code == 400
    response = post(client, '/auth/email/verify', {'challenge': challenge, 'code': sent[0][1]})
    assert response.status_code == 200
    assert accounts.isolated(client.get('/api/me').json()['username'])
    assert post(client, '/auth/email/verify', {'challenge': challenge, 'code': sent[0][1]}).status_code == 400


def test_email_wrong_code_attempts_persist_and_rate_limits_survive_calls(env):
    _, client, monkeypatch = env
    monkeypatch.setattr(api, 'send_code', lambda *args: None)
    value = post(client, '/auth/email/start', {'email': 'new@example.test', 'adult': True}).json()
    for _ in range(5):
        assert post(client, '/auth/email/verify', {'challenge': value['challenge'], 'code': 'not-code'}).status_code == 400
    # Eight-character wrong values consume attempts too.
    with accounts.database() as db:
        assert not db.execute('SELECT * FROM transactions WHERE id=?', (accounts.digest(value['challenge']),)).fetchone()
    for _ in range(2): assert post(client, '/auth/email/start', {'email': 'new@example.test', 'adult': True}).status_code == 200
    assert post(client, '/auth/email/start', {'email': 'new@example.test', 'adult': True}).status_code == 400


def test_google_only_requests_identity_scopes_and_rejects_wrong_browser(env):
    main, client, monkeypatch = env
    response = client.get('/auth/google/login?adult=true', follow_redirects=False)
    query = parse_qs(urlsplit(response.headers['location']).query)
    assert query['scope'] == ['openid email'] and query['code_challenge_method'] == ['S256']
    assert 'gmail' not in response.headers['location'] and 'calendar' not in response.headers['location']
    monkeypatch.setattr(api.requests, 'post', lambda *a, **kw: pytest.fail('Unbound callback must not reach Google'))
    other = TestClient(main.app, base_url='https://testserver')
    assert other.get('/auth/google/callback', params={'state': query['state'][0], 'code': 'test-code'}).status_code == 400


def test_google_verifies_id_token_audience_nonce_and_stable_subject(env):
    main, client, monkeypatch = env
    accounts.invite('google@example.test')
    response = client.get('/auth/google/login?adult=true', follow_redirects=False)
    query = parse_qs(urlsplit(response.headers['location']).query)
    from google.oauth2 import id_token
    seen = []
    class Response:
        status_code = 200
        content = b'{}'
        def json(self): return {'id_token': 'test-only-id-token'}
        def close(self): pass
    monkeypatch.setattr(api.requests, 'post', lambda *a, **kw: (seen.append(kw) or Response()))
    def verified(token, request, audience):
        assert token == 'test-only-id-token' and audience == 'test-only-google-client'
        return {'iss': 'https://accounts.google.com', 'sub': 'stable-google-subject', 'email': 'google@example.test',
                'email_verified': True, 'nonce': query['nonce'][0]}
    monkeypatch.setattr(id_token, 'verify_oauth2_token', verified)
    result = client.get('/auth/google/callback', params={'state': query['state'][0], 'code': 'test-code'}, follow_redirects=False)
    assert result.status_code == 303 and seen[0]['allow_redirects'] is False
    assert seen[0]['data']['code_verifier']
    assert accounts.isolated(client.get('/api/me').json()['username'])
    assert client.get('/auth/google/callback', params={'state': query['state'][0], 'code': 'test-code'}).status_code == 400


def test_new_github_signin_is_browser_bound_and_can_link_without_changing_owner(env):
    main, client, monkeypatch = env
    a = adult('linking@example.test')
    main.sessions['linking-session'] = a['owner']; client.cookies.set('session', 'linking-session')
    started = client.get('/auth/login?link=true', follow_redirects=False)
    state = parse_qs(urlsplit(started.headers['location']).query)['state'][0]
    outsider = TestClient(main.app, base_url='https://testserver')
    assert outsider.get('/auth/callback', params={'code': 'github-code', 'state': state}, follow_redirects=False).status_code == 400
    class Response:
        def __init__(self, data): self.data = data
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.data).encode()
    from unittest.mock import patch
    with patch.object(main.urllib.request, 'urlopen', side_effect=[Response({'access_token': 'test-only-upstream-token'}), Response({'login': 'linked-person', 'id': 234})]):
        done = client.get('/auth/callback', params={'code': 'github-code', 'state': state}, follow_redirects=False)
    assert done.status_code == 307, done.text
    assert client.get('/api/me').json()['username'] == a['owner']
    assert api.github_owner({'login': 'renamed-linked-person', 'id': 234}) == a['owner']


def test_new_account_ordinary_chat_resolves_only_its_selected_provider_and_preferences(env):
    _, _, monkeypatch = env
    a = adult('provider-owner@example.test')
    monkeypatch.setenv('KYREX_MODEL', 'host-model')
    provider_profiles.save_profile(a['owner'], {'id': 'personal', 'name': 'Personal', 'provider': 'openai',
        'base_url': 'https://api.openai.com/v1', 'api_key': 'test-only-personal-provider-credential', 'models': ['personal-model'],
        'headers': {'X-Test-Header': 'test-only-header-value'}})
    cfg = chat_service._resolve_provider(user=a['owner'])
    assert cfg['profile'] == 'personal' and cfg['model'] == 'personal-model'
    assert cfg['api_key'] == 'test-only-personal-provider-credential'
    assert cfg['headers'] == {'X-Test-Header': 'test-only-header-value'}
    assert 'Europe/Rome' in accounts.preference_context(a['owner'])
    assert 'Taylor' in accounts.preference_context(a['owner'])
    assert accounts.preference_context('operator') == ''


def test_new_identity_completes_existing_provider_onboarding_with_its_own_bot(env):
    main, client, monkeypatch = env
    import chat_onboarding
    import bots
    import bot_provider
    a = adult('first-run@example.test')
    main.sessions['first-run-session'] = a['owner']; client.cookies.set('session', 'first-run-session')
    class Response:
        status_code = 200
        def iter_content(self, size): return iter([json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}).encode()])
        def close(self): pass
    monkeypatch.setattr(chat_onboarding.requests, 'request', lambda *args, **kwargs: Response())
    setup = post(client, '/api/chat/onboarding/provider', {'provider': 'openai', 'api_key': 'test-only-new-user-provider-key'})
    assert setup.status_code == 200, setup.text
    assert post(client, '/api/chat/onboarding/test', {'model': 'first-run-model'}).status_code == 200
    completed = post(client, '/api/chat/onboarding/complete')
    assert completed.status_code == 200, completed.text
    state = completed.json()
    bot = bots.get_bot(state['bot_id'])
    assert bot['owner'] == a['owner']
    assert bot_provider.resolve_bot_provider(a['owner'], bot)['api_key'] == 'test-only-new-user-provider-key'
    assert chat_service.get_conversation(a['owner'], state['conversation_id'])
    assert chat_service.get_conversation('operator', state['conversation_id']) is None
    assert client.get('/api/chat/onboarding').json()['required'] is False


def test_new_account_cannot_use_operator_providers_bots_or_workspaces(env):
    main, client, monkeypatch = env
    account = adult()
    main.sessions['new-person-session'] = account['owner']; client.cookies.set('session', 'new-person-session')
    monkeypatch.setenv('KYREX_PROVIDER', 'openai'); monkeypatch.setenv('KYREX_MODEL', 'host-model')
    monkeypatch.setenv('KYREX_API_KEY', 'test-only-host-provider-credential')
    assert client.get('/api/chat/providers').json()['providers'] == []
    with pytest.raises(chat_service.ChatUnavailable): chat_service._resolve_provider('openai', 'host-model', user=account['owner'])
    assert not chat_service._bot_visible_to({'owner': ''}, account['owner'])
    assert chat_service._bot_visible_to({'owner': ''}, 'operator')
    assert client.get('/api/chat/workspaces').json()['workspaces'] == []
    assert post(client, '/api/task', {'task': 'edit the deployment repository'}).status_code == 403
    assert post(client, '/api/chat', {'message': 'read it', 'workspace_id': 'default'}).status_code == 403
    assert client.post('/api/conversations', json={}).status_code == 403
    assert client.post('/api/conversations', json={}, headers={'origin': 'https://evil.example', 'authorization': 'Bearer invalid'}).status_code == 403


def test_new_account_profile_gate_timezone_and_independent_owner_isolation(env):
    main, client, _ = env
    accounts.invite('unfinished@example.test')
    a = accounts.admit('email', 'unfinished@example.test', verified_email='unfinished@example.test')
    main.sessions['unfinished'] = a['owner']; client.cookies.set('session', 'unfinished')
    assert client.get('/api/conversations').status_code == 403
    assert post(client, '/api/account/profile', {'name': 'Taylor', 'timezone': 'bad/time', 'adult': True}, 'PUT').status_code == 400
    assert post(client, '/api/account/profile', {'name': 'Taylor', 'timezone': 'Europe/Rome', 'adult': True}, 'PUT').status_code == 200
    conv = post(client, '/api/conversations').json()
    b = adult('other@example.test')
    assert chat_service.get_conversation(b['owner'], conv['conversation_id']) is None
    with pytest.raises(ValueError): chat_service.get_conversation(a['owner'], '../operator/provider_profiles')


def test_parent_relationship_is_not_shared_authentication(env):
    main, client, _ = env
    account = child(client)
    child_client = learner(main, client, account)
    assert child_client.get('/api/account').json()['account']['owner'] == account['owner']
    assert client.get('/api/me').json()['username'] == 'operator'
    second = adult('other-parent@example.test')
    main.sessions['other-parent'] = second['owner']
    with TestClient(main.app, base_url='https://testserver') as other:
        other.cookies.set('session', 'other-parent')
        assert other.get('/api/account/family').json()['members'] == []
        assert post(other, '/api/account/family/' + account['owner'] + '/pair').status_code == 400


@pytest.mark.parametrize('path,method', [('/api/bots', 'GET'), ('/api/task', 'POST'),
    ('/api/chat/onboarding/provider', 'POST'), ('/api/chat/provider-profiles', 'GET'),
    ('/api/connections', 'GET'), ('/api/connections/google/connect', 'POST'),
    ('/api/voice/session', 'GET'), ('/api/account/family', 'POST'), ('/api/chat/workspace', 'POST')])
def test_managed_session_cannot_reach_tools_connections_or_parent_controls(env, path, method):
    main, client, _ = env
    account = child(client)
    with learner(main, client, account) as child_client:
        assert post(child_client, path, method=method).status_code == 403


def test_pairing_is_single_use_and_pausing_revocation_and_deletion_are_enforced(env):
    main, client, _ = env
    account = child(client); base = '/api/account/family/' + account['owner']
    token = pair(client, account)
    child_client = TestClient(main.app, base_url='https://testserver')
    assert post(child_client, '/auth/managed/pair', {'code': token}).status_code == 200
    assert post(child_client, '/auth/managed/pair', {'code': token}).status_code == 400
    conv = post(child_client, '/api/conversations').json()
    assert client.get(base + '/conversations').json()['conversations'][0]['conversation_id'] == conv['conversation_id']
    assert post(client, base, {'active': False}, 'PATCH').status_code == 200
    assert child_client.get('/api/conversations').status_code in (401, 403)
    assert post(client, base, {'active': True}, 'PATCH').status_code == 200
    pending = pair(client, account)
    assert post(client, base + '/revoke-devices').status_code == 200
    assert child_client.get('/api/conversations').status_code == 401
    assert post(child_client, '/auth/managed/pair', {'code': pending}).status_code == 400
    assert post(client, base, {'confirm_name': 'Avery'}, 'DELETE').status_code == 200
    assert chat_service.list_conversations(account['owner']) == []
    assert client.get('/api/account/family').json()['members'] == []


def test_managed_chat_never_routes_to_engine_or_connectors_and_uses_parent_grant(env):
    main, client, monkeypatch = env
    account = child(client); calls = model(main, client, account, monkeypatch)
    monkeypatch.setattr(chat_service, '_get_engine_session', lambda *a, **kw: pytest.fail('No engine in managed chat'))
    with learner(main, client, account) as child_client:
        response = post(child_client, '/api/chat', {'message': 'Send an email and run code for me.', 'request_id': 'turn-test'})
        assert response.status_code == 200 and '"type": "done"' in response.text, response.text
        assert calls[0][0][1] == 'test-only-parent-provider-credential'
        assert calls[1]['tools'] is None and calls[1]['model'] == 'learning-test'
        assert len(calls[1]['messages']) == 2
        assert 'test-only-parent-provider-credential' not in response.text
        assert provider_profiles.list_profiles(account['owner']) == []
        transcript = child_client.get('/api/conversations').json()['conversations'][0]
        assert chat_service.get_conversation('operator', transcript['conversation_id']) is None
        assert 'Here is a small example.' in client.get('/api/account/family/' + account['owner'] + '/conversations/' + transcript['conversation_id']).text


def test_managed_model_allowlist_rechecked_and_global_fallback_blocked(env):
    main, client, monkeypatch = env
    account = child(client)
    with learner(main, client, account) as child_client:
        response = post(child_client, '/api/chat', {'message': 'Hello'})
        assert 'choose an enabled learning model' in response.text
        model(main, client, account, monkeypatch)
        monkeypatch.setenv('KYREX_MANAGED_MODEL_ALLOWLIST', '[]')
        response = post(child_client, '/api/chat', {'message': 'Hello'})
        assert 'choose an enabled learning model' in response.text


def test_managed_feature_off_blocks_existing_sessions(env):
    main, client, monkeypatch = env
    account = child(client)
    with learner(main, client, account) as child_client:
        monkeypatch.setenv('KYREX_MANAGED_ACCOUNTS_ENABLED', '0')
        assert child_client.get('/api/conversations').status_code in (401, 403)


def test_desktop_new_flow_reuses_pkce_for_google_email_and_managed_sessions(env):
    main, client, _ = env
    import base64, hashlib
    verifier = 'test-only-desktop-verifier-long-enough'
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    response = client.get('/auth/desktop/start', params={'state': 'native-state', 'redirect_uri': main.CHAT_DESKTOP_REDIRECT_URI,
        'code_challenge': challenge, 'code_challenge_method': 'S256'}, follow_redirects=False)
    tx = parse_qs(urlsplit(response.headers['location']).query)['desktop'][0]
    result = post(client, '/api/account/desktop/finish', {'transaction': tx})
    assert result.status_code == 200, result.text
    handoff = parse_qs(urlsplit(result.json()['url']).query)
    exchange = post(client, '/auth/desktop/exchange', {'code': handoff['code'][0], 'redirect_uri': main.CHAT_DESKTOP_REDIRECT_URI, 'code_verifier': verifier})
    assert exchange.status_code == 200 and exchange.json()['username'] == 'operator'
    assert post(client, '/api/account/desktop/finish', {'transaction': tx}).status_code == 400


def test_device_revocation_blocks_native_tokens_refresh_and_racing_cookie_mint(env):
    main, client, _ = env
    account = child(client)
    old_epoch = accounts.session_epoch(account['owner'])
    tokens = main._new_desktop_tokens(account['owner'])
    assert post(client, '/api/account/family/' + account['owner'] + '/revoke-devices').status_code == 200
    bearer = {'authorization': 'Bearer ' + tokens['access_token']}
    outsider = TestClient(main.app, base_url='https://testserver')
    assert outsider.get('/api/me', headers=bearer).status_code == 401
    assert post(outsider, '/auth/desktop/refresh', {'refresh_token': tokens['refresh_token']}).status_code == 403
    assert outsider.post('/auth/desktop/session', headers=bearer).status_code == 403
    # A code redemption that was authorized before revocation cannot mint a
    # browser credential for the newly advanced epoch after the revocation.
    main.sessions.create('test-racing-cookie', account['owner'], old_epoch)
    assert main.sessions.get('test-racing-cookie') is None


def test_managed_websocket_is_denied_before_any_native_handler(env):
    main, client, _ = env
    from starlette.websockets import WebSocketDisconnect
    account = child(client)
    with learner(main, client, account) as child_client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with child_client.websocket_connect('wss://testserver/api/browser-hosts/anything'):
                pytest.fail('Managed sessions have no websocket access')
        assert exc.value.code == 1008


def test_managed_quota_is_durable_and_replaying_a_saved_answer_makes_no_model_call(env):
    main, client, monkeypatch = env
    account = child(client); calls = model(main, client, account, monkeypatch)
    post(client, '/api/account/family/' + account['owner'], {'daily_messages': 1}, 'PATCH')
    with learner(main, client, account) as child_client:
        first = post(child_client, '/api/chat', {'message': 'Explain a variable.', 'request_id': 'first'})
        assert '"type": "done"' in first.text
        conv = child_client.get('/api/conversations').json()['conversations'][0]['conversation_id']
        replay = post(child_client, '/api/chat', {'conversation_id': conv, 'message': 'Explain a variable.', 'request_id': 'first'})
        assert '"type": "done"' in replay.text and len(calls) == 2
        denied = post(child_client, '/api/chat', {'conversation_id': conv, 'message': 'One more question.', 'request_id': 'second'})
        assert 'Too many attempts' in denied.text and len(calls) == 2
