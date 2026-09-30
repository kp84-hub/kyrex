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
        final_response = 'Yes, I can inspect and edit this workspace.'
        approvals = []
        tool_calls = []
        errors = []

        def __init__(self, bridge, root, **kwargs):
            assert root == tmp_path
            assert os.environ['KYREX_CHAT_SYSTEM_PROMPT'].startswith('Original Bot identity')
            assert 'Preserve existing changes' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
            assert 'Repository freshness:' in os.environ['KYREX_CHAT_SYSTEM_PROMPT']
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
