import asyncio

import pytest

import bots
import chat_service
import developer_repositories
from developer_repositories import RepositoryWorkspaces, git
from github_connection import GitHubError
import rift_sandbox
import serve
import task_store


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    root = tmp_path / 'rift'
    root.mkdir()
    git(root, 'init', '-q')
    (root / '.gitignore').write_text('.env\n')
    (root / 'before.txt').write_text('base\n')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'base')
    base = git(root, 'rev-parse', 'HEAD').decode().strip()
    bot = {'id': 'dev', 'owner': 'alice', 'rift': str(root), 'status': 'running',
           'policy': serve.developer_preset_policy()}
    monkeypatch.setenv('KYREX_DATA_DIR', str(data))
    monkeypatch.setenv('KYREX_RIFT_SANDBOX_OWNER', 'alice')
    monkeypatch.setenv('KYREX_RIFT_SANDBOX_ENVIRONMENT_ID', 'pilot-environment')
    monkeypatch.setenv('KYREX_RIFT_SANDBOX_TOKEN', 'test-project-token')
    monkeypatch.setenv('RAILWAY_ENVIRONMENT_ID', 'production-environment')
    monkeypatch.setattr(task_store, 'DATA_DIR', data)
    monkeypatch.setattr(bots, 'load_bots', lambda: {'dev': bot.copy()})
    workspaces = RepositoryWorkspaces(data, auth=lambda owner, repo: ('reader-credential', repo))
    with workspaces.db() as db:
        db.execute('INSERT INTO workspaces VALUES (?,?,?,?,?,?)',
                   ('alice', 'dev', rift_sandbox.REPOSITORY, str(root), 'main', base))
    monkeypatch.setattr(rift_sandbox, 'RepositoryWorkspaces', lambda: workspaces)
    return data, root, base, bot, workspaces


def test_request_is_explicit():
    assert rift_sandbox.requested('Run these tests in a Railway Sandbox.')
    assert rift_sandbox.requested('Can you run the tests in a Railway Sandbox?')
    assert rift_sandbox.requested('/sandbox test')
    assert not rift_sandbox.requested('Run the tests')
    assert not rift_sandbox.requested('What would happen if I run these tests in a Railway Sandbox?')
    assert not rift_sandbox.requested('Run tests in a Railway Sandbox and publish the PR')


def test_snapshot_rebuilds_exact_tree_without_editing_local_index(pilot, tmp_path):
    _, root, base, bot, workspaces = pilot
    replica = tmp_path / 'fresh'
    git(None, 'clone', '-q', '--no-local', str(root), str(replica))
    (root / 'added.bin').write_bytes(b'\x00\xff\x00')
    (root / 'before.txt').unlink()
    (root / '.env').write_text('LOCAL_FIXTURE=1')
    index = git(root, 'write-tree')
    snapshot_base, tree, patch = rift_sandbox._snapshot(workspaces, workspaces.current('alice', bot))
    assert snapshot_base == base
    git(replica, 'apply', '--index', '--binary', '-', data=patch)
    assert git(replica, 'write-tree').decode().strip() == tree
    assert not (replica / '.env').exists()
    assert git(root, 'write-tree') == index

    (root / 'notes.txt').write_text('test-project-token\n')
    with pytest.raises(rift_sandbox.SandboxError, match='sensitive change'):
        rift_sandbox._snapshot(workspaces, workspaces.current('alice', bot))


def test_large_rift_patch_can_run_without_raising_pr_review_limit(pilot, tmp_path, monkeypatch):
    _, root, base, bot, workspaces = pilot
    replica = tmp_path / 'fresh'
    git(None, 'clone', '-q', '--no-local', str(root), str(replica))
    (root / 'fixture.txt').write_bytes(b'Ordinary snapshot fixture data.\n' * 125_000)
    current = workspaces.current('alice', bot)

    snapshot_base, tree, patch = rift_sandbox._snapshot(workspaces, current)
    assert snapshot_base == base
    assert len(patch) > 3_962_465
    git(replica, 'apply', '--index', '--binary', '-', data=patch)
    assert git(replica, 'write-tree').decode().strip() == tree

    for for_publish in (True, False):
        with pytest.raises(GitHubError, match='limit 1000000 bytes'):
            workspaces.snapshot(current, for_publish=for_publish)
    monkeypatch.setattr(developer_repositories, 'SANDBOX_MAX_BYTES', len(patch) - 1)
    with pytest.raises(GitHubError, match='too large for Railway Sandbox testing'):
        rift_sandbox._snapshot(workspaces, current)


