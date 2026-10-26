import base64
import json
import subprocess
from pathlib import Path
import pytest

import bots
import connectors
import developer_repositories as dr
import serve
from github_connection import GitHubConnection, GitHubError
from github_app import GitHubAppFlow
from test_github_app import setup, BASE, state


def command(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path / 'data'))
    source = tmp_path / 'source'; source.mkdir()
    command(source, 'init', '-b', 'main')
    (source / 'README.md').write_text('before\n')
    (source / '.gitignore').write_text('.env\n')
    command(source, 'add', '.')
    command(source, '-c', 'user.name=test', '-c', 'user.email=test@example.test', 'commit', '-m', 'initial')
    bot = {'id': 'dev', 'owner': 'alice', 'status': 'paused', 'rift': str(source), 'repo': 'old-repo',
           'policy': serve.developer_preset_policy()}
    monkeypatch.setattr(bots, 'get_bot', lambda id: bot.copy() if id == 'dev' else (_ for _ in ()).throw(KeyError(id)))
    monkeypatch.setattr(bots, 'update_bot', lambda id, **fields: bot.update(fields) or bot.copy())
    import task_store
    monkeypatch.setattr(task_store, 'CloudTaskStore', lambda: type('Store', (), {'list_tasks': lambda self, **kwargs: []})())
    selected = {'read': ['alice/private'], 'write': ['alice/private']}
    auth_calls = []
    def auth(owner, repository=None, *, write=False):
        auth_calls.append((owner, repository, write))
        assert owner == 'alice'
        repos = selected['write' if write else 'read']
        if repository is not None:
            if repository not in repos: raise GitHubError('repository not selected')
            return 'test-secret-token', repository
        return 'test-secret-token', repos
    calls = []
    def read(token, path, params=None):
        if '/git/commits/' in path:
            return {'tree': {'sha': command(source, 'rev-parse', 'HEAD^{tree}')}}
        return {'full_name': 'alice/private', 'default_branch': 'main'}
    def write(token, path, values):
        calls.append((path, values))
        current = service.current('alice', bot)
        root = Path(current['path'])
        if path.endswith('/blobs'):
            data = base64.b64decode(values['content'])
            sha = subprocess.check_output(['git', '-C', str(root), 'hash-object', '--stdin'], input=data).decode().strip()
            return {'sha': sha}
        if path.endswith('/trees'):
            return {'sha': service.snapshot(current)[0]}
        if path.endswith('/commits'): return {'sha': 'a' * 40}
        if path.endswith('/refs'): return {}
        if path.endswith('/pulls'): return {'html_url': 'https://github.com/alice/private/pull/1'}
        raise AssertionError(path)
    actual_git = dr.git
    clones = []
    def git(root, *args, **kwargs):
        if 'fetch' in args:
            assert 'https://github.com/alice/private.git' in args
            assert 'test-secret-token' not in str(args)
            assert 'Authorization: Basic ' in kwargs['env']['GIT_CONFIG_VALUE_0']
            modified = list(args)
            modified[modified.index('https://github.com/alice/private.git')] = str(source)
            modified[modified.index('protocol.file.allow=never')] = 'protocol.file.allow=always'
            # Local fixture transport has no credentials.
            return actual_git(root, *modified)
        if args[0] == 'clone':
            clones.append((args, kwargs))
            assert args[-2] == 'https://github.com/alice/private.git'
            assert 'test-secret-token' not in str(args)
            modified = list(args); modified[-2] = str(source)
            return actual_git(root, *modified, **kwargs)
        return actual_git(root, *args, **kwargs)
    monkeypatch.setattr(dr, 'git', git)
    service = dr.RepositoryWorkspaces(tmp_path / 'data', read=read, write=write, auth=auth)
    return service, bot, selected, calls, clones, source, auth_calls


def test_authenticated_isolated_checkout_and_restore_preserve_dirty_work(workspace):
    service, bot, _, _, clones, original, _ = workspace
    (original / 'README.md').write_text('dirty original\n')
    assert service.checkout('alice', 'dev', 'alice/private')['repository'] == 'alice/private'
    root = Path(bot['rift'])
    assert root != original and root.exists()
    assert 'Authorization: Basic ' in clones[0][1]['env']['GIT_CONFIG_VALUE_0']
    assert 'test-secret-token' not in (root / '.git/config').read_text()
    assert command(root, 'branch', '--show-current').startswith('kyrex/work-')
    (root / 'README.md').write_text('dirty selected\n')
    service.checkout('alice', 'dev', None)
    assert bot['rift'] == str(original) and bot['repo'] == 'old-repo'
    assert (original / 'README.md').read_text() == 'dirty original\n'
    service.checkout('alice', 'dev', 'alice/private')
    assert (Path(bot['rift']) / 'README.md').read_text() == 'dirty selected\n' and len(clones) == 1


