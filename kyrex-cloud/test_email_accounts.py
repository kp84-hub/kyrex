"""Multi-mailbox admission, isolation, paging and revocation; fake providers only."""
import base64
import json
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_inbox
import connectors as core
import email_accounts as mail

REDIRECT = 'https://chat.example/api/connections/google/callback'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'mailbox-tests-only-encryption')
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'mailbox-test-client')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'mailbox-test-client-credential')
    monkeypatch.setenv('GOOGLE_REDIRECT_URI', REDIRECT)
    legacy = agent_inbox.AgentInboxStore(tmp_path/'legacy.sqlite3')
    registry = mail.EmailAccounts(tmp_path/'accounts.sqlite3')
    google = core.ConnectorStore(tmp_path/'calendar.json')
    monkeypatch.setattr(agent_inbox, 'default_store', lambda:legacy)
    monkeypatch.setattr(mail, 'default_store', lambda:registry)
    monkeypatch.setattr(core, 'default_store', lambda:google)
    calls = []
    hook = [None]

    def exchange(code, redirect, client):
        assert redirect == REDIRECT
        return {'access_token':'access-for-'+code, 'refresh_token':'refresh-for-'+code,
                'expires_in':3600, 'scope':core.GOOGLE_GMAIL_READ_SCOPE}

    def transport(method, url, token, params=None, **kwargs):
        assert method == 'GET'
        name = token.removeprefix('access-for-')
        calls.append((name, url, params))
        if hook[0]:
            fn, hook[0] = hook[0], None
            fn()
        if url.endswith('/profile'):
            return {'emailAddress':name+'@example.com'}
        if url.endswith('/messages'):
            page = (params or {}).get('pageToken', '')
            assert not page or page.startswith(name + '-')
            index = int(page.split('-')[-1]) if page else 0
            # Both accounts intentionally use identical provider message IDs.
            return {'messages':[{'id':f'm{index}', 'threadId':'shared-thread'}],
                    'nextPageToken':name+'-'+str(index+1) if index < 2 else ''}
        native = url.split('/messages/')[-1]
        return {'id':native,'threadId':'shared-thread', 'snippet':name+' preview',
                'payload':{'mimeType':'text/plain','headers':[
                    {'name':'Subject','value':name+' receipt'}, {'name':'From','value':'billing@example.com'},
                    {'name':'Date','value':'Tue, 27 Oct 2026 09:00:00 +0000'}],
                    'body':{'data':base64.urlsafe_b64encode((name+' subscription receipt').encode()).decode()}}}

    def connect(name, owner='alice', account=None):
        started = registry.begin_gmail(owner, account, REDIRECT)
        state = parse_qs(urlsplit(started['authorization_url']).query)['state'][0]
        registry.complete_gmail(state, name, redirect_uri=REDIRECT, exchange=exchange, transport=transport)
        return started['account_id']
    return SimpleNamespace(registry=registry, legacy=legacy, google=google, connect=connect,
                           transport=transport, exchange=exchange, calls=calls, hook=hook, root=tmp_path)


def test_two_gmail_accounts_preserve_calendar_and_dedicated_inbox(setup):
    s = setup
    started = s.google.begin_oauth('alice')
    s.google.complete_oauth('alice', started['state'], 'calendar', exchange=lambda *a:{'access_token':'calendar-test-only'})
    calendar_before = s.google.path.read_bytes()
    with s.legacy.db() as db:
        db.execute('INSERT INTO inboxes(owner,source,inbox,address,sealed,revision,provider,host) VALUES (?,?,?,?,?,?,?,?)',
            ('alice','zoho','primary@example.com','primary@example.com',core.seal_tokens({'password':'dedicated-test-only'}),'original-inbox-revision','zoho','imappro.zoho.com'))
    inbox_before = s.legacy.record('alice')
    first, second = s.connect('first'), s.connect('second')
    assert first != second
    assert s.registry.selection('alice') == 'legacy', 'Adding read grants never changes the chosen source'
    assert s.legacy.record('alice') == inbox_before
    assert s.google.path.read_bytes() == calendar_before
    assert s.registry.view('bob')['accounts'] == [{'id':'legacy','provider':'gmail','address':'',
        'status':'legacy','label':'Existing email reader','read_only':True}]
    text = json.dumps(s.registry.view('alice'))
    assert 'first@example.com' in text and 'second@example.com' in text
    for forbidden in ('access-for-', 'refresh-for-', 'sealed', 'dedicated-test-only', 'credential'):
        assert forbidden not in text
    s.registry.disconnect('alice', first)
    assert s.google.path.read_bytes() == calendar_before and s.legacy.record('alice') == inbox_before
    assert s.registry._record('alice', second)['status'] == 'connected'


