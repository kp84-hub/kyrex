"""Owner-selected local workspaces and explicit, snapshot-bound PR publishing.

Credentials stay in this host module; they never enter the engine, remotes,
command arguments or task payloads. Reader and publisher consent are separate.
"""
from __future__ import annotations
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import stat
import subprocess
import tempfile
import urllib.request

import bots
import connectors
from github_app import GitHubAppFlow
from github_connection import GitHubError, _NoRedirect, _LOCK, request

_THREAD_LOCK = threading.RLock()

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
SHA = re.compile(r'[0-9a-f]{40}')
MAX_DIFF = 1_000_000
MAX_FILES = 100
MAX_BYTES = 5 * 1024 * 1024


def credentials(owner, repository=None, *, write=False):
    flow = GitHubAppFlow(write=True) if write else GitHubAppFlow()
    connection = flow.connection
    lock_dir = connectors.data_dir() / 'github_credential_locks'
    lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256((owner + ('/writer' if write else '/reader')).encode()).hexdigest()
    with _LOCK, (lock_dir / key).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rec = connection._load().get(connectors.ConnectorStore._owner_key(owner), {})
        if rec.get('mode') != 'app':
            raise GitHubError('Connect the GitHub ' + ('publisher' if write else 'reader') + ' app first.')
        token, selected = flow.credentials(owner, rec)
    if repository is not None:
        if not isinstance(repository, str) or not REPO.fullmatch(repository) or any(
                part in ('.', '..') for part in repository.split('/')):
            raise GitHubError('Choose a repository as owner/name.')
        match = next((r for r in selected if r.lower() == repository.lower()), None)
        if match is None:
            raise GitHubError('This repository is not selected for the GitHub ' + ('publisher.' if write else 'reader.'))
        return token, match
    return token, selected


def write_api(token, path, values):
    # Only this service's bounded Git-data and PR endpoints can mutate GitHub.
    if not re.fullmatch(r'/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(git/(blobs|trees|commits|refs)|pulls)', path):
        raise GitHubError('Unsupported GitHub publishing endpoint.')
    body = json.dumps(values).encode()
    if len(body) > MAX_BYTES * 2:
        raise GitHubError('The publish request is too large.')
    req = urllib.request.Request('https://api.github.com' + path, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json', 'User-Agent': 'Kyrex-Repository-Publisher'})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=30) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
    except (OSError, ValueError):
        raise GitHubError('GitHub publishing did not return a confirmed result. Check GitHub before trying again.') from None


def git(root, *args, env=None, data=None):
    # Do not inherit global provider/GitHub credentials or git config. Built-in
    # operations only; no hooks, external diff, filters or submodule commands.
    clean = {'PATH': os.defpath, 'LANG': 'C.UTF-8',
             'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
             'GIT_TERMINAL_PROMPT': '0', 'GIT_LFS_SKIP_SMUDGE': '1'}
    clean.update(env or {})
    try:
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
            '-c', 'credential.helper=', '-c', 'http.followRedirects=false', *args],
            cwd=root, input=data, env=clean, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubError('Repository operation could not finish. Your existing workspace was preserved.') from None
    if result.returncode:
        # Git stderr can contain credentials or repository-controlled text.
        raise GitHubError('Repository operation failed. Check repository access and workspace state.')
    return result.stdout