def test_owner_scope_role_lifecycle_and_live_repository_selection(workspace):
    service, bot, selected, _, clones, _, _ = workspace
    with pytest.raises(GitHubError): service.checkout('bob', 'dev', 'alice/private')
    bot['status'] = 'running'
    with pytest.raises(GitHubError, match='Pause'): service.checkout('alice', 'dev', 'alice/private')
    bot['status'] = 'paused'; bot['policy'] = {'fs:write': 1}
    with pytest.raises(GitHubError): service.checkout('alice', 'dev', 'alice/private')
    bot['policy'] = serve.developer_preset_policy()
    with pytest.raises(GitHubError): service.checkout('alice', 'dev', 'alice/unselected')
    assert not clones
    service.checkout('alice', 'dev', 'alice/private')
    selected['read'] = []
    with pytest.raises(GitHubError): service.validate_execution('alice', 'dev')


def advance_source(source, name='upstream.txt'):
    (source / name).write_text('new remote feature\n')
    command(source, 'add', '-f', name)
    command(source, '-c', 'user.name=test', '-c', 'user.email=test@example.test', 'commit', '-m', 'upstream')
    return command(source, 'rev-parse', 'HEAD')


def test_refresh_advances_clean_checkout_and_preserves_untracked_notes(workspace):
    service, bot, _, calls, _, source, _ = workspace
    service.checkout('alice', 'dev', 'alice/private')
    root = Path(bot['rift']); branch = command(root, 'branch', '--show-current')
    (root / 'notes.txt').write_text('private untracked work')
    head = advance_source(source)
    state = service.refresh('alice', 'dev')['workspace']
    assert state['status'] == 'updated' and state['behind'] == 0
    assert command(root, 'rev-parse', 'HEAD') == head
    assert command(root, 'branch', '--show-current') == branch
    assert (root / 'notes.txt').read_text() == 'private untracked work'
    assert service.current('alice', bot)['base_sha'] == head
    assert 'test-secret-token' not in (root / '.git/config').read_text()
    assert not calls


