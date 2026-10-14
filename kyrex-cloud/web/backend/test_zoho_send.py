"""Real encrypted/source-bound stores, fake Zoho HTTPS only. No live emails."""
import hashlib
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import agent_inbox
import connectors as core
import email_send
import zoho_send as zoho
import zoho_send_api as api
import email_inbox_api
import connections_api
from test_zoho_inbox import Fake, EMAIL, PASSWORD

SECRET = 'test-zoho-client-secret'
TOKEN = 'test-access-token'
REFRESH = 'test-refresh-token'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'test-zoho-https')
    monkeypatch.setenv('ZOHO_CLIENT_ID', 'test-zoho-client-id')
    monkeypatch.setenv('ZOHO_CLIENT_SECRET', SECRET)
    monkeypatch.setenv('ZOHO_REDIRECT_URI', 'https://chat.example.com/api/connections/zoho/callback')
    inbox = agent_inbox.AgentInboxStore(tmp_path / 'inbox.db')
    fake = Fake()
    inbox.zoho_factory = fake.factory
    inbox.connect_zoho('alice', EMAIL, PASSWORD, 'imappro.zoho.com')
    fake.headers[20] = b'From: Office <office@example.com>\r\nReply-To: bookings@example.com\r\nSubject: Appointment\r\nMessage-ID: <parent@office.example>\r\n\r\n'
    calls, behavior = [], {}
    def request(method, url, **kw):
        calls.append((method, url, kw))
        if url == zoho.AUTH + 'token':
            if behavior.get('token_error'): return 400, {'error': SECRET}
            if kw['data']['grant_type'] == 'refresh_token':
                assert kw['data']['refresh_token'] == REFRESH
                return 200, {'access_token': 'fresh-test-token', 'expires_in': 3600}
            return 200, {'access_token': TOKEN, 'refresh_token': REFRESH, 'expires_in': 3600,
                         'api_domain': 'https://attacker.example.com'}
        if url == zoho.API + 'accounts':
            return 200, {'status': {'code': 200}, 'data': [{'type': 'ZOHO_ACCOUNT',
                'primaryEmailAddress': behavior.get('account', EMAIL), 'mailboxStatus': 'enabled', 'accountId': '123456'}]}
        if url.endswith('/messages/search'):
            if kw['params']['limit'] == 1: return 200, {'status': {'code': 200}, 'data': []}
            return 200, {'status': {'code': 200}, 'data': [{'messageId': '222', 'folderId': '333'}]}
        if url.endswith('/header'):
            raw = fake.headers[20].decode()
            if behavior.get('wrong_parent'): raw = raw.replace('<parent@office.example>', '<other@example.com>')
            if behavior.get('wrong_recipient'): raw = raw.replace('bookings@example.com', 'other@example.com')
            return 200, {'status': {'code': 200}, 'data': {'headerContent': raw}}
        if url.endswith('/originalmessage'):
            return 200, {'status': {'code': 200}, 'data': {'content': f'From: {EMAIL}\r\nMessage-ID: <actual@zoho.example>\r\n\r\nBody'}}
        if method == 'POST' and '/messages' in url:
            if behavior.get('send_callback'): behavior['send_callback']()
            if behavior.get('drop'): raise TimeoutError(SECRET)
            if behavior.get('status'): return behavior['status'], {'status': {'code': behavior['status']}}
            return 200, {'status': {'code': 200}, 'data': {'messageId': '987654', 'mailId': '<actual@zoho.example>'}}
        raise AssertionError(url)
    store = zoho.SendStore(inbox, request)
    monkeypatch.setattr(agent_inbox, 'default_store', lambda: inbox)
    monkeypatch.setattr(zoho, 'http', request)
    monkeypatch.setattr(connections_api, '_require_user', lambda r: r.headers.get('x-test-owner', 'alice'))
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(email_inbox_api.router)
    client = TestClient(app)
    return store, inbox, fake, calls, behavior, client, tmp_path


def authorize(setup):
    store = setup[0]
    value = store.begin('alice')
    state = parse_qs(urlsplit(value['authorization_url']).query)['state'][0]
    store.complete('alice', state, 'test-code')
    return state


def draft(setup, **kw):
    drafts = email_send.DraftStore(setup[-1] / 'draft.db', setup[1])
    return drafts, drafts.prepare('alice', 'conversation', 'office@example.com', 'Appointment', 'Exact reviewed body 🌎', **kw)


