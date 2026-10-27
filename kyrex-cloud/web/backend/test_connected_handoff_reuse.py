"""Shared work cannot fan out merely because the coordinator changes Bot names."""
import json
import importlib.util
from types import SimpleNamespace

import pytest

import cal_writer
import delegation
import email_accounts
import jev_stream_router as router
import serve
from task_store import CloudTaskStore


def isolated_router():
    # Other integration suites install compatibility wrappers on the global
    # router during collection. Exercise this boundary without sharing their
    # fake transports, wrapper state or per-test installation flags.
    spec=importlib.util.spec_from_file_location('handoff_test_router',router.__file__)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def f(tmp_path, monkeypatch):
    routing=isolated_router()
    store = CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(email_accounts, 'default_store', lambda: SimpleNamespace(binding=lambda owner:None))
    entries = {key:{'id':key,'owner':'alice','status':'running','name':key,'policy':{}}
               for key in ('mail-one','mail-two','calendar')}
    chat = SimpleNamespace(serve=serve, _task_store=lambda:store,
        delegation=SimpleNamespace(public_view=delegation.public_view),
        bots=SimpleNamespace(load_bots=lambda:entries,is_running=lambda bot:bot['status']=='running'))
    session = SimpleNamespace(delegation_ctx={'owner':'alice','bot':{'id':'chief'},
        'conversation_id':'parent-chat','parent_task_id':'turn-one'})
    calls = []
    def submit(owner, bot, text, *, store, conversation_id, parent_delegation_id):
        calls.append((bot['id'],text))
        return store.submit(session_key=bot['id'],bot_id=bot['id'],rift='',chat_id=owner,
            executor_prefix='gmail',task_text=text,conversation_id=conversation_id,
            parent_delegation_id=parent_delegation_id,resolve_bot=False)
    service = SimpleNamespace(gmail_route_ready=lambda bot:True,submit_gmail_task=submit)
    def send(target='mail-one',command='gmail: search school'):
        return routing._submit_routed_gmail(chat,service,session,{'target_bot_id':target,'task':command},
            {'selected_bot_id':target,'request_text':'Search my emails about school'})
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('status',['queued','running','awaiting_approval'])
def test_pending_task_is_followed_instead_of_launching_other_bots(f,status):
    ok, first = f.send()
    assert ok
    f.store.set_status(first['task_id'],status)
    for target in ('mail-two','calendar','mail-one'):
        ok, reused = f.send(target,'gmail: search a different school query')
        assert ok and reused['task_id']==first['task_id'] and reused['status']==status
        assert reused['reused'] and 'delegation_status' in reused['follow_up']
    assert f.calls==[('mail-one','gmail: search school')]
    assert len(f.store.list_delegations(owner='alice'))==1


def test_completed_query_is_reused_and_distinct_followup_stays_on_same_bot(f):
    _, first = f.send()
    f.store.set_status(first['task_id'],'done')
    ok, reused = f.send('mail-two')
    assert ok and reused['task_id']==first['task_id'] and len(f.calls)==1
    ok, following = f.send('calendar','gmail: read id selected-message')
    assert ok and following['task_id']!=first['task_id']
    assert f.calls[-1]==('mail-one','gmail: read id selected-message')
    assert following['target_bot_id']=='mail-one'
    assert len(f.calls)==2


def test_one_alternate_read_only_after_actual_failure_never_unlimited_fallback(f):
    _, first = f.send()
    f.store.set_status(first['task_id'],'failed')
    ok, second = f.send('mail-two')
    assert ok and second['target_bot_id']=='mail-two' and len(f.calls)==2
    f.store.set_status(second['task_id'],'failed')
    for target in ('calendar','mail-one','mail-two'):
        ok, last = f.send(target)
        assert ok and last['retry_limit_reached'] and last['task_id']==second['task_id']
    assert len(f.calls)==2


def test_cancelled_read_does_not_restart_on_another_bot(f):
    _, first = f.send()
    f.store.set_status(first['task_id'],'cancelled')
    _, reused = f.send('mail-two')
    assert reused['task_id']==first['task_id'] and reused['status']=='cancelled'
    assert len(f.calls)==1


