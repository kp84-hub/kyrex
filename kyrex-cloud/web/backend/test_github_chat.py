import json
import pytest
import bot_capabilities
import chat_service
import connections_api
import github_connection
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_policies_grant_repository_reads_only():
    for policy in (chat_service.serve.coordinator_preset_policy(), chat_service.serve.developer_preset_policy()):
        assert 'github_read' in bot_capabilities.derive_bot_capabilities(policy)['tools']
    assert 'github_read' not in bot_capabilities.derive_bot_capabilities({'fs:read': 0})['tools']
    assert 'github_read' not in bot_capabilities.derive_bot_capabilities({'repo:read': 'deny', '*': 0})['tools']


def test_bridge_derives_owner_and_refuses_denied_session(monkeypatch):
    calls = []
    class Connection:
        def read(self, owner, action, **args):
            calls.append((owner, action, args))
            return {'content': 'private text'}
    monkeypatch.setattr(github_connection, 'GitHubConnection', Connection)
    session = object.__new__(chat_service.EngineSession)
    session.github_owner = 'alice'
    session.allowed_tools = {'github_read'}
    frame = {'action': 'contents', 'repository': 'owner/repo', 'path': 'README.md', 'owner': 'bob'}
    ok, result = session._handle_github_read(frame)
    assert ok and result['content'] == 'private text'
    assert calls[0][0] == 'alice'
    session.allowed_tools = set()
    assert not session._handle_github_read(frame)[0]
    assert len(calls) == 1


def test_developer_read_selection_preserves_mutations():
    assert chat_service.github_read_turn('Can you look at private repos I own on GitHub?')
    assert chat_service.github_read_turn('Read the README in my GitHub repo')
    assert not chat_service.github_read_turn('Read GitHub README and fix the code')
    assert not chat_service.github_read_turn('Merge my GitHub pull request')
    assert not chat_service.github_read_turn('Read my local README')


def test_owner_authenticated_connection_routes(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET', 'github-api-test-secret')
    c = github_connection.GitHubConnection(tmp_path / 'github.json')
    calls = []
    class Flow:
        def begin(self, owner, base):
            calls.append(owner)
            return {'authorization_url': base + '/api/connections/github/setup?state=safe'}
        def cancel(self, owner): calls.append('cancel:' + owner)
    monkeypatch.setattr(connections_api, '_github', lambda: c)
    monkeypatch.setattr(connections_api, '_github_flow', Flow)
    monkeypatch.setattr(connections_api, '_github_origin', lambda request: 'https://chat.kyrex.test')
    monkeypatch.setattr(connections_api, '_require_user', lambda req: req.headers.get('x-owner', 'alice'))
    app = FastAPI(); app.include_router(connections_api.router)
    client = TestClient(app)
    response = client.post('/api/connections/github/connect')
    assert response.status_code == 200 and response.json()['authorization_url']
    assert calls == ['alice']
    c.save_app_connection('alice', {'access_token': 'ghu_TEST'}, 7, 12, ['alice/private'])
    client.post('/api/connections/github/disconnect', headers={'x-owner': 'bob'})
    assert c.view('alice')['connected']
    client.post('/api/connections/github/disconnect')
    assert not c.view('alice')['connected']
    assert calls[-2:] == ['cancel:bob', 'cancel:alice']


def test_engine_host_protocol_github_read(monkeypatch):
    import queue
    import threading
    from unittest.mock import MagicMock
    class Connection:
        def read(self, owner, action, **kwargs):
            assert owner == 'alice'
            return {'repositories': ['alice/private']}
    monkeypatch.setattr(github_connection, 'GitHubConnection', Connection)
    session = object.__new__(chat_service.EngineSession)
    session._closed = False
    session._turn_lock = threading.Lock()
    session._stderr_lock = threading.Lock()
    session.stderr_tail = []
    session._proc = MagicMock(); session._proc.poll.return_value = None
    session.surface_context = None
    session.github_owner = 'alice'; session.allowed_tools = {'github_read'}
    session.delegation_ctx = None
    session.denied_requests = []
    session._frames = queue.Queue()
    session._frames.put({'type': 'confirm_request', 'id': 'g1', 'value': 'github_read', 'action': 'repositories'})
    session._frames.put({'type': 'chat_done', 'content': 'One private repository'})
    session._frames.put({'type': 'phase', 'value': 'IDLE'})
    sent = []; session._send = sent.append
    assert session.run_turn('List my GitHub repos', lambda t: None) == ('One private repository', None)
    assert sent[1] == {'type': 'confirm_response', 'id': 'g1', 'approved': True, 'result': {'repositories': ['alice/private']}}