def test_new_gmail_oauth_is_read_only_pkce_owner_bound_and_single_use(setup):
    s = setup
    start = s.registry.begin_gmail('alice', redirect_uri=REDIRECT)
    params = parse_qs(urlsplit(start['authorization_url']).query)
    assert params['scope'] == [core.GOOGLE_GMAIL_READ_SCOPE]
    assert params['code_challenge_method'] == ['S256']
    assert params['prompt'] == ['consent select_account']
    assert 'code_verifier' not in params
    state = params['state'][0]
    with pytest.raises(core.OAuthStateError):
        s.registry.complete_gmail(state, 'first', redirect_uri=REDIRECT+'/other', exchange=s.exchange)
    with pytest.raises(core.ConnectorError):
        s.registry._record('bob', start['account_id'])
    s.registry.complete_gmail(state, 'first', redirect_uri=REDIRECT, exchange=s.exchange, transport=s.transport)
    with pytest.raises(core.OAuthStateError):
        s.registry.complete_gmail(state, 'first', redirect_uri=REDIRECT, exchange=s.exchange, transport=s.transport)
    # A disconnected pending callback can never restore a grant.
    later = s.registry.begin_gmail('alice', start['account_id'], REDIRECT)
    state = parse_qs(urlsplit(later['authorization_url']).query)['state'][0]
    s.registry.disconnect('alice', start['account_id'])
    with pytest.raises(core.OAuthStateError):
        s.registry.complete_gmail(state, 'first', redirect_uri=REDIRECT, exchange=s.exchange, transport=s.transport)


def test_reconnect_verifies_mailbox_identity_and_duplicate_add_does_not_replace_grant(setup):
    s = setup
    first = s.connect('first')
    original = s.registry._record('alice', first)
    with pytest.raises(core.ConnectorError, match='already connected'):
        s.connect('first')
    assert s.registry._record('alice', first) == original
    with pytest.raises(core.ConnectorUnavailable, match='same email address'):
        s.connect('other', account=first)
    assert s.registry._record('alice', first) == original
    s.connect('first', account=first)
    assert s.registry._record('alice', first)['revision'] != original['revision']


def test_cross_account_colliding_ids_pages_and_full_bodies_remain_bound(setup):
    s = setup
    first, second = s.connect('first'), s.connect('second')
    s.registry.select('alice','all')
    reader = s.google.gmail('alice', transport=s.transport)
    page = reader.search(query='receipt', max_results=1)
    one = reader.read_message(page['messages'][0]['id'])
    assert one['mailbox'] == 'first@example.com' and one['body'].startswith('first')
    page2 = reader.search(query='receipt', max_results=1, page_token=page['next_page_token'])
    two = reader.read_message(page2['messages'][0]['id'])
    assert two['mailbox'] == 'second@example.com' and two['body'].startswith('second')
    assert one['id'] != two['id'] and one['thread_id'] != two['thread_id']
    with pytest.raises(core.ConnectorError, match='same email query'):
        reader.search(query='different', page_token=page['next_page_token'])
    bob = s.connect('bob', owner='bob')
    s.registry.select('bob', bob)
    with pytest.raises(core.ConnectorUnavailable):
        s.google.gmail('bob',transport=s.transport).read_message(one['id'])
    with pytest.raises(core.ConnectorUnavailable):
        s.google.gmail('bob',transport=s.transport).search(query='receipt',page_token=page['next_page_token'])
    s.registry.select('alice', second)
    with pytest.raises(core.ConnectorUnavailable, match='settings changed'):
        reader.read_message(one['id'])
    with pytest.raises(core.ConnectorUnavailable):
        s.google.gmail('alice',transport=s.transport).read_message(one['id'])
    assert s.registry._record('alice', first)['status']=='connected'


