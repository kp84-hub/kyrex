"""Offline exact-message reviews, isolated OAuth, uncertainty and durable workers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import io
from types import SimpleNamespace
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import bots
import connectors
import gmail_cleanup as cleanup
import gmail_cleanup_api as api
import gmail_cleanup_chat as chat
import chat_service
import task_store
import serve
REAL_OWNER=api.owner
REAL_TRANSPORT=cleanup.transport


@pytest.fixture
def f(tmp_path,monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET','gmail-cleanup-fixture-only')
    monkeypatch.setattr(task_store,'DATA_DIR',tmp_path)
    tasks = task_store.CloudTaskStore()
    monkeypatch.setattr(chat_service,'_task_store',lambda:tasks)
    registry = {'assistant':{'id':'assistant','owner':'alice','status':'running','rift':str(tmp_path),'policy':{}}}
    monkeypatch.setattr(bots,'load_bots',lambda:registry)
    fakegrant = SimpleNamespace(scope_granted=lambda owner,scope: owner=='alice',access_token=lambda owner:'fixture-token')
    monkeypatch.setattr(cleanup,'grant',lambda:fakegrant)
    store = cleanup.CleanupStore(tasks)
    with store.db() as db:
        db.execute('INSERT INTO gmail_cleanup_grants VALUES (?,?,?)',('alice','grant-one',connectors.seal_tokens({'address':'alice@example.test'})))
    messages = {'one':{'sender':'offers@shop.example','subject':'First offer','date':'Thu, 29 Oct 2026 10:00:00 +0000','labels':['INBOX']},
                'two':{'sender':'offers@shop.example','subject':'Archived offer','date':'Wed, 28 Oct 2026 10:00:00 +0000','labels':[]},
                'other':{'sender':'other@shop.example','subject':'Different sender','date':'Tue, 27 Oct 2026 10:00:00 +0000','labels':['INBOX']}}
    calls,writes = [],[]
    def transport(method,url,token,params=None,body=None):
        assert token == 'fixture-token'
        calls.append((method,url,params,body))
        if url.endswith('/profile'): return {'emailAddress':'alice@example.test'}
        if method=='POST':
            writes.append(body)
            for identity in body['ids']: messages[identity]['labels']=['TRASH']
            return {}
        if url.endswith('/messages'): return {'messages':[{'id':identity} for identity in messages]}
        identity = url.rsplit('/',1)[-1]; m=messages[identity]
        return {'id':identity,'labelIds':list(m['labels']),'payload':{'headers':[
            {'name':'From','value':'Shop <'+m['sender']+'>'}, {'name':'Subject','value':m['subject']}, {'name':'Date','value':m['date']}]}}
    monkeypatch.setattr(cleanup,'transport',transport)
    monkeypatch.setattr(api,'owner',lambda request,mutation=False:request.headers.get('x-user','alice'))
    app=FastAPI(); app.include_router(api.router); client=TestClient(app)
    return SimpleNamespace(**locals())


def prepare(f):
    view=f.store.create('alice','conversation','assistant','offers@shop.example','request-one')
    task=f.tasks.claim_next('worker-one')
    return view,task


def run(f,task):
    cleanup.run(SimpleNamespace(bot_id='assistant',bot_owner='alice'), 'alice',task['task_text'],task['task_id'],lambda *a:None)


def test_preparing_never_moves_mail_and_retry_reuses_one_task(f):
    first=f.store.create('alice','conversation','assistant','offers@shop.example','request-one')
    second=f.store.create('alice','conversation','assistant','offers@shop.example','request-one')
    assert first['id']==second['id'] and len(f.tasks.list_tasks())==1 and not f.writes
    assert first['mailbox']=='alice@example.test'
    with pytest.raises(ValueError): f.store.view('bob',first['id'])
    with pytest.raises(ValueError): f.store.create('alice','conversation','assistant','other@shop.example','request-one')


def test_only_exact_sender_ids_are_reviewed_and_post_is_fixed_labels(f,monkeypatch):
    view,task=prepare(f)
    def approve(*a,**kw):
        row=f.store.row('alice',view['id'])
        assert row['state']=='ready' and row['count']==2
        assert not f.writes
        # A newly arriving email must not be swept into the frozen review.
        f.messages['new']={**f.messages['one'],'subject':'New arrival'}
        return True
    monkeypatch.setattr(serve,'_request_in_process_approval',approve)
    run(f,task)
    assert f.store.view('alice',view['id'])['state']=='trashed'
    assert f.writes==[{'ids':['one','two'],'addLabelIds':['TRASH'],'removeLabelIds':['INBOX']}]
    assert f.messages['new']['labels']==['INBOX']
    assert f.messages['other']['labels']==['INBOX']
    assert all(not params or params.get('format')!='full' for _,_,params,_ in f.calls)
    assert b'First offer' not in f.tasks.db_path.read_bytes()


def test_decline_and_account_change_submit_no_write(f,monkeypatch):
    view,task=prepare(f)
    def changed(*a,**kw):
        with f.store.db() as db: db.execute("UPDATE gmail_cleanup_grants SET revision='changed' WHERE owner='alice'")
        return True
    monkeypatch.setattr(serve,'_request_in_process_approval',changed)
    run(f,task)
    assert not f.writes and f.store.view('alice',view['id'])['state']=='blocked'


@pytest.mark.parametrize('change',['sender','subject','labels'])
def test_message_changes_during_approval_block_entire_set(f,monkeypatch,change):
    view,task=prepare(f)
    def changed(*a,**kw):
        f.messages['one'][change] = {'sender':'other@example.test','subject':'Updated subject','labels':['TRASH']}[change]
        return True
    monkeypatch.setattr(serve,'_request_in_process_approval',changed)
    run(f,task)
    assert not f.writes and f.store.row('alice',view['id'])['state']=='blocked'


def test_lost_write_response_is_unknown_and_never_replayed(f,monkeypatch):
    view,task=prepare(f)
    original=cleanup.transport
    def lost(method,*args,**kwargs):
        result=original(method,*args,**kwargs)
        if method=='POST': raise RuntimeError('fixture response lost')
        return result
    monkeypatch.setattr(cleanup,'transport',lost)
    monkeypatch.setattr(serve,'_request_in_process_approval',lambda *a,**k:True)
    run(f,task)
    assert f.store.row('alice',view['id'])['state']=='unknown' and len(f.writes)==1
    run(f,task)
    assert len(f.writes)==1


def test_durable_worker_approval_rejects_other_owner_revision_and_replays(f):
    review=f.store.create('alice','conversation','assistant','offers@shop.example','request-one')
    worker=task_store.TaskWorker(f.tasks,worker_id='cleanup-worker',send=lambda *a:'message',edit=lambda *a:None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(worker.claim_and_execute_once)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            view=f.store.view('alice',review['id'])
            if view['approval_id']: break
            time.sleep(.01)
        assert view['approval_id'] and not f.writes
        path='/api/gmail-cleanup/reviews/'+review['id']+'/decision'
        payload={'revision':view['revision'],'approval_id':view['approval_id'],'decision':'trash'}
        assert f.client.post(path,json=payload,headers={'x-user':'bob'}).status_code==409
        assert f.client.post(path,json={**payload,'revision':'0'*32}).status_code==409
        assert f.client.post(path,json=payload).status_code==200
        f.tasks.deliver_operator_replies()
        assert future.result(timeout=5)
    assert f.store.view('alice',review['id'])['state']=='trashed'
    assert f.client.post(path,json=payload).status_code==409
    assert len(f.writes)==1


def test_bot_policy_restrictions_are_not_granted_by_names_or_models(f):
    f.registry['assistant']['policy']={'mail:trash':'deny'}
    with pytest.raises(ValueError): f.store.create('alice','conversation','assistant','offers@shop.example')
    f.registry['assistant']['policy']={'mail:read':0,'*':'deny'}
    with pytest.raises(ValueError): f.store.create('alice','conversation','assistant','offers@shop.example')


def large_mailbox(f,monkeypatch,count=2501):
    template = f.messages['one']
    f.messages.clear()
    f.messages.update({f'm{i}':{**template,'subject':f'Offer {i}','labels':['INBOX']} for i in range(count)})
    original=cleanup.transport
    def transport(method,url,token,params=None,body=None):
        if url.endswith('/messages'):
            f.calls.append((method,url,params,body))
            offset=int((params or {}).get('pageToken','0')); size=params['maxResults']
            ids=list(f.messages)[offset:offset+size]
            return {'messages':[{'id':identity} for identity in ids],
                    **({'nextPageToken':str(offset+size)} if offset+size<count else {})}
        return original(method,url,token,params,body)
    monkeypatch.setattr(cleanup,'transport',transport)


def test_all_pages_one_approval_and_bounded_phone_preview(f,monkeypatch):
    large_mailbox(f,monkeypatch)
    view,task=prepare(f); approvals=[]
    def approve(*a,**kw):
        approvals.append(kw)
        current=f.store.view('alice',view['id'])
        assert current['count']==2501 and len(current['messages'])==50 and current['has_next']
        assert current['scan_complete'] and not current['more'] and not f.writes
        with pytest.raises(ValueError): f.store.append_items('alice',view['id'],[])
        page=f.client.get('/api/gmail-cleanup/reviews/'+view['id']+'?offset=2500')
        assert page.status_code==200 and [m['id'] for m in page.json()['messages']]==['m2500']
        assert not page.json()['has_next']
        assert f.client.get('/api/gmail-cleanup/reviews/'+view['id']+'?offset=50',headers={'x-user':'bob'}).status_code==409
        f.messages['late']={**f.messages['m0'],'labels':['INBOX']}
        return True
    monkeypatch.setattr(serve,'_request_in_process_approval',approve)
    run(f,task)
    current=f.store.view('alice',view['id'])
    assert current['state']=='trashed' and current['moved_count']==2501
    assert len(approvals)==1 and [len(w['ids']) for w in f.writes]==[1000,1000,501]
    ids=[identity for w in f.writes for identity in w['ids']]
    assert len(ids)==len(set(ids))==2501 and f.messages['late']['labels']==['INBOX']
    assert json.loads(approvals[0]['detail'])['count']==2501
    run(f,task); assert len(f.writes)==3


@pytest.mark.parametrize('failure',['unknown','cancel','revoke','changed','stale'])
def test_stops_after_confirmed_batch_without_replay(f,monkeypatch,failure):
    large_mailbox(f,monkeypatch)
    view,task=prepare(f); original=cleanup.transport
    def transport(method,*a,**kw):
        result=original(method,*a,**kw)
        if method=='POST':
            if failure=='unknown' and len(f.writes)==2: raise RuntimeError('lost second response')
            if len(f.writes)==1:
                if failure=='cancel':
                    assert f.client.post('/api/gmail-cleanup/reviews/'+view['id']+'/stop',json={},headers={'x-user':'bob'}).status_code==409
                    assert f.client.post('/api/gmail-cleanup/reviews/'+view['id']+'/stop',json={}).status_code==200
                if failure=='revoke':
                    with f.store.db() as db: db.execute("UPDATE gmail_cleanup_grants SET revision='new' WHERE owner='alice'")
                if failure=='changed': f.messages['m1500']['subject']='Changed after first batch'
                if failure=='stale':
                    with f.tasks._lock:
                        f.tasks._conn.execute("UPDATE tasks SET run_id='new-claim' WHERE task_id=?",(task['task_id'],)); f.tasks._conn.commit()
        return result
    monkeypatch.setattr(cleanup,'transport',transport)
    monkeypatch.setattr(serve,'_request_in_process_approval',lambda *a,**k:True)
    run(f,task)
    current=f.store.view('alice',view['id'])
    assert current['moved_count']==1000
    assert current['state']==('unknown' if failure=='unknown' else 'partial')
    assert current['in_flight_count']==(1000 if failure=='unknown' else 0)
    assert len(f.writes)==(2 if failure=='unknown' else 1)
    run(f,task); assert len(f.writes)==(2 if failure=='unknown' else 1)
    assert 'TRASH' not in f.messages['m2500']['labels']


def test_scan_failure_cannot_offer_partial_set_for_approval(f,monkeypatch):
    large_mailbox(f,monkeypatch,501)
    view,task=prepare(f); original=cleanup.transport
    def transport(method,url,token,params=None,body=None):
        if (params or {}).get('pageToken'): raise RuntimeError('later page failed')
        return original(method,url,token,params,body)
    monkeypatch.setattr(cleanup,'transport',transport)
    monkeypatch.setattr(serve,'_request_in_process_approval',lambda *a,**k:pytest.fail('Incomplete scan cannot approve'))
    run(f,task)
    current=f.store.view('alice',view['id'])
    assert current['state']=='blocked' and current['count']==500 and not current['scan_complete']
    assert not f.writes


def test_repeated_pagination_token_blocks_incomplete_scan(f,monkeypatch):
    original=cleanup.transport
    def transport(method,url,*a,**kw):
        value=original(method,url,*a,**kw)
        if url.endswith('/messages'): value['nextPageToken']='more'
        return value
    monkeypatch.setattr(cleanup,'transport',transport)
    view,task=prepare(f)
    monkeypatch.setattr(serve,'_request_in_process_approval',lambda *a,**kw:False)
    run(f,task)
    assert not f.writes and not f.store.view('alice',view['id'])['scan_complete']
    assert f.store.view('alice',view['id'])['state']=='blocked'


def test_146000_matches_have_one_complete_review_without_large_api_payload(f,monkeypatch):
    total=146000; pages=[]; approvals=[]
    original=cleanup.transport
    def transport(method,url,token,params=None,body=None):
        if url.endswith('/messages'):
            start=int(params.get('pageToken','0')); pages.append(start)
            return {'messages':[{'id':f'm{i}'} for i in range(start,min(start+500,total))],
                    **({'nextPageToken':str(start+500)} if start+500<total else {})}
        return original(method,url,token,params,body)
    monkeypatch.setattr(cleanup,'transport',transport)
    monkeypatch.setattr(cleanup,'metadata',lambda token,identity,sender:
        {'id':identity,'subject':'An offer','date':'2026-10-29','fingerprint':'fixture','labels':['INBOX']})
    view,task=prepare(f)
    # Slow preparation must not consume the owner's approval window.
    with f.store.db() as db:
        db.execute('UPDATE gmail_cleanup_reviews SET created=? WHERE id=?',(time.time()-86400,view['id']))
    def decline(*a,**kw):
        approvals.append(kw)
        current=f.store.view('alice',view['id'])
        assert current['state']=='ready' and current['count']==total and current['scan_complete']
        assert len(current['messages'])==50 and current['has_next']
        last=f.store.view('alice',view['id'],total-50)
        assert last['messages'][-1]['id']=='m145999' and not last['has_next']
        assert len(json.dumps(current))<15000
        return False
    monkeypatch.setattr(serve,'_request_in_process_approval',decline)
    run(f,task)
    assert len(pages)==292 and len(approvals)==1 and not f.writes
    assert f.store.view('alice',view['id'])['state']=='cancelled'


def test_changed_message_in_later_batch_blocks_before_first_write(f,monkeypatch):
    large_mailbox(f,monkeypatch)
    view,task=prepare(f)
    def approve(*a,**kw):
        f.messages['m2500']['subject']='Changed after preview'
        return True
    monkeypatch.setattr(serve,'_request_in_process_approval',approve)
    run(f,task)
    assert not f.writes and f.store.view('alice',view['id'])['state']=='blocked'


@pytest.mark.parametrize('method',['GET','POST'])
def test_rate_limit_backoff_only_retries_reads(f,monkeypatch,method):
    attempts=[]; delays=[]
    class Reply:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,*args): return b'{}'
    class Opener:
        def open(self,request,**kwargs):
            attempts.append(request)
            if len(attempts)<3:
                raise cleanup.urllib.error.HTTPError(request.full_url,429,'rate limited',{},io.BytesIO())
            return Reply()
    monkeypatch.setattr(cleanup.urllib.request,'build_opener',lambda *a:Opener())
    monkeypatch.setattr(cleanup.time,'sleep',delays.append)
    url=cleanup.API+('/profile' if method=='GET' else '/messages/batchModify')
    if method=='GET':
        assert REAL_TRANSPORT(method,url,'fixture')=={}
        assert len(attempts)==3 and delays==[1,2]
    else:
        with pytest.raises(ValueError): REAL_TRANSPORT(method,url,'fixture',body={'ids':['one']})
        assert len(attempts)==1 and not delays


def test_missing_optional_headers_and_notification_failures_do_not_lose_receipt(f,monkeypatch):
    view,task=prepare(f); original=cleanup.transport
    def transport(*a,**kw):
        result=original(*a,**kw)
        if 'payload' in result:
            result['payload']['headers']=[h for h in result['payload']['headers'] if h['name']=='From']
        return result
    def unavailable(*a,**kw): raise RuntimeError('notification unavailable')
    monkeypatch.setattr(cleanup,'transport',transport)
    monkeypatch.setattr(serve,'_request_in_process_approval',lambda *a,**kw:True)
    cleanup.run(SimpleNamespace(bot_id='assistant',bot_owner='alice'),'alice',task['task_text'],task['task_id'],
                unavailable,on_progress=unavailable,on_result=unavailable)
    assert f.store.view('alice',view['id'])['state']=='trashed'
    f.tasks.complete(task['task_id'],{})
    run(f,task)
    assert f.store.view('alice',view['id'])['state']=='trashed' and len(f.writes)==1


def test_scope_is_only_available_on_separate_cleanup_store(f,monkeypatch):
    monkeypatch.setenv('GOOGLE_CLIENT_ID','fixture-client')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET','fixture-secret')
    monkeypatch.setenv('GOOGLE_REDIRECT_URI','https://chat.example.test/api/connections/google/callback')
    normal=connectors.ConnectorStore(f.tmp_path/'normal.json')
    with pytest.raises(connectors.ConnectorError): normal.begin_oauth('alice',scopes=[cleanup.MODIFY])
    isolated=cleanup.CleanupGrant(f.tmp_path/'separate.json')
    with pytest.raises(connectors.ConnectorError): isolated.begin_oauth('alice',scopes=[connectors.GOOGLE_CALENDAR_WRITE_SCOPE])
    monkeypatch.setattr(cleanup,'grant',lambda:isolated)
    started=f.store.begin('alice','https://chat.example.test/api/connections/google/callback')
    from urllib.parse import parse_qs,urlsplit
    query=parse_qs(urlsplit(started['authorization_url']).query)
    assert cleanup.MODIFY in query['scope'][0] and cleanup.MODIFY not in connectors.GOOGLE_ALLOWED_SCOPES
    assert query['state'][0].startswith('trash_') and 'code_challenge' in query
    assert query['include_granted_scopes']==['false']
    result=f.store.complete(query['state'][0],'fixture-code','https://chat.example.test/api/connections/google/callback',
        exchange=lambda *a:{'access_token':'fixture-token','refresh_token':'fixture-refresh','expires_in':3600})
    assert result['address']=='alice@example.test' and result['connected']
    assert not normal.status('alice')['connected']
    with pytest.raises(connectors.OAuthStateError):
        f.store.complete(query['state'][0],'fixture-code','https://chat.example.test/api/connections/google/callback')


def test_natural_intent_only_current_owner_request_and_ambiguous_sender_followup(f):
    assert chat.target('Delete all emails from Home Depot',{})=='Home Depot'
    assert chat.target('Can Kyrex delete emails?',{}) is None
    assert chat.target('Explain how to delete emails from Home Depot',{}) is None
    assert chat.target('Do not delete emails from Home Depot',{}) is None
    assert chat.target('Read my emails from Home Depot',{}) is None
    exact,found=chat.resolve_sender(f.store,'alice','Shop')
    assert not exact and len(found)==2 and not f.writes
    conv={'messages':[{'id':'question','role':'assistant','content':'Which sender?'}],
          'gmail_cleanup_pending':{'assistant':'question','created':time.time()}}
    assert chat.target('offers@shop.example',conv)=='offers@shop.example'


def test_changed_or_stale_worker_cannot_write(f,monkeypatch):
    view,task=prepare(f)
    def stale(*a,**kw):
        with f.tasks._lock:
            f.tasks._conn.execute("UPDATE tasks SET run_id='new-claim' WHERE task_id=?",(task['task_id'],)); f.tasks._conn.commit()
        return True
    monkeypatch.setattr(serve,'_request_in_process_approval',stale)
    run(f,task)
    assert not f.writes


def test_http_auth_and_cross_origin_cleanup_decisions(f,monkeypatch):
    import main
    import connections_api
    monkeypatch.setattr(api,'owner',REAL_OWNER)
    monkeypatch.setattr(connections_api,'_require_user',main.require_user)
    monkeypatch.setitem(main.sessions,'cleanup-session','alice')
    client=TestClient(f.app,base_url='https://chat.example.test')
    assert client.get('/api/gmail-cleanup').status_code==401
    client.cookies.set('session','cleanup-session')
    assert client.get('/api/gmail-cleanup').json()['address']=='alice@example.test'
    assert client.post('/api/gmail-cleanup/disconnect',json={},headers={'origin':'https://other.example.test'}).status_code==403
    assert client.post('/api/gmail-cleanup/disconnect',json={},headers={'sec-fetch-site':'cross-site'}).status_code==403


def test_chat_creates_native_review_without_model_and_preserves_followup(f,monkeypatch):
    conv=chat_service.create_conversation('alice',bot_id='assistant')
    def no_model(*a,**k): raise AssertionError('Cleanup does not call an LLM')
    monkeypatch.setattr(chat_service,'_get_engine_session',no_model)
    async def frames(text,request): return [frame async for frame in chat_service.stream_chat('alice',conv['conversation_id'],text,request_id=request)]
    result=asyncio.run(frames('Delete all emails from Shop','brand'))
    assert 'one email address' in result[-1]['content'] and not f.tasks.list_tasks()
    result=asyncio.run(frames('offers@shop.example','exact'))
    review=result[-1]['gmail_cleanup']['id']
    assert f.store.row('alice',review)['sender']=='offers@shop.example'
    persisted=chat_service.get_conversation('alice',conv['conversation_id'])
    assert any(message.get('gmail_cleanup',{}).get('id')==review for message in persisted['messages'])
    assert len(f.tasks.list_tasks())==1 and not f.writes
    assert len([m for m in persisted['messages'] if m.get('gmail_cleanup')])==1
    # Reconstruct a lost HTTP response from its durable task, without another
    # submission or a second generic approval card.
    persisted['messages']=[m for m in persisted['messages'] if not m.get('gmail_cleanup')]
    chat_service._write('alice',persisted)
    reopened=chat_service.get_conversation('alice',conv['conversation_id'])
    assert any(m.get('gmail_cleanup',{}).get('id')==review for m in reopened['messages'])
    assert len(f.tasks.list_tasks())==1 and not f.writes
