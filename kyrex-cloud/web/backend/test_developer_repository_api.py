import pytest
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import chat_api
import connections_api
import bots
from task_store import CloudTaskStore as RealTaskStore
from github_app import GitHubAppFlow
from github_connection import GitHubConnection
from test_github_app import setup, BASE, state
from test_developer_repositories import command, workspace


@pytest.fixture
def client(workspace, monkeypatch):
    service, *rest = workspace
    monkeypatch.setattr(chat_api, '_repository_service', lambda: service)
    def owner(request):
        if request.headers.get('x-owner') == 'anonymous': raise HTTPException(401, 'Sign in first')
        return request.headers.get('x-owner', 'alice')
    monkeypatch.setattr(chat_api, '_require_user', owner)
    app = FastAPI(); app.include_router(chat_api.router)
    return TestClient(app), service, rest


def test_owner_selected_checkout_review_and_publish_api(client):
    http, service, rest = client
    bot, selected, calls, clones, original, auth_calls = rest
    response = http.post('/api/bots/dev/repository', json={'repository': 'alice/private'})
    assert response.status_code == 200
    (Path(bot['rift']) / 'README.md').write_text('API update\n')
    review = http.post('/api/bots/dev/repository/review').json()
    assert review['files'] == ['README.md'] and 'path' not in review
    published = http.post('/api/bots/dev/repository/publish', json={'snapshot': review['snapshot'], 'title': 'API update'})
    assert published.status_code == 200 and published.json()['url'].endswith('/pull/1')
    assert 'test-secret-token' not in published.text + response.text + str(review)


def test_chat_workspace_previews_current_checkout_without_pausing_or_authorizing_publish(client):
    http, _, rest = client
    bot, _, writes, _, _, auth_calls = rest
    assert http.post('/api/bots/dev/repository', json={'repository': 'alice/private'}).status_code == 200
    root = Path(bot['rift'])
    command(root, 'mv', 'README.md', 'renamed file.md')
    (root / 'extra.py').write_text('print("new file")\n')
    (root / '.env').write_text('ignored private data')
    git_status = command(root, 'status', '--porcelain')
    bot['status'] = 'running'
    auth_calls.clear()
    url = '/api/bots/dev/repository/workspace'
    context = http.get(url)
    assert context.headers['cache-control'] == 'no-store'
    assert context.json() == {'repository': 'alice/private', 'branch': command(root, 'branch', '--show-current'),
                              'base': 'main', 'review_available': True}
    review = http.get(url, params={'review': True})
    assert review.status_code == 200
    changes = {file['path']: file['diff'] for file in review.json()['changes']}
    assert set(changes) == {'README.md', 'extra.py', 'renamed file.md'}
    assert '-before' in changes['README.md'] and '+before' in changes['renamed file.md']
    assert '+print("new file")' in changes['extra.py']
    assert 'snapshot' not in review.json() and 'ignored private data' not in review.text
    assert str(root) not in review.text and command(root, 'status', '--porcelain') == git_status
    assert http.post('/api/bots/dev/repository/review').status_code == 409
    assert http.get(url, params={'review': True}, headers={'x-owner': 'bob'}).status_code == 409
    assert http.get(url, headers={'x-owner': 'anonymous'}).status_code == 401
    assert not auth_calls and not writes


def test_chat_workspace_previews_original_rift_and_waits_for_active_task(client, monkeypatch):
    http, _, rest = client
    bot, _, _, _, root, _ = rest
    bot['repo'] = 'https://github.com/alice/original.git'
    (root / 'README.md').write_text('Rift update\n')
    url = '/api/bots/dev/repository/workspace'
    review = http.get(url, params={'review': True}).json()
    assert review['repository'] == 'alice/original' and review['branch'] == 'main' and review['base'] is None
    assert '+Rift update' in review['changes'][0]['diff']
    import task_store
    store = RealTaskStore(str(root.parent / 'active-tasks.sqlite3'))
    store.submit(session_key='dev', task_text='edit source', executor_prefix='developer',
                 bot_id='dev', rift=bot['rift'], chat_id='alice', resolve_bot=False)
    monkeypatch.setattr(task_store, 'CloudTaskStore', lambda: store)
    assert http.get(url).json()['review_available'] is True
    assert http.get(url, params={'review': True}).status_code == 409