def test_consent_exact_scopes_encrypted_tokens_and_owner_isolation(setup):
    store, inbox, _, calls, _, client, path = setup
    assert not store.view('alice')['connected'] and store.view('alice')['configured']
    started = store.begin('alice')
    query = parse_qs(urlsplit(started['authorization_url']).query)
    assert query['scope'] == [','.join(zoho.SCOPES)] and query['access_type'] == ['offline']
    assert 'ALL' not in query['scope'][0] and 'DELETE' not in query['scope'][0]
    state = query['state'][0]
    with pytest.raises(core.OAuthStateError): store.complete('bob', state, 'test-code')
    store.complete('alice', state, 'test-code')
    assert store.view('alice')['connected'] and not store.view('bob')['connected']
    for secret in (TOKEN, REFRESH, SECRET, 'test-code', state):
        assert secret.encode() not in inbox.path.read_bytes()
        assert secret not in json.dumps(client.get('/api/connections/zoho').json())
    assert all(url.startswith((zoho.AUTH, zoho.API)) for _, url, _ in calls)
    assert not any('/messages' in url and method == 'POST' for method, url, _ in calls)
    with pytest.raises(core.OAuthStateError): store.complete('alice', state, 'test-code')
    assert inbox.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('failure', ['expired', 'redirect', 'source', 'account', 'exchange'])
def test_failed_consent_cannot_enable_sending(setup, monkeypatch, failure):
    store, inbox, _, calls, behavior, _, _ = setup
    state = parse_qs(urlsplit(store.begin('alice')['authorization_url']).query)['state'][0]
    if failure == 'expired':
        with inbox.db() as db: db.execute('UPDATE zoho_send_states SET created=0')
    if failure == 'redirect': monkeypatch.setenv('ZOHO_REDIRECT_URI', 'https://other.example.com/api/connections/zoho/callback')
    if failure == 'source': inbox.select('alice', 'none')
    if failure == 'account': behavior['account'] = 'admin@example.com'
    if failure == 'exchange': behavior['token_error'] = True
    with pytest.raises(core.ConnectorError): store.complete('alice', state, 'test-code')
    assert not store.view('alice')['connected']
    assert not any('/messages' in url and method == 'POST' for method, url, _ in calls)


def test_default_sender_https_exact_review_once_and_reply_identity(setup):
    authorize(setup)
    store, inbox, fake, calls, _, _, _ = setup
    drafts, view = draft(setup)
    assert not [c for c in calls if c[0] == 'POST' and '/messages' in c[1]]
    result = drafts.decide('alice', view['id'], view['revision'], 'send')
    assert result['state'] == 'accepted'
    drafts.decide('alice', view['id'], view['revision'], 'send')
    sent = [c for c in calls if c[0] == 'POST' and '/messages' in c[1]]
    assert len(sent) == 1 and sent[0][1] == zoho.API + 'accounts/123456/messages'
    assert sent[0][2]['body'] == {'fromAddress': EMAIL, 'toAddress': 'office@example.com', 'subject': 'Appointment',
        'content': 'Exact reviewed body 🌎', 'mailFormat': 'plaintext', 'encoding': 'UTF-8', 'askReceipt': 'no'}
    with drafts.db() as db: payload = core.unseal_tokens(drafts._row('alice', view['id'], db)['sealed'])
    assert payload['zoho_mail_id'] == '<actual@zoho.example>'
    assert payload['zoho_message_id'] == '987654'
    assert store.thread_id('alice', payload) == '<actual@zoho.example>'
    assert drafts.replies('alice', view['id'])['status'] == 'no_reply'
    assert any('SEARCH' in str(c) and 'actual@zoho.example' in str(c) for c in fake.calls)
    assert SECRET not in json.dumps(result) and TOKEN not in json.dumps(result)


@pytest.mark.parametrize('behavior,state', [('drop', 'unknown'), ('500', 'unknown'), ('401', 'failed'), ('429', 'failed')])
def test_interrupted_or_rejected_post_never_retries(setup, behavior, state):
    authorize(setup)
    if behavior == 'drop': setup[4]['drop'] = True
    else: setup[4]['status'] = int(behavior)
    drafts, view = draft(setup)
    assert drafts.decide('alice', view['id'], view['revision'], 'send')['state'] == state
    drafts.decide('alice', view['id'], view['revision'], 'send')
    assert len([c for c in setup[3] if c[0] == 'POST' and '/messages' in c[1]]) == 1