def test_dirty_stale_checkout_is_preserved_and_cannot_publish(workspace):
    service, bot, _, calls, _, source, _ = workspace
    service.checkout('alice', 'dev', 'alice/private'); root = Path(bot['rift'])
    (root / 'README.md').write_text('staged local work\n'); command(root, 'add', 'README.md')
    (root / 'README.md').write_text('unstaged local work\n')
    head = command(root, 'rev-parse', 'HEAD')
    before = (root / '.git/index').read_bytes()
    advance_source(source)
    state = service.refresh('alice', 'dev')['workspace']
    assert state['status'] == 'behind' and state['behind'] == 1
    assert command(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == before
    assert (root / 'README.md').read_text() == 'unstaged local work\n'
    with pytest.raises(GitHubError, match='behind or diverged'):
        service.review('alice', 'dev')
    with pytest.raises(GitHubError, match='behind or diverged'):
        service.publish('alice', 'dev', 'a'*64, 'Unsafe stale tree')
    assert not calls
    # Explicit synchronization can retain non-conflicting local changes.
    command(root, 'merge', '--ff-only', 'origin/main')
    review = service.review('alice', 'dev')
    assert review['files'] == ['README.md'], 'Already merged upstream files are not PR changes.'


def test_ignored_file_collision_and_divergent_commits_are_preserved(workspace):
    service, bot, _, calls, _, source, _ = workspace
    service.checkout('alice', 'dev', 'alice/private'); root = Path(bot['rift'])
    (root / '.env').write_text('private local settings')
    head = command(root, 'rev-parse', 'HEAD')
    advance_source(source, '.env')
    assert service.refresh('alice', 'dev')['workspace']['status'] == 'behind'
    assert command(root, 'rev-parse', 'HEAD') == head
    assert (root / '.env').read_text() == 'private local settings'
    (root / 'local.txt').write_text('committed local work')
    command(root, 'add', 'local.txt')
    command(root, '-c', 'user.name=test', '-c', 'user.email=test@example.test', 'commit', '-m', 'local')
    head = command(root, 'rev-parse', 'HEAD')
    state = service.refresh('alice', 'dev')['workspace']
    assert state['status'] == 'diverged' and state['ahead'] == 1 and state['behind'] == 1
    assert command(root, 'rev-parse', 'HEAD') == head
    assert not calls


def test_staged_changes_count_as_dirty_when_working_tree_matches_head(workspace):
    service, bot, _, calls, _, source, _ = workspace
    service.checkout('alice', 'dev', 'alice/private'); root = Path(bot['rift'])
    (root / 'README.md').write_text('staged work\n'); command(root, 'add', 'README.md')
    (root / 'README.md').write_text('before\n')
    head = command(root, 'rev-parse', 'HEAD'); index = (root / '.git/index').read_bytes()
    advance_source(source)
    state = service.refresh('alice', 'dev')['workspace']
    assert state['status'] == 'behind' and state['dirty']
    assert command(root, 'rev-parse', 'HEAD') == head
    assert (root / '.git/index').read_bytes() == index and not calls


def test_new_base_invalidates_review_even_when_reviewed_tree_is_identical(workspace):
    service, bot, _, calls, _, source, _ = workspace
    service.checkout('alice', 'dev', 'alice/private'); root = Path(bot['rift'])
    (root / 'README.md').write_text('local change\n')
    review = service.review('alice', 'dev')
    tree = service.snapshot(service.current('alice', bot))[0]
    command(source, '-c', 'user.name=test', '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-m', 'new base')
    service.refresh('alice', 'dev')  # Fetch only; local changes are preserved.
    command(root, 'merge', '--ff-only', 'origin/main')
    assert service.snapshot(service.current('alice', bot))[0] == tree
    with pytest.raises(GitHubError, match='after review'):
        service.publish('alice', 'dev', review['snapshot'], 'Old review')
    assert not calls
    fresh = service.review('alice', 'dev')
    assert fresh['snapshot'] != review['snapshot'] and fresh['files'] == ['README.md']


def test_running_execution_uses_live_authenticated_freshness_but_ui_refresh_requires_idle(workspace):
    service, bot, _, calls, _, source, _ = workspace
    service.checkout('alice', 'dev', 'alice/private')
    head = advance_source(source)
    bot['status'] = 'running'
    with pytest.raises(GitHubError, match='Pause'):
        service.refresh('alice', 'dev')
    current = service.validate_execution('alice', 'dev')
    assert current['freshness']['status'] == 'updated' and current['freshness']['head'] == head
    assert 'test-secret-token' not in json.dumps(current)
    assert not calls


@pytest.mark.parametrize('policy', [
    {**serve.developer_preset_policy(), 'fs:delete': 'deny'},
    {'*': 0, 'fs:write': 1, 'repo:pr': 1},
    {'*': 0},
    {**serve.developer_preset_policy(), 'shell:run': 1},
])
def test_legacy_coding_grants_can_select_and_review_without_policy_changes(workspace, policy):
    service, bot, *_ = workspace
    bot['policy'] = policy.copy()
    service.checkout('alice', 'dev', 'alice/private')
    assert service.review('alice', 'dev')['repository'] == 'alice/private'
    assert bot['policy'] == policy


@pytest.mark.parametrize('policy', [None, {}, {'*': 'deny'}, {'*': True},
    {'fs:write': 1}, {'cal:create': 1},
    {**serve.developer_preset_policy(), 'fs:write': 'deny'},
    {**serve.developer_preset_policy(), 'repo:pr': 'deny'},
    {**serve.developer_preset_policy(), 'repo:read': 'deny'},
    {**serve.developer_preset_policy(), 'fs:read': 'deny'},
])
def test_incomplete_or_invalid_grants_cannot_manage_repositories(workspace, policy):
    service, bot, _, _, clones, *_ = workspace
    bot['policy'] = policy
    assert not serve.developer_repository_granted(policy)
    with pytest.raises(GitHubError):
        service.checkout('alice', 'dev', 'alice/private')
    assert not clones


def test_busy_queue_refuses_switch_and_publish(workspace, monkeypatch):
    import task_store
    service, _, _, _, clones, _, _ = workspace
    monkeypatch.setattr(task_store, 'CloudTaskStore', lambda: type('Store', (), {'list_tasks': lambda self, **kwargs: [{}]})())
    with pytest.raises(GitHubError, match='tasks'): service.checkout('alice', 'dev', 'alice/private')
    assert not clones


def test_review_and_binary_publication_preserve_index_and_are_idempotent(workspace):
    service, bot, _, calls, _, _, auth_calls = workspace
    service.checkout('alice', 'dev', 'alice/private'); root = Path(bot['rift'])
    before_index = (root / '.git/index').read_bytes()
    (root / 'README.md').write_text('after\n')
    (root / 'binary.bin').write_bytes(b'\0\xff\x01')
    (root / '.env').write_text('ignored-secret')
    review = service.review('alice', 'dev')
    assert review['files'] == ['README.md', 'binary.bin'] and 'ignored-secret' not in review['diff']
    assert (root / '.git/index').read_bytes() == before_index
    result = service.publish('alice', 'dev', review['snapshot'], 'Change project', 'Tests passed locally')
    assert result['url'].endswith('/pull/1') and ('alice', 'alice/private', True) in auth_calls
    assert calls[-1][1]['base'] == 'main' and calls[-1][1]['head'].startswith('kyrex/review-')
    assert all('/merge' not in path for path, _ in calls)
    count = len(calls)
    assert service.publish('alice', 'dev', review['snapshot'], 'Change project')['already_published']
    assert len(calls) == count and (root / '.git/index').read_bytes() == before_index


def test_stale_review_and_revoked_write_grant_prevent_side_effects(workspace):
    service, bot, selected, calls, _, _, _ = workspace
    service.checkout('alice', 'dev', 'alice/private'); root = Path(bot['rift'])
    (root / 'README.md').write_text('first\n'); review = service.review('alice', 'dev')
    (root / 'README.md').write_text('changed after review\n')
    with pytest.raises(GitHubError, match='after review'): service.publish('alice', 'dev', review['snapshot'], 'Title')
    review = service.review('alice', 'dev'); selected['write'] = []
    with pytest.raises(GitHubError): service.publish('alice', 'dev', review['snapshot'], 'Title')
    assert not calls


def test_full_large_review_publishes_exact_content_and_rejects_stale_snapshot(workspace):
    service, bot, _, calls, _, _, _ = workspace
    service.checkout('alice', 'dev', 'alice/private')
    root = Path(bot['rift'])
    before_index = (root / '.git/index').read_bytes()
    content = ''.join(f'{n:05d} ' + 'x' * 43 + '\n' for n in range(7000))
    (root / 'README.md').write_text(content)
    review = service.review('alice', 'dev')
    assert 200_000 < len(review['diff'].encode()) <= dr.MAX_DIFF
    assert review['diff'].endswith('+' + content.splitlines()[-1] + '\n')
    assert (root / '.git/index').read_bytes() == before_index

    # A larger review retains the same exact-tree gate; an old review cannot
    # approve even a one-line change beyond the previously reviewed payload.
    content += 'changed after review\n'
    (root / 'README.md').write_text(content)
    with pytest.raises(GitHubError, match='after review'):
        service.publish('alice', 'dev', review['snapshot'], 'Large change')
    assert not calls
    review = service.review('alice', 'dev')
    result = service.publish('alice', 'dev', review['snapshot'], 'Large change')
    assert result['url'].endswith('/pull/1')
    blobs = [values for path, values in calls if path.endswith('/blobs')]
    assert len(blobs) == 1
    assert base64.b64decode(blobs[0]['content']) == content.encode()
    assert (root / '.git/index').read_bytes() == before_index


def test_diff_over_review_limit_fails_before_remote_writes(workspace):
    service, bot, _, calls, _, _, _ = workspace
    service.checkout('alice', 'dev', 'alice/private')
    root = Path(bot['rift'])
    (root / 'README.md').write_text('x' * dr.MAX_DIFF + '\n')
    with pytest.raises(GitHubError, match=f'limit {dr.MAX_DIFF} bytes'):
        service.review('alice', 'dev')
    assert not calls


@pytest.mark.parametrize('limit', ['files', 'bytes'])
def test_larger_review_does_not_bypass_file_or_total_content_limits(workspace, monkeypatch, limit):
    service, bot, _, calls, _, _, _ = workspace
    service.checkout('alice', 'dev', 'alice/private')
    root = Path(bot['rift'])
    (root / 'one.txt').write_text('a' * 40)
    (root / 'two.txt').write_text('b' * 40)
    if limit == 'files':
        monkeypatch.setattr(dr, 'MAX_FILES', 1)
    else:
        # Each file is below the per-file ceiling, but their sum exceeds it.
        monkeypatch.setattr(dr, 'MAX_BYTES', 64)
    with pytest.raises(GitHubError, match='Publish at most'):
        service.review('alice', 'dev')
    assert not calls


def test_uncertain_publish_is_not_retried(workspace):
    service, bot, _, calls, _, _, _ = workspace
    service.checkout('alice', 'dev', 'alice/private')
    (Path(bot['rift']) / 'README.md').write_text('new\n'); review = service.review('alice', 'dev')
    original_write = service.write
    def uncertain(token, path, payload):
        if path.endswith('/pulls'): raise OSError('secret HTTP failure')
        return original_write(token, path, payload)
    service.write = uncertain
    with pytest.raises(GitHubError, match='not confirmed'): service.publish('alice', 'dev', review['snapshot'], 'Title')
    count = len(calls)
    with pytest.raises(GitHubError, match='not retry'): service.publish('alice', 'dev', review['snapshot'], 'Title')
    assert len(calls) == count


@pytest.mark.parametrize('kind', ['symlink', 'workflow', 'large'])
def test_unsupported_changes_fail_before_publication(workspace, kind, tmp_path):
    service, bot, _, calls, _, _, _ = workspace
    service.checkout('alice', 'dev', 'alice/private'); root = Path(bot['rift'])
    if kind == 'symlink':
        (root / 'leak').symlink_to(tmp_path / 'source/README.md')
    elif kind == 'workflow':
        (root / '.github/workflows').mkdir(parents=True)
        (root / '.github/workflows/ci.yml').write_text('name: ci')
    else:
        (root / 'big').write_bytes(b'a' * (dr.MAX_BYTES + 1))
    with pytest.raises(GitHubError): service.review('alice', 'dev')
    assert not calls


def test_deletions_and_committed_changes_are_reviewed(workspace):
    service, bot, _, calls, _, _, _ = workspace
    service.checkout('alice', 'dev', 'alice/private'); root = Path(bot['rift'])
    (root / 'README.md').unlink()
    command(root, 'add', '-u')
    command(root, '-c', 'user.name=test', '-c', 'user.email=test@example.test', 'commit', '-m', 'remove')
    review = service.review('alice', 'dev')
    assert review['files'] == ['README.md'] and '-before' in review['diff']
    service.publish('alice', 'dev', review['snapshot'], 'Remove file')
    tree = next(payload for path, payload in calls if path.endswith('/trees'))
    assert tree['tree'] == [{'path': 'README.md', 'mode': '100644', 'type': 'blob', 'sha': None}]


def test_publisher_consent_has_separate_manifest_and_scope_validation(setup, tmp_path):
    reader, connection, mode, _ = setup
    writer_conn = GitHubConnection(tmp_path / 'writer-connections.json', reader.transport)
    writer = GitHubAppFlow(tmp_path / 'writer-apps.json', reader.transport, reader.post, writer_conn, write=True)
    reader_url = reader.begin('alice', BASE)['authorization_url']
    writer_url = writer.begin('alice', BASE)['authorization_url']
    manifest = writer.manifest('alice', state(writer_url), BASE)
    assert '/github-write/setup' in writer_url
    assert manifest['default_permissions'] == {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read'}
    assert reader.manifest('alice', state(reader_url), BASE)['default_permissions']['contents'] == 'read'
    with pytest.raises(GitHubError): reader.manifest('alice', state(writer_url), BASE)
    with pytest.raises(GitHubError): writer.registered('alice', state(writer_url), BASE, 'registration')
    mode['permissions'] = manifest['default_permissions']
    writer_url = writer.begin('alice', BASE)['authorization_url']
    install = writer.registered('alice', state(writer_url), BASE, 'registration')
    oauth = writer.installed('alice', state(install), BASE, '7')
    assert writer.callback('alice', state(oauth), BASE, 'auth')['connected']
    assert not connection.view('alice')['connected'] and writer_conn.view('alice')['connected']
    rec = writer_conn._load()[connectors.ConnectorStore._owner_key('alice')]
    assert writer.credentials('alice', rec)[1] == ['alice/private']
    mode['permissions'] = {'contents': 'read', 'metadata': 'read'}
    with pytest.raises(GitHubError): writer.credentials('alice', rec)
