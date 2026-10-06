import asyncio
import json
import sqlite3
import time
import pytest
import device_messages as dm
import messages_send as ms

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'send-tests')
    store=dm.MessagesStore()
    token=store.redeem(store.begin('alice')['pairing_code'])
    store.sync(token,[{'id':'out','conversation_id':'c1','conversation':'Ethan The Neighbor','sender':'You','direction':'outgoing','received':'2026-10-06T00:00:00Z','body':'First message','kind':'RCS'},
                      {'id':'in','conversation_id':'c1','conversation':'Ethan The Neighbor','sender':'Ethan','direction':'incoming','received':'2026-10-06T00:01:00Z','body':'Yes I am home','kind':'RCS'},
                      {'id':'other','conversation_id':'c2','conversation':'School','sender':'School','direction':'incoming','received':'2026-10-06T00:02:00Z','body':'Other unrelated message','kind':'SMS'}])
    queue=ms.SendQueue(store)
    return store,queue,token

def ready(queue,token,key='one'):
    queue.poll(token, True)
    job=queue.start('alice','Ethan','Exact body 🌎',request_key=key)
    command=queue.poll(token,True)['command']
    assert command['action']=='prepare'
    preview={'conversation_id':'c1','token':'a'*32,'text':'Exact body 🌎','recipients':['Ethan · +15555550123'],'name':'Ethan The Neighbor','kind':'RCS'}
    queue.acknowledge(token,job['id'],'prepare',preview)
    return job,preview

def test_send_requires_enable_preview_and_owner_confirmation(setup):
    store,queue,token=setup
    with pytest.raises(dm.MessagesError,match='enable'):queue.start('alice','Ethan','Hello')
    queue.poll(token,True)
    job=queue.start('alice','Ethan','Exact body 🌎',request_key='one')
    with pytest.raises(dm.MessagesError):queue.decide('alice',job['id'],'send')
    command=queue.poll(token,True)['command']
    assert command['text']=='Exact body 🌎' and 'token' not in command
    assert queue.poll(token,True)['command'] is None
    preview={'conversation_id':'c1','token':'a'*32,'text':'Wrong','recipients':['Ethan'],'name':'Ethan','kind':'RCS'}
    with pytest.raises(dm.MessagesError):queue.acknowledge(token,job['id'],'prepare',preview)
    preview['text']='Exact body 🌎'
    queue.acknowledge(token,job['id'],'prepare',preview)
    assert queue.get('alice',job['id'])['state']=='ready'
    assert queue.poll(token,True)['command'] is None, 'Preview must not send automatically'
    assert 'token' not in json.dumps(queue.get('alice',job['id']))
    assert queue.start('alice','Ethan','Exact body 🌎',request_key='one')['id']==job['id']
    with pytest.raises(dm.MessagesError):queue.start('alice','Ethan','Another')
    with pytest.raises(dm.MessagesError):queue.decide('bob',job['id'],'send')
    queue.decide('alice',job['id'],'send');queue.decide('alice',job['id'],'send')
    command=queue.poll(token,True)['command']
    assert command['action']=='send' and command['token']=='a'*32
    assert queue.poll(token,True)['command'] is None, 'Claimed send cannot be retried'
    queue.acknowledge(token,job['id'],'send',{'accepted':True})
    assert queue.get('alice',job['id'])['state']=='accepted'
    assert not queue.acknowledge(token,job['id'],'send',{'accepted':False})['recorded']
    assert b'Exact body' not in store.path.read_bytes() and token.encode() not in store.path.read_bytes()

def test_cancel_disable_expiry_and_lost_ack_never_requeue(setup):
    store,queue,token=setup
    job,preview=ready(queue,token)
    queue.decide('alice',job['id'],'cancel')
    assert not queue.acknowledge(token,job['id'],'prepare',preview)['recorded']
    assert queue.poll(token,True)['command'] is None
    job,_=ready(queue,token,'two')
    queue.poll(token,False)
    assert queue.get('alice',job['id'])['state']=='cancelled'
    job,_=ready(queue,token,'three')
    with store._db() as db:db.execute('UPDATE message_sends SET expires=? WHERE id=?',(time.time()-1,job['id']))
    with pytest.raises(dm.MessagesError):queue.decide('alice',job['id'],'send')
    job,_=ready(queue,token,'four')
    queue.decide('alice',job['id'],'send');queue.poll(token,True)
    with store._db() as db:db.execute('UPDATE message_sends SET expires=? WHERE id=?',(time.time()-1,job['id']))
    assert queue.get('alice',job['id'])['state']=='unknown'
    assert queue.poll(token,True)['command'] is None

def test_disconnect_and_pair_rotation_revoke_commands(setup):
    store,queue,token=setup
    job,_=ready(queue,token)
    replacement=store.redeem(store.begin('alice')['pairing_code'])
    with pytest.raises(dm.MessagesError):queue.poll(token,True)
    with pytest.raises(dm.MessagesError):queue.get('alice',job['id'])
    queue.poll(replacement,True)
    store.disconnect('alice')
    with pytest.raises(dm.MessagesError):queue.poll(replacement,True)

def test_reply_only_and_ambiguous_names(setup):
    store,queue,token=setup
    result=ms.latest_reply('alice','Ethan')
    assert 'Yes I am home' in result['content'] and 'Other unrelated' not in result['content'] and 'First message' not in result['content']
    assert 'Yes I am home' in ms.latest_reply('alice','',{'conversation_id':'c1'})['content']
    job,_=ready(queue,token)
    queue.decide('alice',job['id'],'send')
    assert 'No newer incoming reply' in ms.latest_reply('alice','',{'conversation_id':'c1','send_id':job['id']})['content']
    store.sync(token,[{'conversation_id':'c1','conversation':'Ethan A','sender':'Ethan'}, {'conversation_id':'c2','conversation':'Ethan B','sender':'Ethan'}])
    with pytest.raises(dm.MessagesError,match='More than one'):ms.resolve_thread(store,'alice','Ethan')
    with pytest.raises(dm.MessagesError):ms.latest_reply('bob','Ethan')