def test_combined_paging_does_not_drop_messages_and_narrowing_is_explicit(setup):
    s = setup
    s.connect('first'); s.connect('second'); s.registry.select('alice','all')
    reader = s.google.gmail('alice', transport=s.transport)
    hits, token = [], None
    while True:
        page = reader.search(query='receipt',max_results=1,page_token=token)
        hits += [reader.message(item['id']) for item in page['messages']]
        token = page['next_page_token']
        if not token: break
        assert len(hits)<10
    assert len(hits)==6
    assert {hit['headers']['Subject'] for hit in hits} == {'first receipt','second receipt'}
    with pytest.raises(core.ConnectorError, match='one mailbox'):
        reader.latest(query='receipt')
    scoped = reader.search(query='mailbox:second@example.com receipt')
    assert all(item['mailbox']=='second@example.com' for item in scoped['messages'])
    with pytest.raises(core.ConnectorError): reader.search(query='mailbox:not-connected@example.com receipt')
    # Native Gmail IDs cannot be interpreted against an arbitrary default.
    with pytest.raises(core.ConnectorError): reader.read_message('m0')


def test_disconnect_during_provider_read_discards_response_and_pause_has_no_fallback(setup):
    s = setup
    first=s.connect('first'); s.registry.select('alice',first)
    reader=s.google.gmail('alice',transport=s.transport)
    identity=reader.search()['messages'][0]['id']
    s.hook[0]=lambda:s.registry.disconnect('alice',first)
    with pytest.raises(core.ConnectorUnavailable, match='settings changed'):
        reader.read_message(identity)
    assert s.registry.selection('alice')=='none'
    assert not s.google.gmail_read_available('alice')
    with pytest.raises(core.ConnectorUnavailable): s.google.gmail('alice')


def test_provider_failure_does_not_switch_to_other_account(setup):
    s=setup
    first=s.connect('first'); second=s.connect('second'); s.registry.select('alice',first)
    calls=[]
    def failed(method,url,token,params=None):
        calls.append(token)
        raise core.ConnectorUnavailable('Provider unavailable')
    with pytest.raises(core.ConnectorUnavailable):
        s.google.gmail('alice',transport=failed).search()
    assert calls==['access-for-first']
    assert s.registry._record('alice',second)['status']=='connected'


def test_additional_zoho_uses_existing_adapter_without_replacing_primary(setup,monkeypatch):
    from test_zoho_inbox import Fake, EMAIL, PASSWORD
    s=setup
    original=s.registry._dedicated
    fake=Fake()
    def dedicated(account,credential):
        store=original(account,credential); store.zoho_factory=fake.factory; return store
    monkeypatch.setattr(s.registry,'_dedicated',dedicated)
    before=s.legacy.record('alice')
    view=s.registry.add_dedicated('alice','zoho',{'email':EMAIL,'password':PASSWORD,'host':'imappro.zoho.com'})
    identity=next(row['id'] for row in view['accounts'] if row['provider']=='zoho')
    assert s.legacy.record('alice')==before
    s.registry.select('alice',identity)
    reader=s.google.gmail('alice')
    page=reader.search()
    message=reader.read_message(page['messages'][0]['id'])
    assert 'field trip' in message['body'] and message['mailbox']==EMAIL
    assert PASSWORD not in json.dumps(view)
    assert not hasattr(reader,'send') and not hasattr(reader,'delete')


def test_http_ownership_csrf_and_callback_preserve_legacy_settings(setup,monkeypatch):
    import connections_api
    import email_inbox_api
    s=setup
    monkeypatch.setattr(connections_api,'_require_user',lambda request:request.headers.get('x-test-owner','alice'))
    monkeypatch.setattr(connections_api,'_DEFAULT_STORE',s.google)
    monkeypatch.setattr(connections_api,'exchange_override',s.exchange)
    monkeypatch.setattr(core,'default_transport',s.transport)
    app=FastAPI(); app.include_router(email_inbox_api.router); app.include_router(connections_api.router)
    with TestClient(app) as client:
        root='/api/connections/email-inbox'
        assert client.post(root+'/accounts/gmail',json={},headers={'Origin':'https://other.example'}).status_code==403
        assert client.post(root+'/accounts/gmail',json={'owner':'bob'}).status_code==400
        started=client.post(root+'/accounts/gmail',json={}).json()
        identity=started['account_id']
        assert client.post(root+f'/accounts/{identity}/disconnect',json={},headers={'x-test-owner':'bob'}).status_code==409
        state=parse_qs(urlsplit(started['authorization_url']).query)['state'][0]
        response=client.get('/api/connections/google/callback',params={'state':state,'code':'first'})
        assert response.status_code==200 and 'Gmail connected' in response.text
        view=client.get(root+'/accounts').json()
        assert any(row['address']=='first@example.com' for row in view['accounts'])
        assert 'access-for-' not in json.dumps(view)
        assert client.get(root+'/accounts',headers={'x-test-owner':'bob'}).json()['accounts'][0]['address']==''
        assert client.post(root+'/read-scope',json={'selection':identity}).status_code==200
        assert client.get('/api/connections').json()['connectors'][0]['gmail_connected'] is True


