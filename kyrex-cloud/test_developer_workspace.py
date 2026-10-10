"""Workspace turns preserve diverged and dirty Git state, with no publishing."""
import argparse
import os
import subprocess
from unittest.mock import patch

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
            assert os.environ['KYREX_CHAT_SYSTEM_PROMPT'].startswith('Original Bot identity')
            assert 'Preserve existing changes' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'normally under 100 words' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'Give brief updates before tools' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'Repository freshness:' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'timeout_seconds=120' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'next_char_offset' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'unverified' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert os.environ['KYREX_SESSION_DIR'] == '/tmp/test-conversation'

        def start(self, task):
            return True

        def run(self):
            if args.task == 'edit code.py':
                (tmp_path / 'code.py').write_text('requested change\n')
                self.final_response = 'Updated code.py.'

    with patch.object(git_workflow, 'HeadlessAgent', Agent), \
         patch.object(git_workflow, 'prepare_workspace', side_effect=AssertionError('no sync')), \
         patch.object(git_workflow, 'commit_and_push', side_effect=AssertionError('no push')), \
         patch.object(git_workflow, 'open_pull_request', side_effect=AssertionError('no PR')):
        result = git_workflow.run_workspace_agent(args, 'bridge', lambda msg: None)
        assert result['status'] == 'no_changes'
        assert serve.format_result(result) == Agent.final_response
        args.task = 'edit code.py'
        result = git_workflow.run_workspace_agent(args, 'bridge', lambda msg: None)
        assert result['status'] == 'completed'
        assert result['final_response'] == 'Updated code.py.'
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
