import json
import html
import re
import urllib.parse
from fastapi import FastAPI
from fastapi.testclient import TestClient
import connections_api
from test_github_app import setup, BASE, state, SECRET, TOKEN, REFRESH


def client_for(monkeypatch, f, c):
    monkeypatch.setattr(connections_api, '_github_flow', lambda: f)
    monkeypatch.setattr(connections_api, '_github', lambda: c)
    monkeypatch.setattr(connections_api, '_require_user', lambda req: req.headers.get('x-owner', 'alice'))
    monkeypatch.setattr(connections_api, '_github_origin', lambda req: BASE)
    app = FastAPI(); app.include_router(connections_api.router)
    return TestClient(app, base_url=BASE)


def test_mobile_start_register_install_authorize_finish(setup, monkeypatch):
    f, c, _, _ = setup
    client = client_for(monkeypatch, f, c)
    url = client.post('/api/connections/github/connect').json()['authorization_url']
    page = client.get(url)
    assert page.status_code == 200 and 'Continue on GitHub' in page.text
    assert "form-action https://github.com" in page.headers['content-security-policy']
    manifest = json.loads(html.unescape(re.search(r"name='manifest' value='([^']+)'", page.text)[1]))
    assert manifest['default_permissions'] == {'contents': 'read', 'metadata': 'read'}
    registered = client.get('/api/connections/github/registered', params={'state': state(url), 'code': 'reg'}, follow_redirects=False)
    assert registered.status_code == 303
    install = registered.headers['location']
    installed = client.get('/api/connections/github/installed', params={'state': state(install), 'installation_id': '7'}, follow_redirects=False)
    oauth = installed.headers['location']
    assert installed.status_code == 303 and '/login/oauth/authorize?' in oauth
    callback = client.get('/api/connections/github/callback', params={'state': state(oauth), 'code': 'auth'})
    assert 'GitHub connected' in callback.text and c.view('alice')['connected']
    assert callback.headers['cache-control'] == 'no-store'
    assert callback.headers['referrer-policy'] == 'no-referrer'
    for secret in (SECRET, TOKEN, REFRESH):
        assert secret not in page.text + registered.text + installed.text + callback.text


def test_wrong_owner_and_replay_are_generic_pages(setup, monkeypatch):
    f, c, _, calls = setup
    client = client_for(monkeypatch, f, c)
    url = client.post('/api/connections/github/connect').json()['authorization_url']
    page = client.get(url, headers={'x-owner': 'bob'})
    assert 'expired' in page.text and 'manifest' not in page.text
    fail = client.get('/api/connections/github/registered', params={'state': state(url), 'code': 'secret-code'}, headers={'x-owner': 'bob'})
    assert 'secret-code' not in fail.text and not calls
    success = client.get('/api/connections/github/registered', params={'state': state(url), 'code': 'reg'}, follow_redirects=False)
    assert success.status_code == 303
    fail = client.get('/api/connections/github/registered', params={'state': state(url), 'code': 'reg'})
    assert 'expired' in fail.text


def test_reads_use_live_selected_repositories(setup, monkeypatch):
    import github_app
    from test_github_app import connect
    from github_connection import GitHubError
    import pytest
    f, c, mode, _ = setup; connect(f)
    monkeypatch.setattr(github_app, 'GitHubAppFlow', lambda **kwargs: f)
    assert c.read('alice', 'contents', 'alice/private', 'README.md')['content'] == 'hello'
    mode['repos'] = ['alice/second']
    with pytest.raises(GitHubError, match='not selected'): c.read('alice', 'contents', 'alice/private', 'README.md')
    assert c.read('alice', 'repositories')['repositories'] == ['alice/second']