def test_new_mailbox_tasks_bind_selection_and_automations_keep_legacy(setup):
    from task_store import CloudTaskStore
    from job_contracts import validate_task_contract, JobContractError
    s=setup
    identity=s.connect('first'); s.registry.select('alice',identity)
    tasks=CloudTaskStore(s.root/'tasks.sqlite3')
    task=tasks.submit(session_key='email-bot',task_text='gmail: search receipt',executor_prefix='gmail',
                      chat_id='alice',resolve_bot=False)
    saved=tasks.get(task)
    assert saved['job_contract']['mailbox_binding']==s.registry.binding('alice')
    assert validate_task_contract(saved)['source']=='gmail'
    # No global Google Gmail grant exists, even though the new mailbox works.
    legacy=mail.automation_reader('alice')
    with pytest.raises(core.ConnectorUnavailable): legacy.search()
    s.registry.select('alice','none')
    with pytest.raises(JobContractError,match='settings changed'):
        validate_task_contract(saved)


def test_pkce_verifier_is_sent_to_exchange_but_never_returned_or_stored_in_plaintext(setup,monkeypatch):
    import hashlib
    s=setup
    start=s.registry.begin_gmail('alice',redirect_uri=REDIRECT)
    params=parse_qs(urlsplit(start['authorization_url']).query)
    seen=[]
    def exchange(code,redirect,client,*,code_verifier=None):
        assert code_verifier
        seen.append(code_verifier)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip('=')
        assert params['code_challenge']==[challenge]
        return s.exchange(code,redirect,client)
    monkeypatch.setattr(core.ConnectorStore,'_default_exchange',staticmethod(exchange))
    s.registry.complete_gmail(params['state'][0],'first',redirect_uri=REDIRECT,transport=s.transport)
    assert len(seen)==1 and seen[0] not in json.dumps(start)
    assert all(seen[0] not in path.read_text() for path in s.registry.credentials.glob('*.json'))


def test_default_token_exchange_posts_pkce_proof(monkeypatch):
    from io import BytesIO
    from urllib.parse import parse_qs
    requests = []

    def token_endpoint(request, timeout):
        requests.append(request)
        return BytesIO(b'{"access_token":"test-access"}')

    monkeypatch.setattr(core.urllib.request, 'urlopen', token_endpoint)
    result = core.ConnectorStore._default_exchange('test-code', REDIRECT,
        {'client_id':'test-client', 'client_secret':'test-secret'}, code_verifier='test-proof')
    assert result == {'access_token':'test-access'}
    posted = parse_qs(requests[0].data.decode())
    assert posted['code_verifier'] == ['test-proof']
    assert posted['grant_type'] == ['authorization_code']
    assert posted['redirect_uri'] == [REDIRECT]


def test_concurrent_callback_consumes_once_and_expiry_cannot_exchange(setup):
    from concurrent.futures import ThreadPoolExecutor
    s=setup
    started=s.registry.begin_gmail('alice',redirect_uri=REDIRECT)
    state=parse_qs(urlsplit(started['authorization_url']).query)['state'][0]
    calls=[]
    def exchange(*args): calls.append(True); return s.exchange(*args)
    def finish():
        try:
            s.registry.complete_gmail(state,'first',redirect_uri=REDIRECT,exchange=exchange,transport=s.transport)
            return 'connected'
        except core.OAuthStateError:
            return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:finish(),range(2)))
    assert sorted(results)==['connected','rejected'] and len(calls)==1
    expired=s.registry.begin_gmail('alice',redirect_uri=REDIRECT)
    state=parse_qs(urlsplit(expired['authorization_url']).query)['state'][0]
    with s.registry.db() as db: db.execute('UPDATE oauth SET expires=0')
    with pytest.raises(core.OAuthStateError):
        s.registry.complete_gmail(state,'second',redirect_uri=REDIRECT,exchange=exchange,transport=s.transport)
    assert len(calls)==1


