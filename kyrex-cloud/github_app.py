"""GitHub App registration, owner-bound consent, and expiring user credentials.

App manifests remove manual credential setup. Only GitHub's fixed registration,
OAuth exchange, and read endpoints are used. No installation token or app private
key is retained: reads always act as the authorizing GitHub user.
"""
from __future__ import annotations
import base64
import hashlib
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
import connectors
from github_connection import GitHubConnection, GitHubError, _LOCK, _NoRedirect, request

TTL = 15 * 60
PREFIX = '/api/connections/github'


def post_json(url, values=None):
    """Fixed manifest/OAuth POSTs; never follow a credential-bearing redirect."""
    if not (url == 'https://github.com/login/oauth/access_token' or
            re.fullmatch(r'https://api.github.com/app-manifests/[A-Za-z0-9_-]{1,200}/conversions', url)):
        raise GitHubError('Unsupported GitHub authorization endpoint.')
    body = urllib.parse.urlencode(values or {}).encode()
    req = urllib.request.Request(url, data=body, headers={
        'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded',
        'User-Agent': 'Kyrex-ReadOnly'}, method='POST')
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=20) as resp:
            raw = resp.read(256001)
            if len(raw) > 256000:
                raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get('error'):
                raise ValueError()
            return result
    except (OSError, ValueError):
        raise GitHubError('GitHub authorization could not be completed. Tap Connect to try again.') from None


def origin(value):
    u = urllib.parse.urlsplit(value)
    if u.scheme != 'https' or not u.netloc or u.username or u.password or u.path not in ('', '/') or u.query or u.fragment:
        raise GitHubError('GitHub connection requires the public HTTPS Chat origin.')
    return value.rstrip('/')