@pytest.mark.parametrize('text,expected',[('Text Ethan: Hello 🌎',('Ethan','Hello 🌎')),('Send a message to Ethan saying Hello',('Ethan','Hello')),('reply: Yes',('','Yes')),('Reply to Ethan:  keep spaces  ',('Ethan',' keep spaces  ')),('send my emails to Ethan',None),('show my texts',None)])
def test_send_parser(text,expected): assert ms.send_command(text)==expected

@pytest.mark.parametrize('text,recipient',[('What did Ethan reply?','Ethan'),('Read the latest reply from Ethan','Ethan'),('Did he reply?','he'),('Did Ethan reply?','Ethan'),("Read Ethan's reply",'Ethan'),('Read reply','')])
def test_reply_parser(text,recipient): assert ms.reply_command(text)['recipient']==recipient

def test_stream_persists_send_card_and_followup_context(setup,monkeypatch):
    store,queue,token=setup
    import chat_service as chat
    queue.poll(token,True)
    conv={'conversation_id':'test','messages':[]}
    monkeypatch.setattr(chat,'get_conversation',lambda *a:conv)
    monkeypatch.setattr(chat,'_write',lambda *a:None)
    monkeypatch.setattr(chat,'_resolve_provider',lambda *a,**k:pytest.fail('Messages invoked LLM'))
    async def run(text):return [f async for f in chat.stream_chat('alice','test',text,request_id='request')]
    frames=asyncio.run(run('Text Ethan: Hi'))
    assert any(f['type']=='message_send' for f in frames)
    assert conv['messages'][-1]['message_send']['id']==conv['messages_thread']['send_id']
    frames=asyncio.run(run('What did Ethan reply?'))
    assert 'Yes I am home' in frames[-1]['content'] and 'School' not in frames[-1]['content']


def test_api_owner_and_phone_scope(setup, monkeypatch):
    store, queue, token=setup
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import connections_api, messages_api
    def owner(req):
        who=req.headers.get('x-owner')
        if not who:raise HTTPException(401)
        return who
    monkeypatch.setattr(connections_api,'_require_user',owner)
    app=FastAPI();app.include_router(messages_api.router);client=TestClient(app)
    headers={'Authorization':'Bearer '+token}
    assert client.post('/api/connections/messages/device/poll',json={'allow_send':True}).status_code==401
    assert client.post('/api/connections/messages/device/poll',headers=headers,json={'allow_send':True}).status_code==200
    assert client.post('/api/connections/messages/sends',headers=headers,json={'recipient':'Ethan','text':'Hi'}).status_code==401
    created=client.post('/api/connections/messages/sends',headers={'x-owner':'alice'},json={'recipient':'Ethan','text':'Hi'})
    assert created.status_code==200 and created.headers['cache-control']=='no-store'
    job=created.json();base='/api/connections/messages/sends/'+job['id']
    assert client.get(base,headers=headers).status_code==401
    assert client.get(base,headers={'x-owner':'bob'}).status_code==400
    assert client.post(base+'/decision',headers={'x-owner':'alice'},json={'decision':'send'}).status_code==400
    command=client.post('/api/connections/messages/device/poll',headers=headers,json={'allow_send':True}).json()['command']
    preview={'conversation_id':'c1','token':'a'*32,'text':'Hi','recipients':['Ethan +1555'],'name':'Ethan','kind':'RCS'}
    assert client.post('/api/connections/messages/device/ack',headers=headers,json={'id':job['id'],'action':'prepare','result':preview}).json()['recorded']
    assert client.post(base+'/decision',headers={'x-owner':'alice'},json={'decision':'send'}).json()['state']=='send_queued'
    assert client.post('/api/connections/messages/device/poll',headers=headers,json={'allow_send':True}).json()['command']['action']=='send'
    assert client.post('/api/connections/messages/device/ack',headers=headers,json={'id':job['id'],'action':'send','result':{'accepted':True}}).json()['recorded']
    reply=client.get(base+'/reply',headers={'x-owner':'alice'})
    assert reply.status_code==200 and 'School' not in reply.json()['content']
    assert 'token' not in json.dumps(client.get(base,headers={'x-owner':'alice'}).json())


def test_concurrent_send_claim_and_wrong_phone_ack(setup):
    import concurrent.futures
    store,queue,token=setup
    job,_=ready(queue,token)
    bob=store.redeem(store.begin('bob')['pairing_code'])
    assert queue.poll(bob,True)['command'] is None
    with pytest.raises(dm.MessagesError):queue.acknowledge(bob,job['id'],'send',{'accepted':True})
    queue.decide('alice',job['id'],'send')
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        claims=list(pool.map(lambda _:queue.poll(token,True)['command'],range(2)))
    assert sum(command is not None for command in claims)==1


def test_public_sse_preserves_scoped_send_card():
    import chat_api
    async def source():
        yield {'type':'conversation','conversation_id':'test'}
        yield {'type':'message_send','send_id':'scoped-id'}
        yield {'type':'status','status':'complete','content':'Review the card'}
    async def collect():
        return [frame async for frame in chat_api._drive_stream(source(),'request','test')]
    frames=''.join(asyncio.run(collect()))
    assert '"type": "message_send"' in frames and 'scoped-id' in frames
    assert '"type": "done"' in frames
