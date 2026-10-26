import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import chat_api
import connections_api
import bots
from task_store import CloudTaskStore as RealTaskStore
from github_app import GitHubAppFlow
from github_connection import GitHubConnection
from test_github_app import setup, BASE, state
from test_developer_repositories import workspace


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
    from pathlib import Path
    (Path(bot['rift']) / 'README.md').write_text('API update\n')
    review = http.post('/api/bots/dev/repository/review').json()
    assert review['files'] == ['README.md'] and 'path' not in review
    published = http.post('/api/bots/dev/repository/publish', json={'snapshot': review['snapshot'], 'title': 'API update'})
    assert published.status_code == 200 and published.json()['url'].endswith('/pull/1')
    assert 'test-secret-token' not in published.text + response.text + str(review)


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