def test_large_chat_workspace_lists_files_and_limits_diffs_individually(client):
    http, _, rest = client
    bot, _, writes, _, _, auth_calls = rest
    assert http.post('/api/bots/dev/repository', json={'repository': 'alice/private'}).status_code == 200
    root = Path(bot['rift'])
    for index in range(4):
        (root / f'older-{index}.txt').write_text('earlier work\n' * 70_000)
    (root / 'large-file.txt').write_text('long change\n' * 100_000)
    command(root, 'add', '.')
    command(root, '-c', 'user.name=test', '-c', 'user.email=test@example.test', 'commit', '-m', 'Older work')
    smoke = 'kyrex-review-[smoke].txt'
    (root / smoke).write_text('Kyrex review works.\n')
    (root / 'over-preview-limit.txt').write_bytes(b'x' * (5 * 1024 * 1024 + 1))
    (root / '.env').write_text('ignored private data')
    bot['status'] = 'running'
    url = '/api/bots/dev/repository/workspace'
    auth_calls.clear()
    status = command(root, 'status', '--porcelain')

    response = http.get(url, params={'files_only': True})
    assert response.status_code == 200
    review = response.json()
    assert review['changes'][0] == {'path': smoke, 'error': None}
    assert 'snapshot' not in review and 'ignored private data' not in response.text
    assert len(review['changes']) == 7 and all('diff' not in file for file in review['changes'])
    assert next(file for file in review['changes'] if file['path'] == 'over-preview-limit.txt')['error']

    def diff(file):
        return http.get(url, params={'file': file, 'revision': review['revision']})

    assert diff(smoke).status_code == 200 and '+Kyrex review works.' in diff(smoke).json()['diff']
    assert diff('large-file.txt').json()['error'].startswith('This file’s diff is too large')
    assert diff('.env').status_code == 409
    assert diff('../source/README.md').status_code == 409
    assert http.get(url, params={'file': smoke, 'revision': review['revision']},
                    headers={'x-owner': 'bob'}).status_code == 409
    assert not auth_calls and not writes and command(root, 'status', '--porcelain') == status

    (root / smoke).write_text('New workspace version\n')
    assert 'workspace changed' in diff(smoke).json()['detail']


def test_roster_and_repository_api_agree_for_legacy_coding_bot(client, monkeypatch):
    http, _, rest = client
    bot = rest[0]
    bot['policy'] = {'*': 0, 'fs:write': 1, 'repo:pr': 1}
    monkeypatch.setattr(chat_api.chat_service, '_bot_rift_resolves', lambda bot: True)
    monkeypatch.setattr(chat_api.bot_provider, 'bot_provider_view', lambda owner, bot: {})
    view = chat_api._bot_public(bot, 'alice')
    assert view['role']['id'] != 'developer'
    assert view['repository_available'] is True
    assert http.get('/api/bots/dev/repository').status_code == 200
    assert chat_api._bot_public(bot, 'bob')['repository_available'] is False
    bot['policy']['repo:pr'] = 'deny'
    assert chat_api._bot_public(bot, 'alice')['repository_available'] is False
    assert http.get('/api/bots/dev/repository').status_code == 409


@pytest.mark.parametrize('endpoint,payload', [('/repository', {'repository': 'alice/private'}),
    ('/repository/review', None), ('/repository/refresh', None),
    ('/repository/publish', {'snapshot': 'a'*64, 'title': 'Title'})])