def test_rift_patch_with_more_than_400_changed_files(pilot, tmp_path):
    _, root, base, bot, workspaces = pilot
    replica = tmp_path / 'fresh'
    git(None, 'clone', '-q', '--no-local', str(root), str(replica))
    changed = root / 'changed'
    changed.mkdir()
    for i in range(425):
        (changed / f'file-{i}.txt').write_text('ordinary test fixture\n')
    current = workspaces.current('alice', bot)

    snapshot_base, tree, patch = rift_sandbox._snapshot(workspaces, current)
    assert snapshot_base == base
    git(replica, 'apply', '--index', '--binary', '-', data=patch)
    assert git(replica, 'write-tree').decode().strip() == tree
    assert len(list((replica / 'changed').iterdir())) == 425
    with pytest.raises(GitHubError, match='Publish at most 100 changed files'):
        workspaces.snapshot(current)


def test_workflow_snapshot_runs_with_public_fixture_and_secret_reference(pilot, tmp_path):
    _, root, _, bot, workspaces = pilot
    path = '.github/workflows/smoke.yml'
    workflow = root / path
    workflow.parent.mkdir(parents=True)
    workflow.write_text('name: Smoke\nenv:\n  WEB_SESSION_SECRET: example-ci-fixture\n')
    git(root, 'add', '--', path)
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'public workflow')
    base = git(root, 'rev-parse', 'HEAD').decode().strip()
    current = {**workspaces.current('alice', bot), 'base_sha': base}
    replica = tmp_path / 'fresh'
    git(None, 'clone', '-q', '--no-local', str(root), str(replica))

    workflow.write_text(workflow.read_text().replace('name: Smoke', 'name: Updated smoke')
                        + "  GITHUB_CLIENT_SECRET: '${{ secrets.BUILD_CLIENT_SECRET }}'\n")
    snapshot_base, tree, patch = rift_sandbox._snapshot(workspaces, current)
    assert snapshot_base == base
    git(replica, 'apply', '--index', '--binary', '-', data=patch)
    assert git(replica, 'write-tree').decode().strip() == tree
    assert (replica / path).read_bytes() == workflow.read_bytes()
    with pytest.raises(GitHubError, match='separate manual GitHub grant'):
        workspaces.snapshot(current)

    workflow.write_text(workflow.read_text().replace('example-ci-fixture', 'test-project-token'))
    with pytest.raises(rift_sandbox.SandboxError, match='sensitive change'):
        rift_sandbox._snapshot(workspaces, current)


def test_chat_retry_runs_once_and_reports_result_in_same_thread(pilot, monkeypatch):
    _, root, base, bot, _ = pilot
    store = task_store.CloudTaskStore()
    monkeypatch.setattr(chat_service, '_task_store', lambda: store)
    monkeypatch.setattr(chat_service.account_store, 'isolated', lambda _: False)
    conv = chat_service.create_conversation('alice', bot_id=bot['id'])
    conversation = conv['conversation_id']
    tree = git(root, 'write-tree').decode().strip()
    calls = []

    def remote(config, observed_base, observed_tree, patch, cancelled, progress):
        assert config[0] == 'pilot-environment'
        assert (observed_base, observed_tree, patch) == (base, tree, b'')
        assert not cancelled()
        progress('Running Kyrex Chat smoke tests in Railway…')
        calls.append('run')
        return {'outcome': 'success', 'exitCode': 0}

    stream_result = chat_service._stream_existing_bot_task

    async def view(user, conv, store, task_id, *args, **kwargs):
        assert user == 'alice' and conv['conversation_id'] == conversation
        task = store.claim_next('test-worker')
        if task:
            task_store.TaskWorker(store=store).execute_task(task)
        async for frame in stream_result(user, conv, store, task_id, *args, **kwargs):
            yield frame

    monkeypatch.setattr(rift_sandbox, '_remote_check', remote)
    monkeypatch.setattr(chat_service, '_stream_existing_bot_task', view)

    async def request():
        return [frame async for frame in chat_service.stream_chat(
            'alice', conversation, '/sandbox test', request_id='same-request')]

    first, second = asyncio.run(request()), asyncio.run(request())
    assert first[-1]['content'] == second[-1]['content']
    assert first[-1]['status'] == 'complete'
    assert 'smoke tests passed' in first[-1]['content']
    (task,) = store.tasks_for_conversation(conversation, 'alice')
    assert task['result']['sandbox_snapshot'] == {'base': base, 'tree': tree}
    messages = chat_service.get_conversation('alice', conversation)['messages']
    assert [message['role'] for message in messages] == ['user', 'assistant']
    assert calls == ['run']
    store.close()