@pytest.mark.parametrize('failure', ['missing', 'disabled', 'source', 'refresh'])
def test_pre_submission_failure_does_not_send(setup, failure):
    if failure != 'missing': authorize(setup)
    store, inbox, _, calls, behavior, _, _ = setup
    drafts, view = draft(setup)
    if failure == 'disabled': store.disconnect('alice')
    if failure == 'source': inbox.select('alice', 'none')
    if failure == 'refresh':
        with inbox.db() as db:
            blob = db.execute('SELECT sealed FROM zoho_send_grants').fetchone()[0]
            tokens = core.unseal_tokens(blob); tokens['expires_at'] = 0
            db.execute('UPDATE zoho_send_grants SET sealed=?', (core.seal_tokens(tokens),))
        behavior['token_error'] = True
    if failure == 'source':
        with pytest.raises(email_send.EmailSendError): drafts.decide('alice', view['id'], view['revision'], 'send')
    else:
        value = drafts.decide('alice', view['id'], view['revision'], 'send')
        assert value['state'] == 'failed' and SECRET not in json.dumps(value)
    assert not [c for c in calls if c[0] == 'POST' and '/messages' in c[1]]


def test_refresh_reuses_sealed_refresh_and_sent_metadata_fallback(setup):
    authorize(setup)
    store, inbox, _, calls, _, _, _ = setup
    with inbox.db() as db:
        tokens = core.unseal_tokens(db.execute('SELECT sealed FROM zoho_send_grants').fetchone()[0])
        tokens['expires_at'] = 0
        db.execute('UPDATE zoho_send_grants SET sealed=?', (core.seal_tokens(tokens),))
    drafts, view = draft(setup)
    assert drafts.decide('alice', view['id'], view['revision'], 'send')['state'] == 'accepted'
    send = [c for c in calls if c[0] == 'POST' and '/messages' in c[1]][0]
    assert send[2]['token'] == 'fresh-test-token'
    with drafts.db() as db: payload = core.unseal_tokens(drafts._row('alice', view['id'], db)['sealed'])
    payload.pop('zoho_mail_id')
    assert store.thread_id('alice', payload) == '<actual@zoho.example>'
    payload.pop('zoho_message_id')
    with pytest.raises(core.ConnectorError, match='will not guess'): store.thread_id('alice', payload)


@pytest.mark.parametrize('bad', [None, 'wrong_parent', 'wrong_recipient'])
def test_reply_resolves_api_id_by_verified_inbox_headers(setup, bad):
    authorize(setup)
    _, inbox, _, calls, behavior, _, _ = setup
    if bad: behavior[bad] = True
    parent = inbox.reader('alice')._id(setup[2].validity, 20)
    drafts, view = draft(setup, parent_id=parent)
    value = drafts.decide('alice', view['id'], view['revision'], 'send')
    sent = [c for c in calls if c[0] == 'POST' and '/messages' in c[1]]
    if bad:
        assert value['state'] == 'failed' and not sent
    else:
        assert value['state'] == 'accepted' and sent[0][1].endswith('/messages/222')
        assert sent[0][2]['body']['action'] == 'reply'
        assert sent[0][2]['body']['toAddress'] == 'bookings@example.com'
        assert 'ccAddress' not in sent[0][2]['body'] and 'bccAddress' not in sent[0][2]['body']


@pytest.mark.parametrize('disconnect', ['sending', 'source'])
def test_disconnect_serializes_with_submission_and_concurrent_approvals(setup, disconnect):
    authorize(setup)
    store, _, _, calls, behavior, _, _ = setup
    drafts, view = draft(setup)
    entered, release = threading.Event(), threading.Event()
    def hold(): entered.set(); assert release.wait(5)
    behavior['send_callback'] = hold
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(drafts.decide, 'alice', view['id'], view['revision'], 'send')
        assert entered.wait(3)
        disabled = pool.submit(store.disconnect, 'alice') if disconnect == 'sending' else pool.submit(setup[1].select, 'alice', 'none')
        repeated = pool.submit(drafts.decide, 'alice', view['id'], view['revision'], 'send')
        assert repeated.result(timeout=3)['state'] == 'sending'
        assert not disabled.done()
        release.set()
        assert first.result()['state'] == 'accepted'
        disabled.result()
        assert not store.view('alice')['connected']
    assert len([c for c in calls if c[0] == 'POST' and '/messages' in c[1]]) == 1


