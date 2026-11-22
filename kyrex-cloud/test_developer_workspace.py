"""Workspace turns preserve diverged and dirty Git state, with no publishing."""
import argparse
import io
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import git_workflow
import delegation
import serve


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def test_workspace_question_and_edit_preserve_existing_work(tmp_path, monkeypatch):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    (tmp_path / 'code.py').write_text('original\n')
    git(tmp_path, 'add', 'code.py')
    git(tmp_path, 'commit', '-qm', 'initial')
    git(tmp_path, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    (tmp_path / 'previous.py').write_text('previous local work\n')
    git(tmp_path, 'add', 'previous.py')
    git(tmp_path, 'commit', '-qm', 'local work')
    remote_head = git(tmp_path, 'commit-tree', 'HEAD^{tree}', '-p',
                      'refs/remotes/origin/main', '-m', 'independent remote work').strip()
    git(tmp_path, 'update-ref', 'refs/remotes/origin/main', remote_head)
    assert git(tmp_path, 'merge-base', 'HEAD', 'origin/main').strip() not in (
        git(tmp_path, 'rev-parse', 'HEAD').strip(), remote_head)
    # An unreachable remote must not block conversational turns.
    git(tmp_path, 'remote', 'add', 'origin', str(tmp_path / 'missing-remote'))
    (tmp_path / 'previous.py').write_text('previous staged work\n')
    git(tmp_path, 'add', 'previous.py')
    (tmp_path / 'notes.txt').write_text('untracked notes')
    head = git(tmp_path, 'rev-parse', 'HEAD')
    branch = git(tmp_path, 'branch', '--show-current')
    index = git(tmp_path, 'diff', '--cached', '--binary')
    monkeypatch.setenv('KYREX_CHAT_SYSTEM_PROMPT', 'Original Bot identity')
    monkeypatch.setenv('KYREX_SESSION_DIR', '/tmp/test-conversation')
    args = argparse.Namespace(rift=str(tmp_path), read_only=False, task='Can you edit?',
                              python='python3', startup_timeout=1, idle_timeout=1,
                              overall_timeout=1)

    class Agent:
        chat_done_seen = True
        outcome = 'answered'
        terminal = True
        execution_error = False
        final_response = 'Yes, I can inspect and edit this workspace.'
        approvals = []
        tool_calls = []
        errors = []

        def __init__(self, bridge, root, **kwargs):
            assert root == tmp_path
            assert kwargs['read_only'] is True
            assert kwargs['host_delete'] is None
            assert os.environ['KYREX_CHAT_SYSTEM_PROMPT'].startswith('Original Bot identity')
            assert 'Preserve existing changes' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'Make the final answer stand alone' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'Skip tool-by-tool narration' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'Repository freshness:' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'timeout_seconds=120' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'next_char_offset' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'unverified' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'read_only flag does not mean your local checkout is read-only' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'Do not substitute a patch bundle unless the owner requests one' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert os.environ['KYREX_SESSION_DIR'] == '/tmp/test-conversation'

        def start(self, task):
            return True

        def run(self):
            if args.task == 'edit code.py':
                self.final_response = 'The checkout needs to be refreshed first.'

    with patch.object(git_workflow, 'HeadlessAgent', Agent), \
         patch.object(git_workflow, 'prepare_workspace', side_effect=AssertionError('no sync')), \
         patch.object(git_workflow, 'commit_and_push', side_effect=AssertionError('no push')), \
         patch.object(git_workflow, 'open_pull_request', side_effect=AssertionError('no PR')):
        result = git_workflow.run_workspace_agent(args, 'bridge', lambda msg: None)
        assert result['status'] == 'no_changes'
        assert 'read-only' in serve.format_result(result) and result['final_response'] == Agent.final_response
        args.task = 'edit code.py'
        result = git_workflow.run_workspace_agent(args, 'bridge', lambda msg: None)
        assert result['status'] == 'no_changes' and not result['has_changes']
        assert (tmp_path / 'code.py').read_text() == 'original\n'
    assert git(tmp_path, 'rev-parse', 'HEAD') == head
    assert git(tmp_path, 'branch', '--show-current') == branch
    assert git(tmp_path, 'diff', '--cached', '--binary') == index
    assert (tmp_path / 'previous.py').read_text() == 'previous staged work\n'
    assert (tmp_path / 'notes.txt').read_text() == 'untracked notes'
    assert os.environ['KYREX_CHAT_SYSTEM_PROMPT'] == 'Original Bot identity'

    Agent.final_response = ''
    args.task = 'hello'
    with patch.object(git_workflow, 'HeadlessAgent', Agent):
        result = git_workflow.run_workspace_agent(args, 'bridge', lambda msg: None)
    assert result['status'] == 'agent_failed'
    assert result['errors'] == ['Developer Bot did not return an answer']


def test_workspace_requires_checkout_and_answer(tmp_path):
    args = argparse.Namespace(rift=str(tmp_path), read_only=False, task='hello')
    result = git_workflow.run_workspace_agent(args, 'bridge', lambda msg: None)
    assert result['status'] == 'error'
    assert 'existing Git checkout' in result['errors'][0]


def test_managed_workspace_uses_host_freshness_without_anonymous_refetch(tmp_path, monkeypatch):
    import json
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    git(tmp_path, 'commit', '--allow-empty', '-qm', 'initial')
    head = git(tmp_path, 'rev-parse', 'HEAD').strip()
    state = {'status': 'updated', 'head': head, 'remote_head': head, 'base': 'develop'}
    monkeypatch.setenv('KYREX_CONNECTED_REPOSITORY', 'alice/private')
    monkeypatch.setenv('KYREX_WORKSPACE_FRESHNESS', json.dumps(state))
    monkeypatch.setattr(git_workflow, 'refresh_workspace_agent', lambda *a: (_ for _ in ()).throw(
        AssertionError('Managed workspace performed an anonymous fetch')))
    class Agent:
        chat_done_seen = True; outcome = 'answered'; terminal = True; execution_error = False
        final_response = 'The selected repository is current.'
        approvals = []; tool_calls = []; errors = []
        def __init__(self, *a, **k):
            assert '"base": "develop"' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
        def start(self, task): return True
        def run(self): pass
    monkeypatch.setattr(git_workflow, 'HeadlessAgent', Agent)
    args = argparse.Namespace(rift=str(tmp_path), read_only=False, task='Check current code',
        python='python3', startup_timeout=1, idle_timeout=1, overall_timeout=1)
    result = git_workflow.run_workspace_agent(args, 'bridge', lambda msg: None)
    assert result['status'] == 'no_changes' and result['workspace_freshness'] == state
    monkeypatch.setenv('KYREX_WORKSPACE_FRESHNESS', '{}')
    result = git_workflow.run_workspace_agent(args, 'bridge', lambda msg: None)
    assert result['status'] == 'error' and 'freshness is unavailable' in result['errors'][0]


def test_delegated_developer_defaults_to_workspace():
    target = {'id': 'dev', 'policy': {'fs:write': 1}}
    assert delegation._resolve_delegated_route('repo', target, 'Can you edit?') == (
        'developer', 'Can you edit?')
    assert delegation._resolve_delegated_route('repo', target, 'repo: fix and open a PR') == (
        'repo', 'fix and open a PR')
    assert delegation._resolve_delegated_route('repo', {'id': 'reader', 'policy': {}}, 'hello') == (
        'repo', 'hello')


def test_developer_commentary_is_bounded_and_pretool_only():
    events = []
    relay = git_workflow.developer_progress(events.append)
    relay({'type': 'reasoning', 'content': 'private reasoning'})
    relay({'type': 'token', 'content': 'Found the bug. '})
    relay({'type': 'token', 'content': 'I am fixing it now.'})
    relay({'type': 'tool_start', 'name': 'edit_file'})
    notes = [e for e in events if e['type'] == 'commentary']
    assert notes == [{'type': 'commentary', 'content': 'Found the bug. I am fixing it now.'}]
    relay({'type': 'token', 'content': 'Final answer only.'})
    relay({'type': 'tool_start', 'name': 'task_complete'})
    relay({'type': 'chat_done'})
    relay({'type': 'tool_start', 'name': 'read_local_file'})
    assert len([e for e in events if e['type'] == 'commentary']) == 1
    for _ in range(20):
        relay({'type':'token', 'content':'x'*3000})
        relay({'type':'tool_start', 'name':'search'})
    notes = [e for e in events if e['type'] == 'commentary']
    assert len(notes) == 2, 'Repeated commentary is coalesced, not replayed at each tool'
    assert all(len(e['content']) <= 240 for e in notes)
    assert 'private reasoning' not in str(notes)


def test_progress_distinguishes_recovery_without_exposing_tool_payloads():
    from developer_updates import tool_stage
    for kind, expected in [('command_timeout', 'time limit'), ('file_not_found', 'not found'),
                           ('invalid_arguments', 'arguments'), ('terminal_confirmation', 'terminal confirmation')]:
        stage = tool_stage({'type': 'tool_result', 'name': 'run_command',
            'result': {'error_type': kind, 'error': 'SECRET /private/token.txt', 'output': 'PRIVATE'}})
        assert expected in stage
        assert 'SECRET' not in stage and 'PRIVATE' not in stage and '/private' not in stage
    assert tool_stage({'type':'tool_result','name':'read_local_file',
        'result':{'status':'ok','truncated':True,'content':'PRIVATE'}}) == 'Read part of a file; more content is available.'
    assert tool_stage({'type':'tool_start','name':'list_local_files'}) == 'Inspecting the relevant files…'


def test_headless_marks_its_engine_environment_without_disabling_edit_protocol(tmp_path, monkeypatch):
    import headless_agent
    import threading
    from types import SimpleNamespace
    captured = []
    agent = headless_agent.HeadlessAgent(tmp_path / 'bridge.py', tmp_path)
    agent.out_q.put(('stdout', '{"type":"phase","value":"IDLE"}'))
    monkeypatch.setattr(headless_agent.subprocess, 'Popen', lambda *a, **k: captured.append(k['env']) or SimpleNamespace())
    monkeypatch.setattr(threading.Thread, 'start', lambda *a: None)
    monkeypatch.setattr(agent, '_send', lambda *a: None)
    assert agent.start('Fix the parser')
    assert captured[0]['KYREX_HEADLESS'] == '1'
    assert captured[0]['KYREX_VSCODE'] == '1'
    assert captured[0]['WORKSPACE_ROOT'] == str(tmp_path)


@pytest.mark.parametrize('decision', ['APPROVED', 'DENIED', 'CHANGED'])
def test_tracked_removal_requires_approval_for_exact_unchanged_files(tmp_path, monkeypatch, capsys, decision):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    for name in ('obsolete.js', 'old config.js', 'keep.js'):
        (tmp_path / name).write_text(name + '\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'Tracked files')
    head = git(tmp_path, 'rev-parse', 'HEAD').strip()
    paths = ['obsolete.js', 'old config.js']

    def answer():
        assert all((tmp_path / path).exists() for path in paths)
        if decision == 'CHANGED':
            (tmp_path / paths[0]).write_text('owner edit\n')
        return ('APPROVED' if decision == 'CHANGED' else decision) + '\n'

    monkeypatch.setattr(git_workflow.sys, 'stdin', SimpleNamespace(readline=answer))
    frame = {'id': 'test', 'paths': paths}
    result = git_workflow.tracked_delete(tmp_path, head, frame)
    assert capsys.readouterr().out.startswith('KYREX_APPROVAL:')
    assert result['approved'] is (decision == 'APPROVED')
    assert all((tmp_path / path).exists() == (decision != 'APPROVED') for path in paths)
    assert (tmp_path / 'keep.js').exists()
    assert git(tmp_path, 'rev-parse', 'HEAD').strip() == head
    if decision == 'APPROVED':
        assert result['result']['removed'] == paths
        assert sorted(git(tmp_path, 'diff', '--cached', '--name-status').splitlines()) == [
            'D\tobsolete.js', 'D\told config.js']
    else:
        assert not git(tmp_path, 'diff', '--cached', '--name-status')


def test_tracked_removal_rejects_private_paths_and_preserves_local_edits(tmp_path, capsys):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    (tmp_path / 'local.js').write_text('original')
    (tmp_path / 'link').symlink_to('local.js')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'Tracked files')
    head = git(tmp_path, 'rev-parse', 'HEAD').strip()
    (tmp_path / 'local.js').write_text('unsaved edit')
    (tmp_path / 'untracked.js').write_text('owner file')
    for path in ('../other.js', '.git/config', 'link', 'untracked.js', 'local.js', ':(glob)*'):
        assert not git_workflow.tracked_delete(tmp_path, head, {'id': 'test', 'paths': [path]})['approved']
    assert capsys.readouterr().out == ''
    assert (tmp_path / 'local.js').read_text() == 'unsaved edit'


def test_tracked_removal_respects_owner_binding_and_explicit_delete_denies(monkeypatch):
    ctx = SimpleNamespace(bot_id='dev', bot_owner='alice', rift_path='/work/alice')
    bot = {'owner': 'alice', 'rift': ctx.rift_path, 'policy': {'fs:write': 1}, 'status': 'running'}
    monkeypatch.setattr(serve.bots, 'get_bot', lambda bot_id: bot)
    assert serve.developer_removal_review_allowed(ctx)
    bot['policy']['fs:delete'] = 'deny'
    assert not serve.developer_removal_review_allowed(ctx)
    del bot['policy']['fs:delete']
    bot['rift'] = '/different'
    assert not serve.developer_removal_review_allowed(ctx)


def test_developer_removal_uses_the_existing_owner_approval_round_trip(tmp_path, monkeypatch):
    git(tmp_path, 'init', '-q')
    bot = {'id': 'dev', 'owner': 'alice', 'status': 'running', 'rift': str(tmp_path),
           'policy': {'fs:read': 0, 'fs:write': 1, 'repo:read': 0, 'repo:pr': 1}}
    monkeypatch.setattr(serve.bots, 'load_bots', lambda: {'dev': bot})
    monkeypatch.setattr(serve.audit, 'log', lambda **kwargs: None)
    approval = {'op': 'repo.remove_tracked', 'summary': 'Remove tracked files from Developer Bot',
                'detail': 'Revision 123456789abc\n• obsolete.js\n• old config.js'}
    proc = SimpleNamespace(stdin=io.StringIO(), stderr=[], returncode=0, wait=lambda *a, **k: 0)

    def launch(_, *, env, **kwargs):
        assert env['KYREX_DEVELOPER_DELETE_BRIDGE'] == '1'
        proc.stdout = iter(['KYREX_APPROVAL:' + json.dumps(approval),
                            'KYREX_RESULT_JSON:{"status":"completed","mode":"developer","final_response":"Done"}'])
        return proc

    def review(message_id, tier, token, summary, detail):
        assert tier == 2 and token and detail == approval['detail']
        assert not serve.handle_approval_reply('bob', token, message_id, 'dev')
        assert serve.handle_approval_reply('alice', token, message_id, 'dev')

    monkeypatch.setattr(serve.subprocess, 'Popen', launch)
    monkeypatch.setattr(serve, 'APPROVAL_TIMEOUT', 2)
    lock = serve.session_lock('dev')
    lock.acquire()
    try:
        serve.run_task(chat_id='alice', session_key='dev', repo_url=None,
                       executor_prefix='developer', task_text='Remove old files',
                       send=lambda *_: 'review-1', edit=lambda *_: None, on_approval=review)
    finally:
        if lock.locked():
            lock.release()
    assert proc.stdin.getvalue() == 'APPROVED\n'