class RepositoryWorkspaces:
    def __init__(self, data_dir=None, *, read=request, write=write_api, auth=credentials):
        self.root = Path(data_dir or connectors.data_dir())
        self.read, self.write, self.auth = read, write, auth
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / 'developer_repositories.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS workspaces (
                  owner TEXT, bot TEXT, repository TEXT, path TEXT, base TEXT, base_sha TEXT,
                  PRIMARY KEY(owner,bot,repository));
                CREATE TABLE IF NOT EXISTS original_workspaces (
                  owner TEXT, bot TEXT, rift TEXT, repo TEXT, PRIMARY KEY(owner,bot));
                CREATE TABLE IF NOT EXISTS publishes (
                  owner TEXT, bot TEXT, repository TEXT, tree TEXT, state TEXT, url TEXT,
                  PRIMARY KEY(owner,bot,repository,tree));
            ''')
        self.db_path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def locked(self, owner, bot_id):
        key = hashlib.sha256((owner + '\0' + bot_id).encode()).hexdigest()
        lock_dir = self.root / 'developer_repository_locks'
        lock_dir.mkdir(exist_ok=True, mode=0o700)
        with _THREAD_LOCK, (lock_dir / key).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def bot(self, owner, bot_id, *, idle=False):
        from task_store import CloudTaskStore, NONTERMINAL_STATUSES
        bot = bots.get_bot(bot_id)
        # Match the roster's effective coding grants, including legacy policies.
        # A Bot's display name or a lone filesystem grant cannot authorize this.
        import serve
        if bot.get('owner') != owner or not serve.developer_repository_granted(bot.get('policy')):
            raise GitHubError('An owner-scoped Developer Bot is required.')
        if idle:
            if bots.is_running(bot):
                raise GitHubError('Pause Developer Bot before switching repositories or publishing.')
            store = CloudTaskStore()
            if any(store.list_tasks(status=s, bot_id=bot_id, limit=1) for s in NONTERMINAL_STATUSES):
                raise GitHubError('Wait for Developer Bot’s queued or running tasks to finish first.')
        return bot

    def current(self, owner, bot):
        with self.db() as db:
            row = db.execute('SELECT repository,path,base,base_sha FROM workspaces WHERE owner=? AND bot=? AND path=?',
                             (owner, bot['id'], bot.get('rift'))).fetchone()
        return dict(zip(('repository', 'path', 'base', 'base_sha'), row)) if row else None

    def view(self, owner, bot_id):
        bot = self.bot(owner, bot_id)
        reader_error = publisher_error = ''
        try:
            _, repos = self.auth(owner)
        except GitHubError as exc:
            repos, reader_error = [], str(exc)
        current = self.current(owner, bot)
        flow = GitHubAppFlow(write=True)
        publisher_connected = flow.connection.view(owner)['connected']
        publisher_repos = []
        if publisher_connected:
            try:
                _, publisher_repos = self.auth(owner, write=True)
            except GitHubError as exc:
                publisher_error = str(exc)
        return {'repositories': repos, 'repository': current['repository'] if current else None,
                'base': current['base'] if current else None, 'publisher_connected': publisher_connected,
                'reader_error': reader_error, 'publisher_error': publisher_error,
                'can_publish': bool(current and current['repository'].lower() in [r.lower() for r in publisher_repos])}

    def checkout(self, owner, bot_id, repository):
        with self.locked(owner, bot_id):
            bot = self.bot(owner, bot_id, idle=True)
            if repository is None:  # Restore the original Kyrex Rift intact.
                with self.db() as db:
                    row = db.execute('SELECT rift,repo FROM original_workspaces WHERE owner=? AND bot=?', (owner, bot_id)).fetchone()
                if not row:
                    raise GitHubError('No previous workspace is saved.')
                bots.update_bot(bot_id, rift=row[0], repo=row[1])
                return {'repository': None}
            token, repository = self.auth(owner, repository)
            with self.db() as db:
                row = db.execute('SELECT path FROM workspaces WHERE owner=? AND bot=? AND repository=?',
                                 (owner, bot_id, repository)).fetchone()
            if row:
                path = Path(row[0])
                git(path, 'rev-parse', '--git-dir')  # Never reset existing work.
            else:
                meta = self.read(token, '/repos/' + repository)
                base = meta.get('default_branch')
                if str(meta.get('full_name', '')).lower() != repository.lower() or not isinstance(base, str):
                    raise GitHubError('GitHub repository identity could not be verified.')
                key = hashlib.sha256((owner + '\0' + bot_id + '\0' + repository).encode()).hexdigest()
                parent = self.root / 'developer_repositories' / key
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = parent / 'checkout'
                if path.exists():
                    raise GitHubError('An unfinished checkout exists. Contact the server administrator; no files were overwritten.')
                # Basic header is passed through the child Git environment only.
                # Temporary clone has no repository-controlled local config yet.
                basic = base64.b64encode(('x-access-token:' + token).encode()).decode()
                with tempfile.TemporaryDirectory(dir=parent) as staging:
                    tmp = Path(staging) / 'repo'
                    git(None, 'clone', '--no-recurse-submodules', '--template=', '--single-branch',
                        '--branch', base, '--', 'https://github.com/' + repository + '.git', str(tmp), env={
                            'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraHeader',
                            'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + basic})
                    base_sha = git(tmp, 'rev-parse', 'HEAD').decode().strip()
                    git(tmp, 'switch', '-c', 'kyrex/work-' + secrets.token_hex(6))
                    tmp.rename(path)
                with self.db() as db:
                    db.execute('INSERT INTO workspaces VALUES (?,?,?,?,?,?)',
                               (owner, bot_id, repository, str(path), base, base_sha))
            # Recheck lifecycle and queue after a potentially long clone.
            self.bot(owner, bot_id, idle=True)
            with self.db() as db:
                db.execute('INSERT OR IGNORE INTO original_workspaces VALUES (?,?,?,?)',
                           (owner, bot_id, bot.get('rift'), bot.get('repo')))
            bots.update_bot(bot_id, rift=str(path), repo='https://github.com/' + repository + '.git')
            return {'repository': repository}

    def validate_execution(self, owner, bot_id):
        bot = bots.get_bot(bot_id)
        current = self.current(owner, bot)
        if current:
            self.bot(owner, bot_id)
            self.auth(owner, current['repository'])  # Live read selection, no token sent to engine.
        return current

    def snapshot(self, current):
        root, base = Path(current['path']), current['base_sha']
        if not SHA.fullmatch(base):
            raise GitHubError('Workspace base is invalid.')
        paths = (git(root, 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', 'HEAD').split(b'\0')
                 + git(root, 'ls-files', '-z', '--others', '--exclude-standard').split(b'\0'))
        # Alternate index makes reviewing/publishing leave the real index alone.
        with tempfile.TemporaryDirectory() as staging:
            env = {'GIT_INDEX_FILE': staging + '/index'}
            git(root, 'read-tree', 'HEAD', env=env)
            for raw in sorted(set(paths) - {b''}):
                name = raw.decode('utf-8')
                if name.startswith('/') or any(p in ('', '.', '..', '.git') for p in name.split('/')):
                    raise GitHubError('Unsupported repository path.')
                file = root / name
                # No symlink parent traversal; no reading outside this checkout.
                if any(parent.is_symlink() for parent in [file, *file.parents] if parent != root):
                    raise GitHubError('Symlink changes need a manual Git workflow.')
                if not file.exists():
                    git(root, 'update-index', '--force-remove', '--', name, env=env)
                    continue
                mode = file.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise GitHubError('Submodules and special files need a manual Git workflow.')
                if file.stat().st_size > MAX_BYTES:
                    raise GitHubError('A workspace file exceeds the 5 MB publishing limit.')
                sha = git(root, 'hash-object', '-w', '--no-filters', '--stdin', data=file.read_bytes()).decode().strip()
                git(root, 'update-index', '--add', '--cacheinfo', '100755' if mode & 0o111 else '100644', sha, name, env=env)
            tree = git(root, 'write-tree', env=env).decode().strip()
        diff = git(root, 'diff', '--no-ext-diff', '--no-textconv', '--binary', base, tree)
        if len(diff) > MAX_DIFF:
            raise GitHubError(
                f'The diff is too large to review here ({len(diff)} bytes; '
                f'limit {MAX_DIFF} bytes). Use a manual Git workflow.')
        entries = []
        changed = git(root, 'diff', '--raw', '-z', '--no-renames', base, tree).split(b'\0')
        total = 0
        for i in range(0, len(changed) - 1, 2):
            meta, raw = changed[i:i+2]
            old_mode, mode, old_sha, sha, action = meta.decode().split()
            name = raw.decode('utf-8')
            if name.startswith('.github/workflows/'):
                raise GitHubError('Workflow changes require a separate manual GitHub grant.')
            entry = {'path': name, 'mode': mode if mode != '000000' else old_mode[1:], 'type': 'blob'}
            if action == 'D':
                entry['sha'] = None
            else:
                if mode not in ('100644', '100755'):
                    raise GitHubError('Symlink and submodule changes need a manual Git workflow.')
                content = git(root, 'cat-file', 'blob', tree + ':' + name)
                total += len(content)
                entry['data'] = content
            entries.append(entry)
        if len(entries) > MAX_FILES or total > MAX_BYTES:
            raise GitHubError('Publish at most 100 changed files and 5 MB at a time.')
        return tree, diff.decode('utf-8', errors='replace'), entries

    def review(self, owner, bot_id):
        with self.locked(owner, bot_id):
            bot = self.bot(owner, bot_id, idle=True)
            current = self.current(owner, bot)
            if not current:
                raise GitHubError('Select a connected repository first.')
            self.auth(owner, current['repository'])
            tree, diff, entries = self.snapshot(current)
            return {'repository': current['repository'], 'base': current['base'], 'snapshot': tree,
                    'diff': diff, 'files': [v['path'] for v in entries]}

    def publish(self, owner, bot_id, snapshot, title, body=''):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200 or not isinstance(body, str) or len(body) > 10000:
            raise GitHubError('Provide a PR title up to 200 characters and description up to 10000 characters.')
        if not isinstance(snapshot, str) or not SHA.fullmatch(snapshot):
            raise GitHubError('Review the changes before publishing.')
        with self.locked(owner, bot_id):
            bot = self.bot(owner, bot_id, idle=True)
            current = self.current(owner, bot)
            if not current:
                raise GitHubError('Select a connected repository first.')
            self.auth(owner, current['repository'])
            token, repository = self.auth(owner, current['repository'], write=True)
            tree, _, entries = self.snapshot(current)
            if tree != snapshot:
                raise GitHubError('The workspace changed after review. Review the new diff before publishing.')
            if not entries:
                raise GitHubError('There are no changes to publish.')
            key = (owner, bot_id, repository, tree)
            with self.db() as db:
                existing = db.execute('SELECT state,url FROM publishes WHERE owner=? AND bot=? AND repository=? AND tree=?', key).fetchone()
                if existing:
                    if existing[0] == 'sent':
                        return {'url': existing[1], 'already_published': True}
                    raise GitHubError('A previous publish is unconfirmed. Check GitHub; Kyrex will not retry automatically.')
                db.execute('INSERT INTO publishes VALUES (?,?,?,?,?,?)', (*key, 'sending', None))
            prefix = '/repos/' + repository
            try:
                base_commit = self.read(token, prefix + '/git/commits/' + current['base_sha'])
                remote_entries = []
                for item in entries:
                    entry = {k: v for k, v in item.items() if k != 'data'}
                    if 'data' in item:
                        blob = self.write(token, prefix + '/git/blobs', {
                            'content': base64.b64encode(item['data']).decode(), 'encoding': 'base64'})
                        entry['sha'] = blob['sha']
                    remote_entries.append(entry)
                remote_tree = self.write(token, prefix + '/git/trees', {'base_tree': base_commit['tree']['sha'], 'tree': remote_entries})
                if remote_tree.get('sha') != tree:
                    raise GitHubError('GitHub did not confirm the reviewed file tree.')
                commit = self.write(token, prefix + '/git/commits', {'message': title.strip(), 'tree': tree,
                    'parents': [current['base_sha']]})
                branch = 'kyrex/review-' + secrets.token_hex(12)
                self.write(token, prefix + '/git/refs', {'ref': 'refs/heads/' + branch, 'sha': commit['sha']})
                pr = self.write(token, prefix + '/pulls', {'title': title.strip(), 'body': body,
                    'head': branch, 'base': current['base']})
                url = pr.get('html_url', '')
                if not re.fullmatch(r'https://github\.com/' + re.escape(repository) + r'/pull/[1-9][0-9]*', url, re.I):
                    raise GitHubError('GitHub did not return a verified PR link.')
            except Exception:
                with self.db() as db:
                    db.execute('UPDATE publishes SET state=? WHERE owner=? AND bot=? AND repository=? AND tree=?', ('unknown', *key))
                raise GitHubError('Publishing was not confirmed. Check the repository on GitHub before retrying.') from None
            with self.db() as db:
                db.execute('UPDATE publishes SET state=?,url=? WHERE owner=? AND bot=? AND repository=? AND tree=?', ('sent', url, *key))
            return {'url': url, 'already_published': False}