def test_owner_and_anonymous_requests_fail_before_git(client, endpoint, payload):
    http, _, rest = client
    for owner, status in [('bob', 409), ('anonymous', 401)]:
        response = http.post('/api/bots/dev' + endpoint, json=payload, headers={'x-owner': owner})
        assert response.status_code == status
    assert not rest[2] and not rest[3]


@pytest.mark.parametrize('payload', [{'path': '/etc'}, {'repository': 'alice/private', 'token': 'secret'}, [], {'repository': 3}])
def test_checkout_rejects_untrusted_paths_tokens_and_bad_shapes(client, payload):
    http, _, rest = client
    assert http.post('/api/bots/dev/repository', json=payload).status_code == 400
    assert not rest[3]


def test_refresh_endpoint_returns_only_safe_owner_scoped_state(client):
    http, _, rest = client
    assert http.post('/api/bots/dev/repository', json={'repository': 'alice/private'}).status_code == 200
    response = http.post('/api/bots/dev/repository/refresh')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['workspace']['status'] == 'current'
    assert 'test-secret-token' not in response.text and 'path' not in response.json()['workspace']
    rest[0]['status'] = 'running'
    assert http.post('/api/bots/dev/repository/refresh').status_code == 409


def test_workspace_switch_after_submission_fails_before_executor(workspace, monkeypatch, tmp_path):
    from task_store import CloudTaskStore, TaskWorker
    service, bot, *_ = workspace
    store = RealTaskStore(str(tmp_path / 'tasks.sqlite3'))
    bot['status'] = 'running'
    task_id = store.submit(session_key='dev', task_text='edit source', executor_prefix='developer',
                           bot_id='dev', rift=bot['rift'], chat_id='alice', resolve_bot=False)
    task = store.claim_next('worker-test')
    bot['rift'] = '/changed/checkout'
    calls = []
    worker = TaskWorker(store, worker_id='worker-test', executor=lambda **kwargs: calls.append(kwargs))
    worker.execute_task(task)
    assert store.status(task_id) == 'failed' and not calls
    assert 'workspace changed' in store.get(task_id)['error'].lower()


def test_writer_api_is_separate_from_existing_reader(setup, tmp_path, monkeypatch):
    reader, reader_connection, mode, _ = setup
    writer_connection = GitHubConnection(tmp_path / 'writer.json', reader.transport)
    writer = GitHubAppFlow(tmp_path / 'writer-app.json', reader.transport, reader.post, writer_connection, write=True)
    monkeypatch.setattr(connections_api, '_github_flow', lambda: reader)
    monkeypatch.setattr(connections_api, '_github', lambda: reader_connection)
    monkeypatch.setattr(connections_api, '_github_write_flow', lambda: writer)
    monkeypatch.setattr(connections_api, '_require_user', lambda request: request.headers.get('x-owner', 'alice'))
    monkeypatch.setattr(connections_api, '_github_origin', lambda request: BASE)
    app = FastAPI(); app.include_router(connections_api.router); http = TestClient(app, base_url=BASE)
    mode['permissions'] = writer.permissions
    url = http.post('/api/connections/github-write/connect').json()['authorization_url']
    assert '/github-write/setup' in url
    assert 'Continue on GitHub' in http.get(url).text
    registered = http.get('/api/connections/github-write/registered', params={'state': state(url), 'code': 'reg'}, follow_redirects=False)
    installed = http.get('/api/connections/github-write/installed', params={'state': state(registered.headers['location']), 'installation_id': '7'}, follow_redirects=False)
    assert 'GitHub connected' in http.get('/api/connections/github-write/callback', params={'state': state(installed.headers['location']), 'code': 'auth'}).text
    assert writer_connection.view('alice')['connected'] and not reader_connection.view('alice')['connected']
    reader_connection.save_app_connection('alice', {'access_token': 'ghu_reader'}, 8, 13, ['alice/private'])
    http.post('/api/connections/github-write/disconnect')
    assert reader_connection.view('alice')['connected'] and not writer_connection.view('alice')['connected']
