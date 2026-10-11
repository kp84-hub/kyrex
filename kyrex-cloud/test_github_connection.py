import base64
import json
import pytest
from github_connection import GitHubConnection, GitHubError

TOKEN = 'github_pat_' + 'A' * 40


def file_payload(text, sha='1' * 40):
    raw = text.encode('utf-8')
    return {'type': 'file', 'encoding': 'base64', 'size': len(raw), 'sha': sha,
            'content': base64.b64encode(raw).decode()}

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


def test_paged_read_reaches_large_file_tail_without_splitting_unicode(connection):
    c, calls = connection
    c.connect('alice', TOKEN, ['owner/private'])
    text = 'a' * 39999 + '😀' + 'b' * 30555 + '\nFINAL_SYNTHESIS = True\n'
    pages = []
    def transport(token, path, params):
        assert token == TOKEN and path.endswith('/chat.py') and params == {'ref': 'feature/review'}
        return file_payload(text)
    c.transport = transport
    page = c.read('alice', 'contents', 'owner/private', 'chat.py', 'feature/review')
    assert len(page['content']) == 40000 and page['content'].endswith('😀')
    assert page['truncated'] and page['next_offset'] == 40000
    pages.append(page['content'])
    page = c.read('alice', 'contents', 'owner/private', 'chat.py', 'feature/review',
                  offset=page['next_offset'], expected_sha=page['sha'])
    pages.append(page['content'])
    assert ''.join(pages) == text
    assert page['offset'] == 40000 and page['total_chars'] == len(text)
    assert not page['truncated'] and page['next_offset'] is None
    assert 'FINAL_SYNTHESIS = True' in page['content']
    assert TOKEN not in json.dumps(page) and page['untrusted_repository_content']


@pytest.mark.parametrize('args', [
    {'offset': -1}, {'offset': True}, {'offset': '40000'}, {'offset': 256001},
    {'offset': 1}, {'limit': 0}, {'limit': True}, {'limit': '10'}, {'limit': 40001},
    {'expected_sha': 'not-a-sha'}, {'expected_sha': None},
])
def test_invalid_pages_fail_before_network(connection, args):
    c, calls = connection
    c.connect('alice', TOKEN, ['owner/private'])
    before = len(calls)
    with pytest.raises(GitHubError): c.read('alice', 'contents', 'owner/private', 'README.md', **args)
    assert len(calls) == before


def test_file_change_cannot_mix_page_versions(connection):
    c, _ = connection; c.connect('alice', TOKEN, ['owner/private'])
    c.transport = lambda *a: file_payload('original source', '1' * 40)
    first = c.read('alice', 'contents', 'owner/private', 'chat.py', limit=5)
    c.transport = lambda *a: file_payload('replacement source', '2' * 40)
    with pytest.raises(GitHubError, match='changed between reads'):
        c.read('alice', 'contents', 'owner/private', 'chat.py', offset=first['next_offset'],
               expected_sha=first['sha'])


def test_empty_file_eof_and_out_of_bounds(connection):
    c, _ = connection; c.connect('alice', TOKEN, ['owner/private'])
    c.transport = lambda *a: file_payload('')
    page = c.read('alice', 'contents', 'owner/private', 'empty.py')
    assert page['content'] == '' and page['total_chars'] == 0 and page['next_offset'] is None
    assert not page['truncated']
    with pytest.raises(GitHubError, match='past the end'):
        c.read('alice', 'contents', 'owner/private', 'empty.py', offset=1, expected_sha=page['sha'])


def test_pages_preserve_selected_repository_and_owner_scope(connection):
    c, calls = connection; c.connect('alice', TOKEN, ['owner/private'])
    before = len(calls)
    for owner, repo in [('bob', 'owner/private'), ('alice', 'owner/unselected')]:
        with pytest.raises(GitHubError):
            c.read(owner, 'contents', repo, 'chat.py', offset=40000, expected_sha='1' * 40)
    assert len(calls) == before
    c.disconnect('alice')
    with pytest.raises(GitHubError, match='Connect GitHub'):
        c.read('alice', 'contents', 'owner/private', 'chat.py', offset=40000, expected_sha='1' * 40)


def test_pagination_is_not_directory_or_status_pagination(connection):
    c, calls = connection; c.connect('alice', TOKEN, ['owner/private'])
    with pytest.raises(GitHubError, match='not directories'):
        c.read('alice', 'contents', 'owner/private', limit=10)
    before = len(calls)
    for action in ('status', 'repositories'):
        with pytest.raises(GitHubError, match='only for file contents'):
            c.read('alice', action, limit=10)
    assert len(calls) == before

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
