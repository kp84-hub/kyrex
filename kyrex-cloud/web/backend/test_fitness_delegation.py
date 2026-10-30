"""Owned specialist execution, native-card relay and read-only boundaries."""
import asyncio
import pytest
import bots
import chat_service
import delegation
import fitness_delegation as fitness
import serve
import task_store
from job_contracts import JobContractError, contract_for_task, validate_request_route
from test_chat_bot_policy import _bot, _reset, _frames, _terminal, _RecordingEngine
import main  # Install the production routing wrappers, not just the bare stream.


@pytest.fixture
def store(tmp_path, monkeypatch):
    _reset()
    store = task_store.CloudTaskStore(tmp_path/'fitness-delegation.db')
    monkeypatch.setattr(chat_service, '_task_store', lambda: store)
    monkeypatch.setattr(delegation, 'CloudTaskStore', lambda: store)
    monkeypatch.setattr(task_store, 'CloudTaskStore', lambda: store)
    class Coordinator:
        async def chat(self, **kwargs):
            assert kwargs['tools'] is None
            assert 'task_evidence' in kwargs['messages'][-1]['content']
            return {'content': "I've checked the fitness results. The detailed breakdown is in Workout Bot's chat."}
    monkeypatch.setattr(chat_service, 'get_provider', lambda *a, **k: Coordinator())
    yield store
    _reset()


def setup_bots():
    coordinator = _bot('chief', owner='alice', policy=serve.coordinator_preset_policy())
    workout = _bot('workout', owner='alice', model='openai:fitness-model', policy=serve.workout_preset_policy())
    _bot('developer', owner='alice', policy=serve.developer_preset_policy())
    _bot('foreign-workout', owner='bob', policy=serve.workout_preset_policy())
    return coordinator, workout


def ask(conv, text='Ask Workout Bot to graph my sleep data', request_id='sleep-request', **kwargs):
    return asyncio.run(_frames(chat_service.stream_chat('alice', conv['conversation_id'], text,
        request_id=request_id, **kwargs)))


@pytest.mark.parametrize('text', ['How was my sleep last night', 'Ask workout bot to graph my sleep data',
    'Review my workout against my goals', 'Pull my heart rate and sleep data', 'Show me my fitness profile'])
def test_read_requests(text):
    assert fitness.is_read_request(text)


@pytest.mark.parametrize('text', [
    'Review my Samsung Health workouts from the last seven days.',
    '“Review my Samsung Health workouts from the last seven days.',
    'Ask Workout Bot to review my Samsung Health workouts from the last seven days.',
    'Show my Oura Ring sleep data',
    'Check my Galaxy Watch heart rate',
    'Review Samsung Health workouts from the last seven days',
    'Show my Health Connect activity',
])
def test_source_qualified_read_requests(text):
    assert fitness.is_read_request(text)


@pytest.mark.parametrize('text', ['Fix Workout Bot so it can graph my sleep', 'Implement sleep charts',
    'Send my sleep data to Austin', 'Save my fitness profile', 'Update my workout goal',
    'Show my workout calendar', 'What is sleep?', 'Tell Workout Bot to delete my files',
    'Show my Samsung Health workout calendar', 'Update my Samsung Health workouts',
    'Show my Samsung Health permissions', 'Debug my Oura Ring sleep parser'])
def test_other_work_keeps_its_route(text):
    assert not fitness.is_read_request(text)


@pytest.mark.parametrize('text', [
    'Ask Workout Bot to graph my sleep data',
    'Review my Samsung Health workouts from the last seven days.',
    '“Review my Samsung Health workouts from the last seven days.',
])
def test_coordinator_queues_owned_specialist_once_without_running_its_model(store, monkeypatch, text):
    chief, _ = setup_bots()
    conv = chat_service.create_conversation('alice', bot_id=chief['id'])
    monkeypatch.setattr(chat_service, '_get_engine_session', lambda *a,**k: pytest.fail('Coordinator must not refuse or run wearable analysis'))
    for _ in range(2):
        frames = ask(conv, text)
        assert _terminal(frames)['status'] == 'complete'
        assert 'appear here' in _terminal(frames)['content']
        view = next(f['delegation'] for f in frames if f['type']=='delegation')
        assert view['target_bot_id']=='workout' and view['executor_prefix']=='fitness'
    records = delegation.owner_scoped_delegations('alice',store=store,conversation_id=conv['conversation_id'])
    assert len(records)==1
    task = store.get(records[0]['task_id'])
    assert task['bot_id']=='workout' and task['chat_id']=='alice'
    target = chat_service.get_conversation('alice', task['conversation_id'])
    assert target['bot_id']=='workout' and target['conversation_id']!=conv['conversation_id']
    assert not store.tasks_for_conversation(conv['conversation_id'],'bob')