class GitHubAppFlow:
    def __init__(self, path=None, transport=None, post=None, connection=None):
        self.path = Path(path) if path else connectors.data_dir() / 'github_apps.json'
        self.transport = transport or request
        self.post = post or post_json
        self.connection = connection or GitHubConnection()

    def _load(self):
        if not self.path.exists():
            return {'apps': {}, 'states': {}, 'generations': {}}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data.get('apps'), dict) or not isinstance(data.get('states'), dict):
                raise ValueError()
            data.setdefault('generations', {})
            return data
        except (OSError, ValueError, AttributeError):
            raise GitHubError('GitHub connection setup storage is unavailable.') from None

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def config(self, owner):
        with _LOCK:
            blob = self._load()['apps'].get(connectors.ConnectorStore._owner_key(owner))
        return connectors.unseal_tokens(blob)

    def _state(self, owner, base, kind, **extra):
        connectors._box()  # No pending registration without encrypted storage.
        base = origin(base)
        state = secrets.token_urlsafe(32)
        with _LOCK:
            data = self._load()
            data['states'] = {k: v for k, v in data['states'].items() if v.get('expires', 0) > time.time()}
            # One active handoff per owner/kind prevents abandoned states accumulating.
            key = connectors.ConnectorStore._owner_key(owner)
            data['states'] = {k: v for k, v in data['states'].items() if not (v.get('owner') == key and v.get('kind') == kind)}
            data['states'][hashlib.sha256(state.encode()).hexdigest()] = {
                'owner': key, 'base': base, 'kind': kind, 'expires': time.time() + TTL,
                'generation': data['generations'].get(key, 0), **extra}
            self._save(data)
        return state

    def _transaction(self, owner, state, base, kind, consume=True):
        if not isinstance(state, str) or not 20 <= len(state) <= 200:
            raise GitHubError('GitHub sign-in expired. Tap Connect to try again.')
        with _LOCK:
            data = self._load()
            key = hashlib.sha256(state.encode()).hexdigest()
            rec = data['states'].get(key, {})
            if (rec.get('owner') != connectors.ConnectorStore._owner_key(owner) or rec.get('base') != origin(base)
                    or rec.get('kind') != kind or rec.get('expires', 0) <= time.time()):
                raise GitHubError('GitHub sign-in expired or did not match this account. Tap Connect to try again.')
            if consume:
                del data['states'][key]
                self._save(data)
            return rec

    def cancel(self, owner):
        with _LOCK:
            data = self._load()
            key = connectors.ConnectorStore._owner_key(owner)
            data['states'] = {k: v for k, v in data['states'].items() if v.get('owner') != key}
            data['generations'][key] = data['generations'].get(key, 0) + 1
            self._save(data)

    def begin(self, owner, base, manage=False):
        if self.config(owner):
            url = self.install(owner, base) if manage else self.authorize(owner, base)
        else:
            state = self._state(owner, base, 'manifest')
            url = origin(base) + PREFIX + '/setup?' + urllib.parse.urlencode({'state': state})
        return {'authorization_url': url, 'provider': 'github'}

    def manifest(self, owner, state, base):
        self._transaction(owner, state, base, 'manifest', consume=False)
        base = origin(base)
        return {'name': 'Kyrex Repository Reader ' + secrets.token_hex(3), 'url': base,
                'description': 'Read selected repositories in Kyrex Chat.', 'public': False,
                'hook_attributes': {'url': base + PREFIX + '/webhook', 'active': False},
                'redirect_url': base + PREFIX + '/registered',
                'callback_urls': [base + PREFIX + '/callback'],
                'setup_url': base + PREFIX + '/installed', 'setup_on_update': True,
                'request_oauth_on_install': False,
                'default_permissions': {'contents': 'read', 'metadata': 'read'}, 'default_events': []}

    def registered(self, owner, state, base, code):
        tx = self._transaction(owner, state, base, 'manifest')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', str(code or '')):
            raise GitHubError('GitHub app registration was not completed.')
        app = self.post('https://api.github.com/app-manifests/' + code + '/conversions')
        if str((app.get('owner') or {}).get('login', '')).lower() != owner.lower():
            raise GitHubError('Create the GitHub app using the same GitHub account as Kyrex.')
        if app.get('permissions') != {'contents': 'read', 'metadata': 'read'}:
            raise GitHubError('The GitHub app must request only read-only Contents and Metadata.')
        if not (isinstance(app.get('id'), int) and re.fullmatch(r'[A-Za-z0-9-]+', app.get('slug', ''))
                and app.get('client_id') and app.get('client_secret')):
            raise GitHubError('GitHub app registration returned incomplete configuration.')
        # Discard the private key/webhook secret. User access tokens need neither.
        safe = {k: app[k] for k in ('id', 'slug', 'client_id', 'client_secret')}
        safe['base'] = origin(base)
        with _LOCK:
            data = self._load()
            self._check_generation(data, tx)
            data['apps'][connectors.ConnectorStore._owner_key(owner)] = connectors.seal_tokens(safe)
            self._save(data)
            return self.install(owner, base)

    def install(self, owner, base):
        app = self.config(owner)
        if not app or app.get('base') != origin(base):
            raise GitHubError('GitHub app configuration does not match this Chat address.')
        state = self._state(owner, base, 'install')
        return 'https://github.com/apps/' + app['slug'] + '/installations/new?' + urllib.parse.urlencode({'state': state})

    def installed(self, owner, state, base, installation_id):
        tx = self._transaction(owner, state, base, 'install')
        if not re.fullmatch(r'[1-9][0-9]{0,19}', str(installation_id or '')):
            raise GitHubError('GitHub repository selection was not completed.')
        # This id is only a hint until the live user/installations API verifies it.
        with _LOCK:
            self._check_generation(self._load(), tx)
            return self.authorize(owner, base, int(installation_id))

    def authorize(self, owner, base, installation_id=None):
        app = self.config(owner)
        if not app or app.get('base') != origin(base):
            raise GitHubError('GitHub app configuration does not match this Chat address.')
        verifier = secrets.token_urlsafe(48)
        state = self._state(owner, base, 'oauth', installation_id=installation_id,
                            verifier=connectors.seal_tokens({'verifier': verifier}))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        return 'https://github.com/login/oauth/authorize?' + urllib.parse.urlencode({
            'client_id': app['client_id'], 'redirect_uri': origin(base) + PREFIX + '/callback',
            'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})

    @staticmethod
    def token_payload(result):
        token = result.get('access_token')
        if not isinstance(token, str) or not token.startswith('ghu_'):
            raise GitHubError('GitHub did not return an app authorization. Tap Connect again.')
        payload = {'access_token': token, 'refresh_token': result.get('refresh_token')}
        for source, dest in (('expires_in', 'expires_at'), ('refresh_token_expires_in', 'refresh_expires_at')):
            if source in result:
                try:
                    seconds = int(result[source])
                    if seconds <= 0: raise ValueError()
                    payload[dest] = time.time() + seconds
                except (ValueError, TypeError):
                    raise GitHubError('GitHub returned invalid authorization expiry.') from None
        return payload

    def _installations(self, token, app_id):
        result = self.transport(token, '/user/installations', {'per_page': 100})
        if result.get('total_count', 0) > 100:
            raise GitHubError('Too many GitHub installations. Contact the Kyrex administrator.')
        return [v for v in result.get('installations', []) if v.get('app_id') == app_id and not v.get('suspended_at')
                and v.get('permissions') == {'contents': 'read', 'metadata': 'read'}]

    def repositories(self, token, installation_id):
        repos = []
        for page in range(1, 6):
            result = self.transport(token, f'/user/installations/{installation_id}/repositories', {'per_page': 100, 'page': page})
            if result.get('total_count', 0) > 500:
                raise GitHubError('Select at most 500 repositories for Kyrex on GitHub.')
            rows = result.get('repositories', [])
            repos.extend(v['full_name'] for v in rows if re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(v.get('full_name', ''))))
            if len(rows) < 100: break
        return sorted(set(repos))

    @staticmethod
    def _check_generation(data, tx):
        if data['generations'].get(tx['owner'], 0) != tx.get('generation', 0):
            raise GitHubError('GitHub connection was cancelled. Tap Connect to start again.')

    def callback(self, owner, state, base, code):
        tx = self._transaction(owner, state, base, 'oauth')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', str(code or '')):
            raise GitHubError('GitHub authorization was cancelled or did not complete.')
        app = self.config(owner)
        verifier = connectors.unseal_tokens(tx['verifier']).get('verifier')
        payload = self.token_payload(self.post('https://github.com/login/oauth/access_token', {
            'client_id': app['client_id'], 'client_secret': app['client_secret'], 'code': code,
            'redirect_uri': origin(base) + PREFIX + '/callback', 'code_verifier': verifier}))
        token = payload['access_token']
        user = self.transport(token, '/user')
        if str(user.get('login', '')).lower() != owner.lower():
            raise GitHubError('Authorize with the same GitHub account you use for Kyrex.')
        installations = self._installations(token, app['id'])
        hint = tx.get('installation_id')
        if hint is not None:
            installations = [v for v in installations if v.get('id') == hint]
            if not installations:
                raise GitHubError('The selected GitHub installation could not be verified for your account.')
        if len(installations) != 1:
            with _LOCK:
                self._check_generation(self._load(), tx)
                return {'next_url': self.install(owner, base)}
        installation = installations[0]['id']
        repos = self.repositories(token, installation)
        if not repos:
            raise GitHubError('Choose at least one repository for Kyrex on GitHub.')
        with _LOCK:
            self._check_generation(self._load(), tx)
            self.connection.save_app_connection(owner, payload, installation, app['id'], repos)
            return {'connected': True}

    def credentials(self, owner, rec):
        """Refresh expired user tokens and verify live installation/repo selection."""
        payload = connectors.unseal_tokens(rec.get('sealed'))
        app = self.config(owner)
        if not app or app.get('id') != rec.get('app_id'):
            raise GitHubError('Reconnect GitHub in Connections.')
        if payload.get('expires_at', float('inf')) <= time.time() + 60:
            if not payload.get('refresh_token') or payload.get('refresh_expires_at', 0) <= time.time():
                raise GitHubError('GitHub authorization expired. Reconnect in Connections.')
            result = self.post('https://github.com/login/oauth/access_token', {
                'client_id': app['client_id'], 'client_secret': app['client_secret'],
                'grant_type': 'refresh_token', 'refresh_token': payload['refresh_token']})
            payload = self.token_payload(result)
            self.connection.update_app_credentials(owner, rec, payload)
        token = payload.get('access_token')
        if not token:
            raise GitHubError('Reconnect GitHub in Connections.')
        installations = self._installations(token, app['id'])
        if not any(v.get('id') == rec['installation_id'] for v in installations):
            raise GitHubError('GitHub repository access was removed. Reconnect in Connections.')
        return token, self.repositories(token, rec['installation_id'])
