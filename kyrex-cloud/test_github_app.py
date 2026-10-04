import base64
import json
import time
import urllib.parse
import pytest
import connectors
from github_app import GitHubAppFlow
from github_connection import GitHubConnection, GitHubError

BASE = 'https://chat.kyrex.test'
SECRET = 'app-client-secret-test'
TOKEN = 'ghu_TEST_TOKEN'
REFRESH = 'ghr_TEST_REFRESH'

def state(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['state'][0]

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET', 'github-flow-test')
    calls = []
    mode = {'login': 'alice', 'permissions': {'contents': 'read', 'metadata': 'read'},
            'installation': 7, 'repos': ['alice/private'], 'app_id': 12}
    def transport(token, path, params=None):
        calls.append((path, params))
        if path == '/user': return {'login': mode['login']}
        if path == '/user/installations': return {'total_count': 1, 'installations': [{
            'app_id': mode['app_id'], 'id': mode['installation'], 'permissions': mode['permissions']}]} if mode['installation'] else {'installations': []}
        if path.startswith('/user/installations/'): return {'total_count': len(mode['repos']), 'repositories': [{'full_name': r} for r in mode['repos']]}
        if path.endswith('/README.md'): return {'type': 'file', 'encoding': 'base64', 'size': 5, 'content': base64.b64encode(b'hello').decode()}
        raise AssertionError(path)
    def post(url, values=None):
        calls.append((url, values))
        if 'app-manifests' in url:
            return {'id': 12, 'slug': 'kyrex-reader-test', 'owner': {'login': mode['login']},
                    'permissions': mode['permissions'], 'client_id': 'Iv1.TEST', 'client_secret': SECRET,
                    'pem': 'PRIVATE_KEY_NOT_NEEDED', 'webhook_secret': 'WEBHOOK_NOT_NEEDED'}
        return {'access_token': TOKEN, 'refresh_token': REFRESH, 'expires_in': 28800, 'refresh_token_expires_in': 15897600}
    c = GitHubConnection(tmp_path / 'connection.json', transport)
    f = GitHubAppFlow(tmp_path / 'apps.json', transport, post, c)
    return f, c, mode, calls

def register(f):
    url = f.begin('alice', BASE)['authorization_url']
    manifest = f.manifest('alice', state(url), BASE)
    assert manifest['default_permissions'] == {'contents': 'read', 'metadata': 'read'}
    assert manifest['setup_url'] == BASE + '/api/connections/github/installed'
    return f.registered('alice', state(url), BASE, 'registrationcode')

def authorize(f):
    install = register(f)
    return f.installed('alice', state(install), BASE, '7')

def connect(f):
    oauth = authorize(f)
    assert f.callback('alice', state(oauth), BASE, 'oauthcode') == {'connected': True}

def test_complete_flow_and_encrypted_refresh(setup):
    f, c, mode, calls = setup
    oauth = authorize(f)
    assert 'code_challenge=' in oauth and 'scope=' not in oauth
    assert f.callback('alice', state(oauth), BASE, 'oauthcode')['connected']
    assert c.view('alice')['connected'] and not c.view('bob')['connected']
    text = f.path.read_text() + c.path.read_text()
    for secret in (SECRET, TOKEN, REFRESH, 'PRIVATE_KEY_NOT_NEEDED', 'WEBHOOK_NOT_NEEDED'):
        assert secret not in text
    exchange = next(v for url, v in calls if url == 'https://github.com/login/oauth/access_token')
    assert exchange['code_verifier']
    with pytest.raises(GitHubError): f.callback('alice', state(oauth), BASE, 'oauthcode')
    # Force expiry and refresh through the live-read path.
    data = c._load(); key = connectors.ConnectorStore._owner_key('alice')
    payload = connectors.unseal_tokens(data[key]['sealed']); payload['expires_at'] = time.time() - 1
    data[key]['sealed'] = connectors.seal_tokens(payload); c._save(data)
    rec = c._load()[key]
    token, repos = f.credentials('alice', rec)
    assert token == TOKEN and repos == ['alice/private']
    assert any(v and v.get('grant_type') == 'refresh_token' for url, v in calls if url.startswith('https://'))

@pytest.mark.parametrize('owner,base,kind', [('bob', BASE, 'manifest'), ('alice','https://evil.test','manifest'), ('alice', BASE,'oauth')])
def test_owner_origin_and_step_bound_state(setup, owner, base, kind):
    f, _, _, calls = setup
    url = f.begin('alice', BASE)['authorization_url']
    with pytest.raises(GitHubError): f._transaction(owner, state(url), base, kind)
    assert not calls
    assert f.manifest('alice', state(url), BASE)  # Invalid attempts do not consume another owner's state.

def test_expired_state_and_cancel(setup):
    f, _, _, _ = setup
    url = f.begin('alice', BASE)['authorization_url']
    data = f._load()
    for tx in data['states'].values(): tx['expires'] = time.time() - 1
    f._save(data)
    with pytest.raises(GitHubError): f.manifest('alice', state(url), BASE)
    url = f.begin('alice', BASE)['authorization_url']; f.cancel('alice')
    with pytest.raises(GitHubError): f.manifest('alice', state(url), BASE)

@pytest.mark.parametrize('wrong', ['user', 'installation', 'app', 'permissions'])
def test_live_identity_installation_and_permissions_verified(setup, wrong):
    f, c, mode, _ = setup
    oauth = authorize(f)
    if wrong == 'user': mode['login'] = 'bob'
    if wrong == 'installation': mode['installation'] = 999
    if wrong == 'app': mode['app_id'] = 99
    if wrong == 'permissions': mode['permissions']['contents'] = 'write'
    with pytest.raises(GitHubError): f.callback('alice', state(oauth), BASE, 'oauthcode')
    assert not c.view('alice')['connected']

def test_registration_validates_creator_and_readonly_app(setup):
    f, c, mode, _ = setup
    url = f.begin('alice', BASE)['authorization_url']; mode['login'] = 'bob'
    with pytest.raises(GitHubError): f.registered('alice', state(url), BASE, 'code')
    assert not f.config('alice')
    mode['login'] = 'alice'; mode['permissions']['contents'] = 'write'
    url = f.begin('alice', BASE)['authorization_url']
    with pytest.raises(GitHubError): f.registered('alice', state(url), BASE, 'code')
    assert not f.config('alice')

def test_current_repository_selection_and_revocation(setup):
    f, c, mode, _ = setup; connect(f)
    rec = c._load()[connectors.ConnectorStore._owner_key('alice')]
    assert f.credentials('alice', rec)[1] == ['alice/private']
    mode['repos'] = ['alice/second']
    assert f.credentials('alice', rec)[1] == ['alice/second']
    mode['installation'] = None
    with pytest.raises(GitHubError): f.credentials('alice', rec)

def test_disconnect_during_exchange_cannot_reconnect(setup):
    f, c, _, _ = setup; oauth = authorize(f)
    original = f.post
    def cancelled(url, values=None):
        result = original(url, values); f.cancel('alice'); c.disconnect('alice'); return result
    f.post = cancelled
    with pytest.raises(GitHubError, match='cancelled'): f.callback('alice', state(oauth), BASE, 'code')
    assert not c.view('alice')['connected']

def test_no_installation_routes_to_repository_selection(setup):
    f, _, mode, _ = setup; register(f); mode['installation'] = None
    oauth = f.begin('alice', BASE)['authorization_url']
    result = f.callback('alice', state(oauth), BASE, 'code')
    assert '/installations/new?' in result['next_url']

def test_fixed_post_endpoints_and_sanitized_errors(monkeypatch):
    import github_app
    import urllib.error
    class Opener:
        def open(self, req, timeout):
            assert req.method == 'POST'
            assert req.full_url == 'https://github.com/login/oauth/access_token'
            assert SECRET.encode() in req.data
            raise urllib.error.HTTPError(req.full_url, 400, SECRET, {}, None)
    monkeypatch.setattr(github_app.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(GitHubError) as error:
        github_app.post_json('https://github.com/login/oauth/access_token', {'client_secret': SECRET})
    assert SECRET not in str(error.value)
    with pytest.raises(GitHubError): github_app.post_json('https://evil.test/login/oauth/access_token')


def test_reconnect_uses_existing_registration(setup):
    f, c, _, calls = setup; connect(f)
    f.cancel('alice'); c.disconnect('alice')
    url = f.begin('alice', BASE)['authorization_url']
    assert '/login/oauth/authorize?' in url
    assert f.callback('alice', state(url), BASE, 'code')['connected']
    assert len([p for p, _ in calls if 'app-manifests' in p]) == 1


def test_encryption_missing_prevents_registration(setup, monkeypatch):
    f, _, _, calls = setup
    monkeypatch.delenv('WEB_SESSION_SECRET')
    monkeypatch.delenv('KYREX_PROVIDER_SECRETS_KEY', raising=False)
    with pytest.raises(connectors.ConnectorError): f.begin('alice', BASE)
    assert not calls