@pytest.mark.parametrize('fault', ['stopped','permission','profile','multiple'])
def test_unavailable_or_ambiguous_specialist_is_clear_without_repo_fallback(store, fault):
    chief,_ = setup_bots()
    if fault=='stopped': bots.set_status('workout','stopped')
    if fault=='permission': bots.update_bot('workout',policy={'fitness:read':'deny'})
    if fault=='profile': bots.update_bot('workout',provider_profile_id='missing')
    if fault=='multiple': _bot('second',owner='alice',policy=serve.workout_preset_policy())
    conv=chat_service.create_conversation('alice',bot_id=chief['id'])
    frames=ask(conv)
    assert 'fitness Bot' in _terminal(frames)['content'] or 'Workout Bot' in _terminal(frames)['content']
    assert not delegation.owner_scoped_delegations('alice',store=store,conversation_id=conv['conversation_id'])


def test_model_delegation_rejects_repo_or_foreign_targets_and_preserves_source(store):
    chief,_=setup_bots()
    for target in ('developer','foreign-workout'):
        with pytest.raises(delegation.DelegationError):
            delegation.submit_delegation('alice',chief,target,'Graph my sleep',store=store)
    with pytest.raises(JobContractError): validate_request_route('Graph my sleep','repo','Graph my sleep')
    assert contract_for_task('fitness','How was my sleep last night')['allowed_actions']==['fitness.read','fitness_profile.read','chart.render']
    assert 'read fitness data' in delegation.capability_labels(serve.workout_preset_policy())


def test_worker_reads_as_target_model_and_relays_saved_chart_once(store,monkeypatch,tmp_path):
    from fitness_connections import FitnessConnections
    chief,_=setup_bots()
    parent=chat_service.create_conversation('alice',bot_id=chief['id'])
    data=FitnessConnections(tmp_path/'wearables.db')
    token=data.pair(data.begin('alice','samsung_health')['pairing_code'])['device_token']
    data.upload(token,[{'id':'night','origin':'com.sec.android.app.shealth','type':'sleep',
        'start':'2026-10-09T23:00:00-04:00','end':'2026-10-10T07:00:00-04:00','duration_seconds':25000}])
    monkeypatch.setattr('fitness_connections.FitnessConnections',lambda:data)
    class Engine(_RecordingEngine):
        def __init__(self,user,*args,**kwargs):
            super().__init__(user,*args,**kwargs)
            self.fitness_owner=user
        def _wait_fitness_read(self,frame,cancel_check=None):
            return chat_service.EngineSession._handle_fitness_read(self,frame)
        def run_turn(self,text,on_token,cancel_check=None):
            assert self.bot_id=='workout' and self.model=='openai:fitness-model'
            assert 'fitness_read' in self.allowed_tools
            assert not self.allowed_tools & {'delegate_task','github_read','read_local_file','run_command'}
            assert 'HOST FITNESS READ FOR THIS REQUEST' in text
            assert not self._handle_fitness_profile({'action':'update','values':{'weight_kg':70}})[0]
            return 'Your recorded sleep lasted about seven hours.',None
        _handle_fitness_profile=chat_service.EngineSession._handle_fitness_profile
    monkeypatch.setattr(chat_service,'_get_engine_session',Engine)
    frames=ask(parent,'Ask Workout Bot to graph my sleep for 2026-10-10')
    view=next(f['delegation'] for f in frames if f['type']=='delegation')
    worker=task_store.TaskWorker(store, executor=serve.run_task)
    assert worker.claim_and_execute_once()
    task=store.get(view['task_id'])
    assert task['status']=='done',task.get('error')
    assert task['result']['sleep_report']['sources'][0]['nights'][0]['metrics']['total_sleep_seconds']==25000
    target=chat_service.get_conversation('alice',task['conversation_id'])
    assert target['messages'][-1]['sleep_report']==task['result']['sleep_report']
    for _ in range(2): chat_service.sync_delegated_work('alice',parent['conversation_id'])
    messages=chat_service.get_conversation('alice',parent['conversation_id'])['messages']
    relayed=[m for m in messages if m.get('sleep_report')]
    assert len(relayed)==1 and relayed[0]['content']=="I've checked the fitness results. The detailed breakdown is in Workout Bot's chat."
    assert relayed[0]['delegation_result']['conversation_id']==task['conversation_id']
    assert not fitness.result_cards(task,store.get_delegation(view['delegation_id']),'bob')