def test_jev_retry_limit_cannot_fall_through_to_generic_bot_execution(f,monkeypatch):
    routing=f.routing
    class Session:
        def _handle_delegation(self,frame):
            pytest.fail('A repeated shared-mail request must never fall through to the generic executor.')
    async def stream(*a,**k):
        yield {'type':'status','status':'complete'}
    f.chat.EngineSession=Session
    f.chat.stream_chat=stream
    f.service.is_writable_bot_policy=lambda policy:False
    f.service.browser_route_ready=lambda bot:False
    f.service.email_calendar_route_ready=lambda bot:False
    routing.install(f.chat,f.service)
    session=Session()
    session.delegation_ctx=f.session.delegation_ctx
    session._jev_bot_target_hint={'coordinator_bot_id':'chief','selected_bot_id':'mail-one',
        'request_text':'Search my emails about school','consumed':False}
    frame={'target_bot_id':'mail-one','task':'gmail: search school'}
    _,first=session._handle_delegation(frame)
    _,same=session._handle_delegation({**frame,'target_bot_id':'mail-two'})
    assert same['task_id']==first['task_id'] and len(f.calls)==1
    f.store.set_status(first['task_id'],'failed')
    _,second=session._handle_delegation({**frame,'target_bot_id':'mail-two'})
    f.store.set_status(second['task_id'],'failed')
    _,bounded=session._handle_delegation({**frame,'target_bot_id':'calendar'})
    assert bounded['retry_limit_reached'] and len(f.calls)==2


def test_restart_of_same_user_turn_recovers_durable_handoff(f):
    _, first = f.send()
    f.session._connected_handoffs = {}
    _, recovered = f.send('mail-two')
    assert recovered['task_id']==first['task_id'] and len(f.calls)==1
    f.session.delegation_ctx['parent_task_id']='turn-two'
    _, fresh = f.send('mail-two')
    assert fresh['task_id']!=first['task_id'] and len(f.calls)==2
    assert f.store.get_delegation(fresh['delegation_id'])['parent_task_id']=='turn-two'


@pytest.mark.parametrize('field,value',[('owner','bob'),('coordinator_bot_id','other-chief'),
    ('parent_conversation_id','other-chat'),('parent_task_id','other-turn'),('target_bot_id','other-bot')])
def test_unverifiable_existing_binding_never_starts_a_fallback(f,field,value):
    _, first = f.send()
    original = f.store.get_delegation
    def altered(did):
        rec=original(did)
        return {**rec,field:value} if did==first['delegation_id'] else rec
    f.store.get_delegation=altered
    ok,result=f.send('mail-two')
    assert not ok and 'could not be verified' in result['error']
    assert len(f.calls)==1


@pytest.mark.parametrize('status',['queued','running','awaiting_approval','done','failed','cancelled'])
def test_calendar_write_handoff_is_never_automatically_replayed(f,status):
    intent=json.dumps(cal_writer.build_intent('School event','2026-11-03','09:00','10:00',all_day=False))
    did=f.store.create_delegation(owner='alice',coordinator_bot_id='chief',target_bot_id='calendar',
        parent_conversation_id='parent-chat',parent_task_id='turn-one',executor_prefix='email_calendar',task_text='Add that to my calendar')
    tid=f.store.submit('calendar',intent,bot_id='calendar',rift='',chat_id='alice',executor_prefix='email_calendar',
        conversation_id='parent-chat',resolve_bot=False)
    f.store.set_delegation_status(did,'queued',task_id=tid)
    f.store.set_status(tid,status)
    # Recovery uses the real persisted task payload, not model-provided text.
    target,outcome=f.routing._connected_handoff_route(f.chat,f.session,'email_calendar',intent,'mail-two')
    assert target=='calendar' and outcome[0] and outcome[1]['task_id']==tid
    assert outcome[1]['status']==status and outcome[1]['reused']
    assert len(f.store.list_tasks())==1


def test_auxiliary_browser_read_does_not_consume_initial_mail_route(monkeypatch):
    routing=isolated_router()
    calls=[]
    class Session:
        def _handle_delegation(self,frame): calls.append(frame); return True,dict(frame)
    async def stream(*a,**k):
        yield {'type':'status','status':'complete'}
    chat=SimpleNamespace(stream_chat=stream,EngineSession=Session,serve=serve)
    service=SimpleNamespace(is_writable_bot_policy=lambda p:False,browser_route_ready=lambda b:False,
        gmail_route_ready=lambda b:False,email_calendar_route_ready=lambda b:False)
    monkeypatch.setattr(routing,'_is_explicit_browser_subtask',lambda c,d,s,frame:frame['target_bot_id']=='browser')
    monkeypatch.setattr(routing,'_submit_routed_gmail',lambda *a:None)
    monkeypatch.setattr(routing,'_submit_routed_email_calendar',lambda *a:None)
    routing.install(chat,service)
    session=Session()
    session._jev_bot_target_hint={'selected_bot_id':'mail-one','coordinator_bot_id':'chief','consumed':False}
    session._handle_delegation({'target_bot_id':'browser','task':'Read an actual source link'})
    assert not session._jev_bot_target_hint['consumed']
    session._handle_delegation({'target_bot_id':'calendar','task':'Read the school email'})
    assert calls[-1]['target_bot_id']=='mail-one'
