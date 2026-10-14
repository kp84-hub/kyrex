"""Conversational booking, actual owner state and fake authenticated browser. Zero live bookings."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import booking_chat as flow
import booking_store
import booking_schema as schema
import browser_hosts
import browser_host_bridge
import bots
import chat_service as chat
import email_identity
import serve
from task_store import CloudTaskStore

URL='https://restaurant.example/book'
CONTACT='Keith Plane, 919-555-0101'
START='Book a table at Fixture Grill in Raleigh on 2030-06-14 at 8:30 PM for four. '+URL
DETAILS={'venue':'Fixture Grill','location':'Raleigh','date':'2030-06-14','time':'20:30','party_size':4,'name':'Keith Plane','phone':'919-555-0101','email':'keith@kyrex.dev'}


@pytest.mark.parametrize('text',[
    "Yep, I'm also looking to book an appointment or a reservation at Ruth's Christ in Morrisville. Can you handle that for me?",
    "I'm looking to book a table at Ruth's Chris in Raleigh.",
    "I am hoping to reserve a table for three.",
    "Could you help me book a reservation?",
    "Can you please book a table?",
    "I'd also like to book a table.",
    "I’d like to reserve a table.",
    "Let's make a reservation.",
    "Also, book a table at Fixture Grill.",
])
def test_natural_owner_requests_enter_booking_flow(text):
    assert flow.action(text,{'messages':[]})=='start'


@pytest.mark.parametrize('text',[
    "Don't book a table.", "I'm not looking to book a table.",
    "I don't want to book a table.", "Can you explain how to book a table?",
    "What happens when I book a table?", "Read this: book a table at Fixture Grill.",
    'The website says "book a table".', "Show my reservation at Fixture Grill.",
    "I'm looking to book a doctor appointment.", "Book an appointment at the dentist.",
    "Book an appointment tomorrow.", "Book a hotel reservation.",
    "Book a flight.", "Book a table by email.",
    "Can you book a reservation using your email address?",
])
def test_research_negation_email_and_other_bookings_stay_out(text):
    assert flow.action(text,{'messages':[]}) is None


def test_conversational_request_collects_details_in_native_flow(rig):
    text="Yep, I'm also looking to book an appointment or a reservation at Fixture Grill in Raleigh. Can you handle that for me?"
    result=turn(rig,text,'natural-start')[-1]
    assert 'preferred time' in result['content'] and 'party size' in result['content']
    assert 'date' in result['content']
    entry=flow.latest(chat.get_conversation('alice',rig[0]))['booking']
    assert entry['state']=='collecting'
    assert flow.action('Three.',chat.get_conversation('alice',rig[0]))=='details'
    assert rig[1]['posts']==0
    turn(rig,'2030-06-14 at 8:30 PM for four. '+URL,'natural-details')
    proposal=turn(rig,CONTACT,'natural-contact')[-1]
    assert 'Does that work?' in proposal['content'] and rig[1]['posts']==0
    result=turn(rig,'yes','natural-confirm')[-1]
    assert 'website confirmed' in result['content'] and rig[1]['posts']==1


def controls():
    out=[]
    for field,label,tag,typ in [('name','Name','input','text'),('phone','Phone','input','tel'),('email','Email','input','email'),('date','Date','input','date'),('party_size','Party size','select',''),('time','Available time','select','')]:
        c={'ref':'c'+str(len(out)),'selector':'css=#'+field,'label':label,'tag':tag,'type':typ,'value':'','href':'','required':True,'disabled':False,'form_action':URL,'form_method':'post','options':[],'frame':URL}
        if field=='party_size': c['options']=[{'value':'4','label':'4 people','disabled':False}]
        if field=='time': c['options']=[{'value':'20:30','label':'8:30 PM','disabled':False}]
        c['fingerprint']=schema.fingerprint(c);out.append(c)
    c={'ref':'c6','selector':'css=#book','label':'Book reservation','tag':'button','type':'submit','value':'','href':'','required':False,'disabled':False,'form_action':URL,'form_method':'post','options':[],'frame':URL}
    c['fingerprint']=schema.fingerprint(c);out.append(c)
    return out


@pytest.fixture
def rig(tmp_path,monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR',str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET','booking-test-secret')
    monkeypatch.setenv('KYREX_PROVIDER','openai');monkeypatch.setenv('KYREX_API_KEY','test-only-key');monkeypatch.setenv('KYREX_MODEL','test-model')
    monkeypatch.setattr(chat,'_data_dir',lambda:tmp_path)
    monkeypatch.setattr(chat.chat_memory,'context',lambda owner:'')
    monkeypatch.setattr(bots,'BOTS_FILE',str(tmp_path/'bots.json'))
    rift=tmp_path/'rift';rift.mkdir()
    bots.add_bot('browser-bot','Browser Bot','openai:test-model',str(rift),owner='alice',status='running',policy=serve.browser_preset_policy(),browser_allowlist=['restaurant.example'])
    host=SimpleNamespace(host_id='test-host',is_available=lambda:True)
    monkeypatch.setattr(browser_hosts,'host_for',lambda owner,bid:host if owner=='alice' and bid=='browser-bot' else None)
    monkeypatch.setattr(email_identity,'current',lambda owner:{'status':'connected','provider':'zoho','address':'keith@kyrex.dev','can_send':False})
    state={'calls':[],'posts':0,'outcome':'confirmed','changed':False,'planner_calls':0}
    task=CloudTaskStore(db_path=tmp_path/'tasks.db');monkeypatch.setattr(chat,'_task_store',lambda:task)
    def browser(**kw):
        spec=json.loads(kw['task_text'])[schema.ACTION]
        store=booking_store.BookingStore();store.authorize(kw['owner'],kw['bot_id'],kw['host_id'],kw['task_text'])
        assert store.active(kw['owner'],kw['bot_id'],kw['host_id'],kw['task_text'])
        state['calls'].append(spec)
        if state.get('offline'): return None,'lost connection'
        c=controls()
        for item in spec['steps']:
            target=next(v for v in c if v['fingerprint']==item['control']['fingerprint'])
            assert item['op'] in {'fill','select'}
            target['value']=item['value']
        snap={'url':URL,'title':'Fixture Grill','text':'Fixture Grill, Raleigh\nAvailable times for your reservation.','controls':c,'blocked':False}
        if spec['stage']=='submit':
            assert spec['expected']==schema.review_hash(snap)
            if state['changed']: return {'mode':'booking_browser','outcome':'changed'},None
            state['posts']+=1
            return {'mode':'booking_browser','outcome':state['outcome'],'reference':'FG-1234','url':URL+'/confirmation','evidence':'Actual fixture response'},None
        return {'mode':'booking_browser','snapshot':snap},None
    monkeypatch.setattr(browser_host_bridge,'request_browser_dispatch',browser)
    async def provider(**kw):
        system=kw['messages'][0]['content'];data=json.loads(kw['messages'][1]['content'])
        if system.startswith('Extract'):
            result={k:v for k,v in data['current_details'].items()}
            evidence={}
            alltext='\n'.join(data['owner_messages'])
            if 'Fixture Grill' in alltext:
                for key,value,quote in [('venue','Fixture Grill','Fixture Grill'),('location','Raleigh','Raleigh'),('date','2030-06-14','2030-06-14'),('time','20:30','8:30 PM'),('party_size',4,'four')]:
                    if quote in alltext:
                        result[key]=value;evidence[key]=quote
            if 'Keith Plane' in alltext:
                result.update(name='Keith Plane',phone='919-555-0101');evidence.update(name='Keith Plane',phone='919-555-0101')
            if state.get('edit'): result['time']='21:00';evidence['time']='9:00 PM'
            return {'content':json.dumps({'details':result,'evidence':evidence,'url':URL if URL in alltext else ''})}
        state['planner_calls']+=1
        if state.get('inject'): return {'content':json.dumps({'action':'fill','ref':'c0','field':'name','value':'Attacker'})}
        d=data['owner_details'];snap=data['snapshot']
        needed=[k for k in ('name','phone','email') if not d.get(k)]
        if needed: return {'content':json.dumps({'action':'needs_details','fields':needed})}
        for c in snap['controls'][:-1]:
            field=c['selector'].split('#')[1]
            if c['value']!=str(d[field]):
                return {'content':json.dumps({'action':'select' if c['tag']=='select' else 'fill','ref':c['ref'],'field':field,'value':str(d[field])})}
        return {'content':json.dumps({'action':'review','ref':'c6'})}
    monkeypatch.setattr(chat,'get_provider',lambda *a,**kw:SimpleNamespace(chat=provider))
    cid=chat.create_conversation('alice')['conversation_id']
    yield cid,state,tmp_path,task
    chat.close_all_engine_sessions()


def turn(rig,text,key=None,owner='alice',cancel=None):
    async def run(): return [f async for f in chat.stream_chat(owner,rig[0],text,request_id=key,cancel_event=cancel)]
    return asyncio.run(run())


def review(rig):
    first=turn(rig,START,'start')[-1]
    assert 'reservation name' in first['content'] and 'phone number' in first['content']
    assert 'contact email' not in first['content'],'The native mailbox fills only the known email.'
    assert not rig[1]['posts'] and rig[1]['calls']
    second=turn(rig,CONTACT,'contacts')[-1]
    assert 'Does that work?' in second['content'] and '8:30 PM' in second['content']
    assert 'Keith Plane' in second['content'] and 'keith@kyrex.dev' in second['content']
    assert not rig[1]['posts']
    return second


def test_collect_actual_form_review_yes_and_persistent_confirmation(rig):
    review(rig)
    before=len(rig[1]['calls'])
    result=turn(rig,'yes','confirm')[-1]
    assert 'website confirmed' in result['content'] and 'FG-1234' in result['content']
    assert rig[1]['posts']==1 and len(rig[1]['calls'])==before+1
    # No sending OAuth is required for a website's contact email.
    assert email_identity.current('alice')['can_send'] is False
    repeat=turn(rig,'yes','confirm')[-1]
    assert repeat['content']==result['content'] and rig[1]['posts']==1
    turn(rig,'yes','another-confirm')
    assert rig[1]['posts']==1
    conv=chat.get_conversation('alice',rig[0])
    assert len([m for m in conv['messages'] if m['id']=='turn-confirm-user'])==1
    assert len([m for m in conv['messages'] if m['id']=='turn-confirm-assistant'])==1
    raw=(rig[2]/'bookings.sqlite3').read_bytes()
    for pii in (CONTACT,'Keith Plane','919-555-0101','keith@kyrex.dev'): assert pii.encode() not in raw


@pytest.mark.parametrize('answer',['no','cancel','no thanks'])
def test_no_cancels_only_unsubmitted_proposal(rig,answer):
    review(rig)
    assert 'Nothing was booked' in turn(rig,answer,'deny')[-1]['content']
    assert rig[1]['posts']==0
    turn(rig,'yes','late-yes')
    assert rig[1]['posts']==0


@pytest.mark.parametrize('outcome',['unknown','submitted','changed'])
def test_actual_site_outcome_is_not_invented_or_retried(rig,outcome):
    review(rig)
    rig[1]['outcome']=outcome;rig[1]['changed']=outcome=='changed'
    result=turn(rig,'yes','confirm')[-1]['content']
    assert 'website confirmed' not in result
    assert ('unknown' in result if outcome=='unknown' else 'not confirmed' in result if outcome=='submitted' else 'changed' in result)
    old=rig[1]['posts'];turn(rig,'yes','repeat')
    assert rig[1]['posts']==old


def test_expired_review_is_not_reissued_by_an_error_answer(rig):
    review(rig)
    store=booking_store.BookingStore()
    with store.db() as db: db.execute('UPDATE bookings SET updated=0')
    assert 'expired' in turn(rig,'yes','old-review')[-1]['content']
    last=flow.latest(chat.get_conversation('alice',rig[0]))
    assert not last.get('booking')
    assert flow.action('yes',chat.get_conversation('alice',rig[0])) is None
    assert rig[1]['posts']==0


def test_changed_details_require_a_new_review_not_embedded_yes(rig):
    review(rig)
    rig[1]['edit']=True
    result=turn(rig,'yes but 9:00 PM instead','change')[-1]['content']
    assert rig[1]['posts']==0 and 'unavailable' in result


def test_unrelated_assistant_turn_cannot_approve_an_older_booking(rig):
    review(rig)
    conv=chat.get_conversation('alice',rig[0]);chat._append_message('alice',conv,'assistant','Your calendar is empty.');chat._write('alice',conv)
    assert flow.action('yes',conv) is None
    assert rig[1]['posts']==0


def test_foreign_owner_stopped_bot_and_forged_model_details_cannot_book(rig,monkeypatch):
    review(rig)
    store=booking_store.BookingStore();conv=chat.get_conversation('alice',rig[0]);ident=flow.latest(conv)['booking']['id']
    with pytest.raises(booking_store.BookingError): store.get('bob',ident)
    bots.set_status('browser-bot','stopped')
    assert 'Browser Bot' in turn(rig,'yes','stopped')[-1]['content']
    assert rig[1]['posts']==0


def test_model_cannot_type_an_unprovided_contact_or_approve(rig):
    turn(rig,START,'start');rig[1]['inject']=True
    result=turn(rig,CONTACT,'bad-plan')[-1]['content']
    assert 'does not match' in result and rig[1]['posts']==0
    assert all(c['stage']=='prepare' for c in rig[1]['calls'])


def test_cancelled_owner_turn_never_starts_browser_work(rig):
    cancel=asyncio.Event();cancel.set()
    assert turn(rig,START,'stop',cancel=cancel)[-1]['status']=='cancelled'
    assert not rig[1]['calls']


def test_durable_bridge_seals_private_specs_and_results(rig):
    task=rig[3];spec=json.dumps({'booking_browser':{'contact':CONTACT}})
    ident=task.submit_browser_dispatch(task_id='booking-test',owner='alice',bot_id='browser-bot',host_id='test-host',task_text=spec)
    assert task.get_browser_dispatch(ident)['task_text']==spec
    task.record_browser_dispatch_progress(ident,{'private':CONTACT})
    task.record_browser_dispatch_progress(ident,{'stage':'done'})
    notes,cursor=task.browser_dispatch_progress(ident)
    assert notes==[{'private':CONTACT},{'stage':'done'}] and cursor==2
    task.complete_browser_dispatch(ident,{'snapshot':{'private':CONTACT}})
    assert CONTACT in task.get_browser_dispatch(ident)['result']
    with task._lock:
        row=task._conn.execute('SELECT task_text,result,progress FROM browser_dispatches WHERE dispatch_id=?',(ident,)).fetchone()
    assert all(CONTACT not in cell for cell in row)
    failed=task.submit_browser_dispatch(task_id='booking-error',owner='alice',bot_id='browser-bot',host_id='test-host',task_text=spec)
    task.fail_browser_dispatch(failed,CONTACT)
    assert task.get_browser_dispatch(failed)['error']==CONTACT
    assert CONTACT.encode() not in (rig[2]/'tasks.db').read_bytes()


def test_nonce_cannot_be_forged_reused_or_used_after_cancellation(rig):
    review(rig)
    store=booking_store.BookingStore();entry=flow.latest(chat.get_conversation('alice',rig[0]))['booking'];row=store.get('alice',entry['id'])
    row=store.claim('alice',row['id'],row['revision'],rig[0],'turn-contacts-assistant')
    wire=store.wire(row,'browser-bot','test-host','submit')
    tampered=json.loads(wire);tampered['booking_browser']['details']['party_size']=20
    with pytest.raises(booking_store.BookingError): store.authorize('alice','browser-bot','test-host',json.dumps(tampered,sort_keys=True))
    with pytest.raises(booking_store.BookingError): store.authorize('bob','browser-bot','test-host',wire)
    store.authorize('alice','browser-bot','test-host',wire)
    assert store.active('alice','browser-bot','test-host',wire)
    with pytest.raises(booking_store.BookingError): store.authorize('alice','browser-bot','test-host',wire)
    store.request_cancel('alice',row['id'])
    assert not store.active('alice','browser-bot','test-host',wire)
