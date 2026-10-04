"""Owner-scoped GitHub reads. Credentials stay in encrypted host storage."""
from __future__ import annotations
import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import connectors

class GitHubError(Exception):
    pass

_LOCK = threading.RLock()
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
MAX_BYTES = 256_000

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def request(token, path, params=None):
    """Only fixed api.github.com GETs, no credential-bearing redirects."""
    url = 'https://api.github.com' + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Kyrex-ReadOnly',
    }, method='GET')
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=20) as resp:
            raw = resp.read(2_000_001)
            if len(raw) > 2_000_000:
                raise GitHubError('GitHub response is too large; choose a smaller directory or file.')
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        messages = {401: 'GitHub token expired or was revoked. Reconnect in Connections.',
                    403: 'GitHub denied access or rate limited the request. Check token permissions and try later.',
                    404: 'Repository, ref, or path was not found or is not accessible with this token.'}
        raise GitHubError(messages.get(exc.code, 'GitHub read failed. Try again later.')) from None
    except (OSError, ValueError):
        raise GitHubError('GitHub could not be reached or returned an invalid response.') from None

class GitHubConnection:
    def __init__(self, path=None, transport=None):
        self.path = Path(path) if path else connectors.data_dir() / 'github_connections.json'
        self.transport = transport or request

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (OSError, ValueError):
            raise GitHubError('GitHub connection storage is unavailable.') from None

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def view(self, owner):
        with _LOCK:
            rec = self._load().get(connectors.ConnectorStore._owner_key(owner), {})
        try:
            connectors._box()
            configured = True
        except connectors.ConnectorError:
            configured = False
        connected = bool(rec.get('sealed'))
        return {'provider': 'github', 'configured': configured, 'connected': connected,
                'usable': connected and configured, 'expired': False, 'read_only': True,
                'status': 'connected' if connected else 'disconnected',
                'capabilities': {'bots': {'github_reader': {
                    'capabilities': ['github.read'], 'read_only': True,
                    'unsupported': ['push', 'create', 'delete', 'merge']}}}}

    def connect(self, owner, token, repositories):
        # Fine-grained tokens allow selected repositories and read-only Contents.
        if not isinstance(token, str) or not re.fullmatch(r'github_pat_[A-Za-z0-9_]{20,250}', token):
            raise GitHubError('Use a fine-grained GitHub token with Contents: Read-only.')
        if not isinstance(repositories, list) or not 1 <= len(repositories) <= 20:
            raise GitHubError('Select between 1 and 20 repositories as owner/name.')
        repos = sorted(set(repositories)) if all(isinstance(r, str) for r in repositories) else []
        if not repos or any(not _REPO.fullmatch(r) or any(p in ('.', '..') for p in r.split('/')) for r in repos):
            raise GitHubError('Repositories must use owner/name.')
        sealed = connectors.seal_tokens({'access_token': token})
        for repo in repos:
            meta = self.transport(token, '/repos/' + repo)
            if str(meta.get('full_name', '')).lower() != repo.lower():
                raise GitHubError('GitHub repository identity could not be verified.')
            # Verify Contents access, not merely public metadata access.
            self.transport(token, '/repos/' + repo + '/contents')
        with _LOCK:
            data = self._load()
            data[connectors.ConnectorStore._owner_key(owner)] = {
                'sealed': sealed, 'repositories': repos, 'connected_at': time.time()}
            self._save(data)
        return self.view(owner)

    def disconnect(self, owner):
        with _LOCK:
            data = self._load()
            data.pop(connectors.ConnectorStore._owner_key(owner), None)
            self._save(data)
        return self.view(owner)

    def read(self, owner, action='status', repository='', path='', ref=''):
        if action not in ('status', 'repositories', 'contents'):
            raise GitHubError('Supported GitHub reads: status, repositories, contents.')
        if action == 'status':
            return self.view(owner)
        with _LOCK:
            rec = self._load().get(connectors.ConnectorStore._owner_key(owner), {})
        token = connectors.unseal_tokens(rec.get('sealed')).get('access_token')
        if not token:
            raise GitHubError('Connect GitHub in Connections before reading private repositories.')
        repos = rec.get('repositories', [])
        if action == 'repositories':
            return {'repositories': repos, 'read_only': True}
        if repository.lower() not in [r.lower() for r in repos]:
            raise GitHubError('This repository was not selected in your GitHub connection.')
        if not isinstance(path, str) or len(path) > 1000 or path.startswith('/') or any(p in ('.', '..') for p in path.split('/')) or '\\' in path or any(ord(c) < 32 for c in path):
            raise GitHubError('Use a repository-relative file or directory path.')
        if not isinstance(ref, str) or len(ref) > 200 or any(ord(c) < 32 for c in ref):
            raise GitHubError('Invalid GitHub branch or commit ref.')
        result = self.transport(token, '/repos/' + repository + '/contents/' + urllib.parse.quote(path, safe='/'), {'ref': ref} if ref else None)
        if isinstance(result, list):
            return {'repository': repository, 'path': path, 'ref': ref or 'default branch',
                    'entries': [{'path': e.get('path'), 'type': e.get('type'), 'size': e.get('size')} for e in result[:200]],
                    'truncated': len(result) > 200, 'untrusted_repository_content': True}
        if result.get('type') != 'file' or result.get('encoding') != 'base64' or result.get('size', MAX_BYTES + 1) > MAX_BYTES:
            raise GitHubError('Only text files up to 256 KB can be read. Symlinks and submodules are unsupported.')
        try:
            raw = base64.b64decode(result.get('content', ''), validate=False)
            if len(raw) > MAX_BYTES or len(raw) != result.get('size') or b'\0' in raw:
                raise ValueError()
            content = raw.decode('utf-8')
        except (ValueError, UnicodeError):
            raise GitHubError('This file is binary, too large, or not readable UTF-8 text.') from None
        return {'repository': repository, 'path': path, 'ref': ref or 'default branch',
                'sha': result.get('sha'), 'content': content[:40_000], 'truncated': len(content) > 40_000,
                'untrusted_repository_content': True}
