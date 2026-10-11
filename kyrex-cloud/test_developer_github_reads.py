"""Developer GitHub reads cross the real host/runner/engine protocol with data."""
import base64
import io
import json
from pathlib import Path
import sys

import pytest
import bots
import github_connection
import git_workflow
import headless_agent
import serve


@pytest.fixture
def bound(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET', 'developer-github-test-key')
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(bots, 'BOTS_FILE', str(tmp_path / 'bots.json'))
    root = tmp_path / 'rift'; root.mkdir()
    bots.add_bot('dev', 'Developer', 'test:model', str(root),
                 policy=serve.developer_preset_policy(), status='running', owner='alice')
    return serve.build_context('dev', 'developer'), root


@pytest.fixture
def connection(bound, monkeypatch):
    calls = []
    token = 'github_pat_' + 'A' * 40
    text = 'MODEL = "cortex-test-model"\n'
    def transport(credential, path, params=None):
        assert credential == token
        calls.append((path, params))
        if path in ('/repos/owner/private-app', '/repos/owner/empty'):
            return {'full_name': path.removeprefix('/repos/')}
        if path.endswith('/settings.py'):
            return {'type': 'file', 'encoding': 'base64', 'size': len(text),
                    'sha': 'abc', 'content': base64.b64encode(text.encode()).decode()}
        return [] if '/empty/' in path else [{'path': 'settings.py', 'type': 'file', 'size': len(text)}]
    c = github_connection.GitHubConnection(transport=transport)
    c.connect('alice', token, ['owner/private-app', 'owner/empty'])
    monkeypatch.setattr(github_connection, 'GitHubConnection', lambda: c)
    return c, calls, token


def request(action='contents', **args):
    return {'type': 'confirm_request', 'id': 'read-1', 'value': 'github_read',
            'action': action, 'repository': 'owner/private-app', 'path': 'settings.py',
            'ref': 'feature/test', **args}


def test_parent_reads_selected_private_source_using_bound_owner(bound, connection):
    ctx, _ = bound
    c, calls, token = connection
    reply = serve._developer_github_read(ctx, 'developer', request(owner='bob'))
    assert reply['approved'] and reply['result']['content'] == 'MODEL = "cortex-test-model"\n'
    assert calls[-1] == ('/repos/owner/private-app/contents/settings.py', {'ref': 'feature/test'})
    assert token not in json.dumps(reply) and 'sealed' not in json.dumps(reply)
    assert reply['result']['untrusted_repository_content'] is True
    denied = serve._developer_github_read(ctx, 'developer', request(repository='owner/unselected'))
    assert not denied['approved'] and 'not selected' in denied['result']['error']


@pytest.mark.parametrize('fault', ['unbound', 'other-owner', 'stopped', 'revoked', 'wrong-executor', 'wrong-tool'])
def test_parent_rechecks_authority_before_any_read(bound, connection, fault):
    ctx, _ = bound
    _, calls, _ = connection
    prefix = 'developer'; frame = request()
    if fault == 'unbound': ctx.bot_owner = ''
    elif fault == 'other-owner': ctx.bot_owner = 'bob'
    elif fault == 'stopped': bots.set_status('dev', 'stopped')
    elif fault == 'revoked': bots.update_bot('dev', policy={'fs:write': 1, 'repo:read': 'deny'})
    elif fault == 'wrong-executor': prefix = 'repo'
    else: frame['value'] = 'fitness_profile'
    before = len(calls)
    reply = serve._developer_github_read(ctx, prefix, frame)
    assert not reply['approved'] and reply['result']['error']
    assert len(calls) == before


@pytest.mark.parametrize('args', [{'action': 'push'}, {'path': '../secret'}, {'path': None}, {'ref': 'main\n'}])
def test_invalid_reads_fail_without_remote_operations(bound, connection, args):
    ctx, _ = bound; _, calls, _ = connection
    before = len(calls)
    reply = serve._developer_github_read(ctx, 'developer', request(**args))
    assert not reply['approved'] and reply['result']['error']
    assert len(calls) == before


def test_disconnect_and_transport_errors_remain_explanations(bound, connection):
    ctx, _ = bound; c, _, token = connection
    def fail(*args): raise github_connection.GitHubError('GitHub denied access or rate limited the request.')
    c.transport = fail
    reply = serve._developer_github_read(ctx, 'developer', request())
    assert not reply['approved'] and 'denied access' in reply['result']['error']
    c.disconnect('alice')
    reply = serve._developer_github_read(ctx, 'developer', request())
    assert not reply['approved'] and 'Connect GitHub' in reply['result']['error']
    assert token not in json.dumps(reply)


def test_parent_uses_live_github_app_selection(bound, connection, monkeypatch):
    from github_app import GitHubAppFlow
    ctx, _ = bound; c, _, token = connection
    c.save_app_connection('alice', {'access_token': 'ghu_test'}, 7, 9, ['owner/stale'])
    calls = []
    def credentials(self, owner, live):
        calls.append((owner, live['installation_id']))
        return token, ['owner/private-app']
    monkeypatch.setattr(GitHubAppFlow, 'credentials', credentials)
    reply = serve._developer_github_read(ctx, 'developer', request('repositories'))
    assert reply['result']['repositories'] == ['owner/private-app']
    assert serve._developer_github_read(ctx, 'developer', request())['approved']
    assert not serve._developer_github_read(ctx, 'developer', request(repository='owner/stale'))['approved']
    assert calls == [('alice', 7)] * 3


def test_runner_never_auto_approves_host_tools_without_payload():
    agent = headless_agent.HeadlessAgent(Path('bridge'), Path('repo'))
    sent = []; agent._send = sent.append
    for value in headless_agent.HOST_TOOL_GATES:
        assert not headless_agent.auto_approve_gate(value)
        agent._confirm(request(value=value))
        assert not sent[-1]['approved'] and sent[-1]['result']['error']
    agent._confirm(request(value='edit'))
    assert sent[-1]['approved']
    agent._confirm(request(value='deletion'))
    assert not sent[-1]['approved']


@pytest.mark.parametrize('payload', [None, {}, {'type': 'confirm_response', 'id': 'wrong', 'approved': True, 'result': {'content': 'x'}},
    {'type': 'confirm_response', 'id': 'read-1', 'approved': True, 'result': {}}])
def test_runner_missing_or_mismatched_replies_are_errors(payload):
    agent = headless_agent.HeadlessAgent(Path('bridge'), Path('repo'), host_read=lambda _: payload)
    sent = []; agent._send = sent.append
    agent._confirm(request())
    assert not sent[-1]['approved'] and sent[-1]['result']['error']


def test_workflow_bridge_correlates_requests_and_does_not_read_local_credentials(monkeypatch, capsys):
    monkeypatch.delenv('KYREX_GITHUB_HOST_BRIDGE', raising=False)
    assert not git_workflow.github_host_read(request())['approved']
    assert not capsys.readouterr().out
    monkeypatch.setenv('KYREX_GITHUB_HOST_BRIDGE', '1')
    reply = {'type': 'confirm_response', 'id': 'read-1', 'approved': True, 'result': {'content': 'source'}}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(reply) + '\n'))
    assert git_workflow.github_host_read(request(owner='bob')) == reply
    emitted = json.loads(capsys.readouterr().out.split(':', 1)[1])
    assert 'owner' not in emitted and emitted['repository'] == 'owner/private-app'
    monkeypatch.setattr(sys, 'stdin', io.StringIO('{"id":"other"}\n'))
    assert not git_workflow.github_host_read(request())['approved']