def test_api_origin_auth_fields_and_callback_secrecy(setup, monkeypatch):
    _, _, _, calls, _, client, _ = setup
    assert client.post('/api/connections/zoho/connect', json={}, headers={'origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/connections/zoho/connect', json={'scope': 'ALL'}).status_code == 400
    started = client.post('/api/connections/zoho/connect', json={}).json()
    state = parse_qs(urlsplit(started['authorization_url']).query)['state'][0]
    bad = client.get('/api/connections/zoho/callback', params={'state': state, 'code': SECRET}, headers={'x-test-owner': 'bob'})
    assert bad.status_code == 409 and SECRET not in bad.text
    good = client.get('/api/connections/zoho/callback', params={'state': state, 'code': 'test-code'})
    assert good.status_code == 200 and good.headers['referrer-policy'] == 'no-referrer'
    assert good.headers['cache-control'] == 'no-store' and state not in good.text
    assert client.get('/api/connections/email-inbox').json()['sending']['connected']
    assert not [c for c in calls if c[0] == 'POST' and '/messages' in c[1]]
    monkeypatch.setattr(connections_api, '_require_user', lambda r: (_ for _ in ()).throw(HTTPException(401)))
    assert client.get('/api/connections/zoho').status_code == 401


def test_transport_tls_fixed_origins_no_redirect_and_bounded_response(monkeypatch):
    seen = {}
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def iter_content(self, size): yield json.dumps({'status': {'code': 200}}).encode()
    def request(method, url, **kw): seen.update(kw); return Response()
    monkeypatch.setattr(zoho.requests, 'request', request)
    assert zoho.http('GET', zoho.API + 'accounts', token=TOKEN)[0] == 200
    assert seen['allow_redirects'] is False and seen['timeout'] == (5, 20) and 'verify' not in seen
    monkeypatch.setattr(Response, 'iter_content', lambda self, size: iter([b'x' * (zoho.MAX_RESPONSE + 1)]))
    with pytest.raises(core.ConnectorError): zoho.http('GET', zoho.API + 'accounts', token=TOKEN)


def test_process_loss_preserves_https_transport_and_never_guesses_reply_id(setup):
    authorize(setup)
    drafts, view = draft(setup)
    def crash(*args): raise SystemExit('process lost')
    drafts.sender = crash
    with pytest.raises(SystemExit): drafts.decide('alice', view['id'], view['revision'], 'send')
    with drafts.db() as db:
        row = drafts._row('alice', view['id'], db)
        assert row['state'] == 'sending'
        assert core.unseal_tokens(row['sealed'])['transport'] == 'zoho_https'
    with pytest.raises(core.ConnectorError, match='will not guess'): drafts.replies('alice', view['id'])
    assert drafts.decide('alice', view['id'], view['revision'], 'send')['state'] == 'sending'


def test_identity_reports_sending_authorization_without_reading_mail(setup):
    import email_identity
    assert not email_identity.current('alice')['can_send']
    assert 'Authorize Zoho sending' in email_identity.answer('alice')
    authorize(setup)
    before = len(setup[3])
    assert email_identity.current('alice')['can_send']
    assert 'when you press Send email' in email_identity.answer('alice')
    assert len(setup[3]) == before
    setup[0].disconnect('alice')
    assert not email_identity.current('alice')['can_send']


@pytest.mark.parametrize('change', ['disconnect', 'source', 'reconnect'])
def test_inbox_change_removes_local_oauth_material_and_pending_consent(setup, change):
    authorize(setup)
    store, inbox, _, calls, _, _, _ = setup
    state = parse_qs(urlsplit(store.begin('alice')['authorization_url']).query)['state'][0]
    with inbox.db() as db:
        db.execute('INSERT INTO zoho_send_grants VALUES (?,?,?)', ('bob', 'other-source', core.seal_tokens({'refresh_token': 'bobs-test-token'})))
    if change == 'disconnect': inbox.disconnect('alice')
    if change == 'source': inbox.select('alice', 'gmail')
    if change == 'reconnect': inbox.connect_zoho('alice', EMAIL, '', 'imappro.zoho.com')
    with inbox.db() as db:
        assert not db.execute('SELECT 1 FROM zoho_send_grants WHERE owner=?', ('alice',)).fetchone()
        assert not db.execute('SELECT 1 FROM zoho_send_states WHERE owner=?', ('alice',)).fetchone()
        assert db.execute('SELECT 1 FROM zoho_send_grants WHERE owner=?', ('bob',)).fetchone()
    before = len(calls)
    with pytest.raises(core.OAuthStateError): store.complete('alice', state, 'test-code')
    assert len(calls) == before and not store.view('alice')['connected']