def test_legacy_gmail_identity_prevents_double_search_and_is_invalidated_on_reconnect(setup):
    s=setup
    old=s.google.begin_oauth('alice',scopes=[core.GOOGLE_GMAIL_READ_SCOPE])
    s.google.complete_oauth('alice',old['state'],'first',exchange=s.exchange)
    identity=s.connect('first')
    assert s.registry.legacy_address('alice')=='first@example.com'
    s.registry.select('alice','all')
    reader=s.google.gmail('alice',transport=s.transport)
    s.calls.clear()
    page=reader.search()
    assert len(page['messages'])==1
    assert len([call for call in s.calls if call[1].endswith('/messages')])==1
    # A new legacy grant must invalidate both its cached identity and the old group.
    old=s.google.begin_oauth('alice',scopes=[core.GOOGLE_GMAIL_READ_SCOPE])
    s.google.complete_oauth('alice',old['state'],'second',exchange=s.exchange)
    assert s.registry.legacy_address('alice')==''
    with pytest.raises(core.ConnectorUnavailable): reader.read_message(page['messages'][0]['id'])
    assert s.registry._record('alice',identity)['status']=='connected'


def test_automation_task_stays_on_legacy_source_while_chat_selection_changes(setup):
    from task_store import CloudTaskStore
    from job_contracts import validate_task_contract, JobContractError
    s=setup
    old=s.google.begin_oauth('alice',scopes=[core.GOOGLE_GMAIL_READ_SCOPE])
    s.google.complete_oauth('alice',old['state'],'legacy',exchange=s.exchange)
    identity=s.connect('first'); s.registry.select('alice',identity)
    tasks=CloudTaskStore(s.root/'legacy-tasks.sqlite3')
    task=tasks.submit(session_key='email-bot',task_text='gmail: read id m0',executor_prefix='gmail',
                      chat_id='alice',resolve_bot=False,email_scope='legacy')
    saved=tasks.get(task)
    assert saved['job_contract']['mailbox_binding']['scope']=='legacy'
    s.registry.select('alice','none')
    assert not s.google.gmail_read_available('alice') and mail.automation_available('alice')
    assert validate_task_contract(saved)['source']=='gmail'
    s.legacy.select('alice','none')
    with pytest.raises(JobContractError): validate_task_contract(saved)


def test_real_executor_reads_both_accounts_and_publishes_bound_results(setup,monkeypatch):
    import serve
    import task_store
    s=setup
    s.connect('first'); s.connect('second'); s.registry.select('alice','all')
    tasks=task_store.CloudTaskStore(s.root/'mail-tasks.sqlite3')
    task=tasks.submit(session_key='email-bot',task_text='gmail: search receipt',executor_prefix='gmail',
                      chat_id='alice',resolve_bot=False)
    assert tasks.claim_next('mail-tests')['task_id']==task
    monkeypatch.setattr(task_store,'CloudTaskStore',lambda:tasks)
    monkeypatch.setattr(core,'default_transport',s.transport)
    results=[]
    ctx=serve.ExecutionContext(session_id='mail-tests',bot_id='email-bot',bot_owner='alice')
    serve._run_gmail_read_task(ctx,'alice','gmail: search receipt',task,lambda *a:None,on_result=results.append)
    assert len(results)==1 and results[0]['count']==2
    assert 'first@example.com' in results[0]['final_response'] and 'second@example.com' in results[0]['final_response']
    assert all(mid.startswith('mx1_') for mid in results[0]['message_ids'])
    tasks.complete(task,results[0])
    assert tasks.get(task)['status']=='done'


def test_scope_change_after_read_prevents_result_publication(setup):
    from task_store import CloudTaskStore
    s=setup
    identity=s.connect('first'); s.registry.select('alice',identity)
    tasks=CloudTaskStore(s.root/'publication.sqlite3')
    task=tasks.submit(session_key='email-bot',task_text='gmail: search receipt',executor_prefix='gmail',
                      chat_id='alice',resolve_bot=False)
    tasks.claim_next('mail-tests')
    s.registry.select('alice','none')
    tasks.complete(task,{'status':'no_changes','mode':'search','count':0,
        'final_response':'No matching messages.','message_ids':[],'query':'receipt','next_page_token':''},publish_result=True)
    assert tasks.get(task)['status']=='failed'
    assert not any(event['type']=='result' for event in tasks.get_events(task))