def test_real_subprocess_chain_returns_status_roots_and_source(bound, connection, tmp_path, monkeypatch):
    """serve -> git runner -> HeadlessAgent -> engine, and back through all pipes."""
    _, root = bound
    _, _, token = connection
    cloud = Path(serve.__file__).parent
    bridge = tmp_path / 'engine.py'
    bridge.write_text('''import json, sys
print(json.dumps({'type':'phase','value':'IDLE'}), flush=True)
json.loads(sys.stdin.readline())
answers = []
for n, (action, repo, path, ref) in enumerate([
    ('status','','',''), ('repositories','','',''),
    ('contents','owner/private-app','',''),
    ('contents','owner/private-app','settings.py','feature/test'),
    ('contents','owner/empty','','')]):
    print(json.dumps({'type':'confirm_request','id':str(n),'value':'github_read',
        'action':action,'repository':repo,'path':path,'ref':ref}), flush=True)
    response = json.loads(sys.stdin.readline())
    assert response['approved'], response
    answers.append(response['result'])
print(json.dumps({'type':'chat_done','content':json.dumps(answers),
    'outcome':'answered','terminal':True}), flush=True)
sys.stdin.readline()
''')
    runner = tmp_path / 'runner.py'
    runner.write_text(f'''import json, sys
from pathlib import Path
sys.path.insert(0, {str(cloud)!r})
from headless_agent import HeadlessAgent
from git_workflow import github_host_read
agent = HeadlessAgent(Path({str(bridge)!r}), Path({str(root)!r}), python=sys.executable,
    startup_timeout=3, idle_timeout=3, overall_timeout=8,
    surface='Kyrex Chat', host_read=github_host_read)
assert agent.start('Inspect the other project')
agent.run()
assert not agent.errors, agent.errors
print('KYREX_RESULT_JSON:' + json.dumps({{'mode':'developer','status':'no_changes',
    'final_response':agent.final_response}}), flush=True)
''')
    monkeypatch.setitem(serve.EXECUTORS, 'developer', str(runner))
    monkeypatch.setattr(serve, 'TASK_TIMEOUT', 15)
    results = []
    serve.run_task('alice', None, 'Inspect the other project', executor_prefix='developer',
        session_key='dev', send=lambda *a: 1, edit=lambda *a: None, on_result=results.append)
    assert len(results) == 1
    answers = json.loads(results[0]['final_response'])
    assert answers[0]['connected'] is True
    assert answers[1]['repositories'] == ['owner/empty', 'owner/private-app']
    assert answers[2]['entries'][0]['path'] == 'settings.py'
    assert 'cortex-test-model' in answers[3]['content']
    assert answers[4]['entries'] == [], 'An empty repository is valid data, unlike a missing payload'
    assert token not in json.dumps(results) and not list(root.iterdir())