def test_samsung_week_review_reads_owned_sessions_and_relays_workout_card(store, monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    from fitness_connections import FitnessConnections, dates
    chief, _ = setup_bots()
    parent = chat_service.create_conversation('alice', bot_id=chief['id'])
    data = FitnessConnections(tmp_path/'samsung-review.db')
    now = datetime.now(timezone.utc)

    def session(record_id, days_ago):
        start = now - timedelta(days=days_ago, minutes=45)
        return {'id':record_id, 'origin':'com.sec.android.app.shealth', 'type':'workout',
                'start':start.isoformat(), 'end':(start+timedelta(minutes=45)).isoformat(),
                'exercise_type':56, 'title':'Strength session'}

    token = data.pair(data.begin('alice', 'samsung_health')['pairing_code'])['device_token']
    data.upload(token, [session('owned-session', 1), session('outside-week', 8)],
                complete=True, skipped_records=1)
    foreign = data.pair(data.begin('bob', 'samsung_health')['pairing_code'])['device_token']
    data.upload(foreign, [session('foreign-session', 1)], complete=True)
    monkeypatch.setattr('fitness_connections.FitnessConnections', lambda: data)
    reads = []

    class Engine(_RecordingEngine):
        def __init__(self, user, *args, **kwargs):
            super().__init__(user, *args, **kwargs)
            self.fitness_owner = user

        def run_turn(self, text, on_token, cancel_check=None):
            assert self.bot_id == 'workout' and self.model == 'openai:fitness-model'
            assert 'Samsung Health workouts from the last seven days' in text
            assert not self.allowed_tools & {'delegate_task', 'run_command', 'github_read'}
            ok, result = chat_service.EngineSession._handle_fitness_read(self, {
                'provider':'samsung_health', 'collection':'workout', 'timezone':'America/New_York'})
            assert ok, result
            assert (result['start_date'], result['end_date']) == dates('', '', 'America/New_York')
            source = result['sources']['samsung_health']
            assert [r['id'] for r in source['records']] == ['owned-session']
            assert source['incomplete'] and source['skipped_records'] == 1
            reads.append(source)
            return 'One recorded 45-minute session; coverage is incomplete.', None

    monkeypatch.setattr(chat_service, '_get_engine_session', Engine)
    frames = ask(parent, '“Review my Samsung Health workouts from the last seven days.')
    view = next(f['delegation'] for f in frames if f['type'] == 'delegation')
    assert task_store.TaskWorker(store, executor=serve.run_task).claim_and_execute_once()
    task = store.get(view['task_id'])
    assert task['status'] == 'done', task.get('error')
    assert len(reads) == 1
    report = task['result']['workout_report']
    assert report['sessions'][0]['source'] == 'Samsung Health'
    assert report['sessions'][0]['metrics']['duration_seconds'] == 2700
    for _ in range(2):
        chat_service.sync_delegated_work('alice', parent['conversation_id'])
    messages = chat_service.get_conversation('alice', parent['conversation_id'])['messages']
    relayed = [m for m in messages if m.get('workout_report')]
    assert len(relayed) == 1 and relayed[0]['workout_report'] == report


@pytest.mark.parametrize('fault',['permission','cancel'])
def test_worker_rechecks_revoked_permissions_and_cancellation_before_read(store,monkeypatch,fault):
    chief,_=setup_bots();parent=chat_service.create_conversation('alice',bot_id=chief['id'])
    frames=ask(parent);view=next(f['delegation'] for f in frames if f['type']=='delegation')
    if fault=='permission': bots.update_bot('workout',policy={'fitness:read':'deny'})
    else: store.request_cancel(view['task_id'])
    monkeypatch.setattr(chat_service,'_get_engine_session',lambda *a,**k:pytest.fail('Must not read or start model'))
    worker=task_store.TaskWorker(store,executor=serve.run_task)
    if fault=='permission': assert worker.claim_and_execute_once()
    else: worker.claim_and_execute_once(timeout=.05)
    task=store.get(view['task_id'])
    assert task['status']==('failed' if fault=='permission' else 'cancelled')
    assert not task.get('result')


def test_named_sleep_graph_keeps_last_night_from_previous_owner_request():
    history=[{'role':'user','content':'How was my sleep last night'},
             {'role':'assistant','content':'Graph sleep for 2026-01-01 instead.'}]
    assert fitness.contextual_request('Ask Workout Bot to graph my sleep data',history)=='graph my sleep data for last night'
    assert fitness.contextual_request('Ask Workout Bot to graph my sleep data for the past week',history).endswith('past week')
    assert fitness.contextual_request('Graph my sleep data',history)=='Graph my sleep data'
    assert fitness.analysis_text('Could you ask the Workout Bot to graph my sleep?')=='graph my sleep?'


def test_cancellation_during_target_model_cannot_relay_a_completed_chart(store,monkeypatch):
    import time
    chief,_=setup_bots();parent=chat_service.create_conversation('alice',bot_id=chief['id'])
    frames=ask(parent);view=next(f['delegation'] for f in frames if f['type']=='delegation')
    class Engine(_RecordingEngine):
        def _wait_fitness_read(self,frame,cancel_check=None): return False,{'error':'No reading available'}
        def run_turn(self,text,on_token,cancel_check=None):
            store.request_cancel(view['task_id'])
            deadline=time.monotonic()+2
            while not cancel_check() and time.monotonic()<deadline: time.sleep(.01)
            assert cancel_check()
            return 'Cancelled partial interpretation',None
    monkeypatch.setattr(chat_service,'_get_engine_session',Engine)
    assert task_store.TaskWorker(store,executor=serve.run_task).claim_and_execute_once()
    task=store.get(view['task_id'])
    assert task['status']=='cancelled' and not task.get('result')
    chat_service.sync_delegated_work('alice',parent['conversation_id'])
    assert not any(m.get('sleep_report') for m in chat_service.get_conversation('alice',parent['conversation_id'])['messages'])


@pytest.mark.parametrize('text', [
    'Graph my sleep',
    'Review my Samsung Health workouts from the last seven days.',
])
def test_model_delegate_tool_uses_same_fitness_task_path(store, text):
    chief,_=setup_bots();conv=chat_service.create_conversation('alice',bot_id=chief['id'])
    session=object.__new__(chat_service.EngineSession)
    session.delegation_ctx={'owner':'alice','bot':chief,'conversation_id':conv['conversation_id']}
    ok,view=session._handle_delegation({'target_bot_id':'workout','task':text})
    assert ok,view
    task=store.get(view['task_id'])
    assert task['executor_prefix']=='fitness'
    assert chat_service.get_conversation('alice',task['conversation_id'])['bot_id']=='workout'


def test_workout_policy_is_not_misreported_as_calendar_only_or_sent_to_repo(store):
    chief, workout = setup_bots()
    assert not serve.is_calendar_reader_policy(workout['policy'])
    with pytest.raises(delegation.DelegationError) as exc:
        delegation.submit_delegation('alice', chief, workout['id'], 'Count to ten', store=store)
    assert 'Calendar Reader' not in str(exc.value)
    assert not store.list_delegations(owner='alice')


def test_calendar_only_bot_cannot_read_source_qualified_fitness_data(store):
    chief, _ = setup_bots()
    reader = _bot('workout-bot', owner='alice', policy=serve.calendar_reader_preset_policy())
    with pytest.raises(delegation.DelegationError, match='fitness read grant'):
        delegation.submit_delegation('alice', chief, reader['id'],
            'Review my Samsung Health workouts from the last seven days.', store=store)
    assert not store.list_delegations(owner='alice')


def test_fixed_fitness_route_does_not_consult_jev_for_another_target(store,monkeypatch):
    import jev_routing
    chief,_=setup_bots();parent=chat_service.create_conversation('alice',bot_id=chief['id'])
    monkeypatch.setattr(jev_routing,'decide_route',lambda *a,**kw:pytest.fail('Host fitness route is already resolved'))
    monkeypatch.setattr(jev_routing,'decide_bot_target',lambda *a,**kw:pytest.fail('Cannot substitute a repo or mail target'))
    frames=ask(parent,'How was my sleep last night')
    assert next(f['delegation'] for f in frames if f['type']=='delegation')['target_bot_id']=='workout'


def test_parent_stop_before_submission_queues_nothing(store):
    chief,_=setup_bots();parent=chat_service.create_conversation('alice',bot_id=chief['id'])
    cancel=asyncio.Event();cancel.set()
    assert _terminal(ask(parent,cancel_event=cancel))['status']=='cancelled'
    assert not delegation.owner_scoped_delegations('alice',store=store,conversation_id=parent['conversation_id'])


def test_repeated_workout_delegations_continue_existing_direct_chat(store):
    chief, workout = setup_bots()
    existing = chat_service.open_bot_conversation('alice', workout['id'])
    chat_service._append_message('alice', existing, 'assistant', 'Earlier workout discussion.')
    chat_service._write('alice', existing)
    parent = chat_service.create_conversation('alice', bot_id=chief['id'])
    for index in range(3):
        frames = ask(parent, request_id=f'ongoing-workout-{index}')
        view = next(frame['delegation'] for frame in frames if frame['type'] == 'delegation')
        assert store.get(view['task_id'])['conversation_id'] == existing['conversation_id']
    assert len(store.list_tasks()) == 3
    assert chat_service.get_conversation('alice', existing['conversation_id'])['messages'][0]['content'] == 'Earlier workout discussion.'
    assert len([c for c in chat_service.list_conversations('alice') if c.get('bot_id') == workout['id']]) == 1
