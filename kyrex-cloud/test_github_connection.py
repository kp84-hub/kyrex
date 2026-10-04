import base64
import json
import pytest
from github_connection import GitHubConnection, GitHubError

TOKEN = 'github_pat_' + 'A' * 40

@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET', 'github-unit-test-secret')
    calls = []
    def transport(token, path, params=None):
        calls.append((token, path, params))
        if path == '/repos/owner/private':
            return {'full_name': 'owner/private'}
        if path.endswith('/README.md'):
            return {'type': 'file', 'size': 5, 'encoding': 'base64', 'sha': 'abc',
                    'content': base64.b64encode(b'hello').decode()}
        return [{'path': 'README.md', 'type': 'file', 'size': 5}]
    return GitHubConnection(tmp_path / 'github.json', transport), calls

def test_sealed_selected_repo_reads_and_owner_isolation(connection):
    c, calls = connection
    c.connect('alice', TOKEN, ['owner/private'])
    assert TOKEN not in c.path.read_text()
    assert 'sealed' not in json.dumps(c.view('alice'))
    assert c.read('alice', 'repositories')['repositories'] == ['owner/private']
    assert c.read('alice', 'contents', 'owner/private', 'README.md', 'feature/test')['content'] == 'hello'
    assert calls[-1][2] == {'ref': 'feature/test'}
    assert not c.view('bob')['connected']
    with pytest.raises(GitHubError, match='Connect GitHub'):
        c.read('bob', 'repositories')
    with pytest.raises(GitHubError, match='not selected'):
        c.read('alice', 'contents', 'other/private')
    c.disconnect('alice')
    assert TOKEN not in c.path.read_text()
    with pytest.raises(GitHubError):
        c.read('alice', 'contents', 'owner/private', 'README.md')

@pytest.mark.parametrize('path', ['../secret', '/etc/passwd', 'a/../b', 'a\\b', 'a\n'])
def test_paths_fail_before_network(connection, path):
    c, calls = connection
    c.connect('alice', TOKEN, ['owner/private'])
    n = len(calls)
    with pytest.raises(GitHubError): c.read('alice', 'contents', 'owner/private', path)
    assert len(calls) == n

@pytest.mark.parametrize('action', ['push', 'delete', 'merge', 'http://evil'])
def test_no_mutations(connection, action):
    c, calls = connection
    with pytest.raises(GitHubError): c.read('alice', action)
    assert not calls

def test_failed_connect_does_not_replace_existing(connection):
    c, _ = connection
    c.connect('alice', TOKEN, ['owner/private'])
    previous = c.path.read_text()
    def denied(*args): raise GitHubError('denied')
    c.transport = denied
    with pytest.raises(GitHubError): c.connect('alice', TOKEN, ['owner/private'])
    assert c.path.read_text() == previous

def test_binary_and_limits(connection):
    c, _ = connection
    c.connect('alice', TOKEN, ['owner/private'])
    for result in [
        {'type': 'symlink'},
        {'type': 'file', 'encoding': 'base64', 'size': 256001},
        {'type': 'file', 'encoding': 'base64', 'size': 1, 'content': 'AA=='},
    ]:
        c.transport = lambda *args: result
        with pytest.raises(GitHubError): c.read('alice', 'contents', 'owner/private', 'f')

def test_no_token_or_body_echo_on_transport_failure(monkeypatch):
    import github_connection as gh
    import urllib.error
    class Opener:
        def open(self, req, timeout):
            assert req.method == 'GET'
            assert req.full_url.startswith('https://api.github.com/')
            raise urllib.error.HTTPError(req.full_url, 401, TOKEN, {}, None)
    monkeypatch.setattr(gh.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(GitHubError) as e: gh.request(TOKEN, '/repos/owner/private')
    assert TOKEN not in str(e.value)


def test_missing_encryption_fails_before_network(connection, monkeypatch):
    c, calls = connection
    monkeypatch.delenv('WEB_SESSION_SECRET')
    monkeypatch.delenv('KYREX_PROVIDER_SECRETS_KEY', raising=False)
    import connectors
    with pytest.raises(connectors.ConnectorError): c.connect('alice', TOKEN, ['owner/private'])
    assert not calls
