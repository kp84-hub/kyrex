"""Workout continuations use the existing durable Calendar task and review."""
import asyncio
import json

import pytest

import bots
import chat_api
import chat_service as chat
import dev_bot
import serve
from task_store import CloudTaskStore, TaskWorker
from test_level6_calendar_batch import LINES


REQUEST = 'Can you add these to my calendar'


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'calendar-handoff-tests')
    registry = {
        'chief': {'id': 'chief', 'owner': 'alice', 'status': 'running',
                  'policy': serve.COORDINATOR_PRESET, 'rift': str(tmp_path)},
        'calendar': {'id': 'calendar', 'owner': 'alice', 'status': 'running',
                     'policy': serve.calendar_preset_policy(), 'rift': str(tmp_path)},
    }
    monkeypatch.setattr(bots, 'load_bots', lambda: registry)
    store = CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(chat, '_task_store', lambda: store)
    monkeypatch.setattr(chat, '_resolve_provider', lambda *a, **k: pytest.fail('Reached LLM'))
    monkeypatch.setattr(chat.bot_provider, 'resolve_bot_provider', lambda *a, **k: pytest.fail('Reached Bot LLM'))
    monkeypatch.setattr(chat, 'EngineSession', lambda *a, **k: pytest.fail('Started engine'))
    conv = chat.create_conversation('alice', bot_id='chief')
    # Same shape as the screenshot: fenced message body, separate trainer rows.
    body = '\n'.join(line.replace(' — trainer: ', '\nTrainer: ') for line in LINES)
    chat._append_message('alice', conv, 'assistant',
        'Preview only — nothing sent.\n\n```text\n#L6Workout\n'
        '🏋️ Level 6 — Workout Week\n' + body + '\n```')
    conv['gmail_selected'] = {'id': 'unrelated-email', 'facts': {}}
    chat._write('alice', conv)
    return store, conv['conversation_id'], registry


def test_screenshot_request_streams_existing_review_and_persists_result(handoff):
    store, cid, _ = handoff
    executions = []

    def execute(**kwargs):
        assert kwargs['executor_prefix'] == 'level6'
        assert kwargs['task_text'] == serve.LEVEL6_CALENDAR_BATCH_REQUEST
        assert kwargs['chat_id'] == 'alice' and kwargs['session_key'] == 'calendar'
        executions.append(kwargs['task_id'])
        kwargs['on_approval']('review-1', 1, 'private-review-token',
                              'Add 6 workouts', 'Six exact event titles and dates')
        assert store.get(kwargs['task_id'])['status'] == 'awaiting_approval'
        kwargs['on_approval_resolved']('review-1', 'approved')
        kwargs['on_result']({'status': 'no_changes', 'mode': 'level6_calendar_batch',
                              'count': 6, 'final_response': 'Added 6 Level 6 workouts to your calendar.'})

    worker = TaskWorker(store, worker_id='calendar-handoff', executor=execute,
                        idle_sleep=0.01, heartbeat_interval=0.01)
    worker.start()

    async def run():
        source = chat.stream_chat('alice', cid, REQUEST, request_id='calendar-request')
        return [json.loads(frame.split('data: ', 1)[1])
                async for frame in chat_api._drive_stream(source, 'calendar-request', cid)]

    try:
        frames = asyncio.run(run())
    finally:
        worker.stop()
    assert frames[-1]['type'] == 'done'
    assert frames[-1]['content'] == 'Added 6 Level 6 workouts to your calendar.'
    approvals = [f for f in frames if f['type'] == 'approval_request']
    assert len(approvals) == 1 and approvals[0]['task_id'] == executions[0]
    assert 'private-review-token' not in json.dumps(frames)
    saved = chat.get_conversation('alice', cid)
    assert saved['messages'][-1]['content'] == frames[-1]['content']
    assert saved['messages'][-1]['id'] == f'task-{executions[0]}-result'
    assert [m['content'] for m in saved['messages'] if m['role'] == 'user'] == [REQUEST]
    assert len(store.tasks_for_conversation(cid, 'alice')) == 1
    assert store.tasks_for_conversation(cid, 'bob') == []
    # A replay after the completed result replaced the preview still resolves
    # to the accepted task, without a model, worker or new Calendar execution.
    replay = asyncio.run(run())
    assert replay[-1] == frames[-1]
    assert len(store.tasks_for_conversation(cid, 'alice')) == 1
    assert len(executions) == 1
    async def changed_request():
        return [f async for f in chat.stream_chat('alice', cid,
            'Add it to a different calendar', request_id='calendar-request')]
    with pytest.raises(chat.ChatUnavailable, match='already used'):
        asyncio.run(changed_request())


def test_retries_reattach_to_pending_or_same_completed_request(handoff):
    store, cid, registry = handoff
    calendar = registry['calendar']
    first = dev_bot.submit_level6_calendar_batch_task(
        'alice', calendar, store, cid, request_id='one')
    assert store.claim_next('worker')['task_id'] == first
    store.persist_approval_request(first, 'calendar', 'review-1', 1, 'secret', 'Review', 'Exact dates')
    # A new HTTP request from Retry still observes the pending review.
    again = dev_bot.submit_level6_calendar_batch_task(
        'alice', calendar, store, cid, request_id='two')
    assert again == first and store.get(first)['status'] == 'awaiting_approval'
    store.resolve_approval_request(first, 'review-1', 'approved')
    store.complete(first, {'final_response': 'Added approved workouts.'})
    # A transport replay of the accepted request cannot execute it again.
    assert dev_bot.submit_level6_calendar_batch_task(
        'alice', calendar, store, cid, request_id='one') == first
    assert len(store.tasks_for_conversation(cid, 'alice')) == 1
    assert store.claim_next('worker') is None
    with pytest.raises(dev_bot.DevBotError):
        dev_bot.submit_level6_calendar_batch_task('bob', calendar, store, cid, request_id='one')


def test_legacy_delegated_calendar_work_cannot_be_duplicated(handoff):
    store, cid, registry = handoff
    did = store.create_delegation(owner='alice', coordinator_bot_id='chief',
        target_bot_id='calendar', task_text=serve.LEVEL6_CALENDAR_BATCH_REQUEST,
        parent_conversation_id=cid, executor_prefix='level6')
    tid = store.submit('calendar', serve.LEVEL6_CALENDAR_BATCH_REQUEST,
        executor_prefix='level6', bot_id='calendar', chat_id='alice',
        conversation_id='specialist-chat', parent_delegation_id=did, resolve_bot=False)
    store.set_delegation_status(did, 'queued', task_id=tid)
    with pytest.raises(dev_bot.DevBotError, match='already in progress'):
        dev_bot.submit_level6_calendar_batch_task('alice', registry['calendar'],
            store, cid, request_id='retry-old-handoff')
    assert len(store.list_tasks()) == 1 and store.get(tid)['status'] == 'queued'


@pytest.mark.parametrize('problem', ['missing', 'foreign', 'stopped', 'restricted', 'ambiguous', 'workspace', 'cancelled'])
def test_unavailable_targets_and_cancelled_requests_cannot_submit(handoff, problem):
    store, cid, registry = handoff
    kwargs = {}
    if problem == 'missing':
        del registry['calendar']
    elif problem == 'foreign':
        registry['calendar']['owner'] = 'bob'
    elif problem == 'stopped':
        registry['calendar']['status'] = 'stopped'
    elif problem == 'restricted':
        registry['calendar']['policy'] = {'cal:list': 0, 'cal:create': 'deny'}
    elif problem == 'ambiguous':
        registry['second'] = {**registry['calendar'], 'id': 'second'}
    elif problem == 'workspace':
        kwargs['workspace_id'] = 'another-workspace'
    else:
        kwargs['cancel_event'] = asyncio.Event()
        kwargs['cancel_event'].set()

    async def run():
        return [f async for f in chat.stream_chat('alice', cid, REQUEST, **kwargs)]

    if problem == 'cancelled':
        assert asyncio.run(run())[-1]['status'] == 'cancelled'
    else:
        with pytest.raises(chat.ChatUnavailable):
            asyncio.run(run())
    assert store.tasks_for_conversation(cid, 'alice') == []


PASTED = ('Can you add these to my calendar #L6Workout 🏋️ Level 6 — Workout Week '
          'Monday 2026-11-02 — FULL BODY STRENGTH CIRCUIT Trainer: Donna Albertone '
          'Tuesday 2026-11-03 — BALANCE, CORE, & AGILITY + Trainer: Emmitt Terrell '
          'Wednesday 2026-11-04 — MUSCULAR ENDURANCE TRAINING Trainer: Donna Albertone '
          'Thursday 2026-11-05 — ARMS & SHOULDERS Trainer: Austin Ordonez '
          'Friday 2026-11-06 — CARDIO IS HARDIO Trainer: Austin Ordonez '
          'Saturday 2026-11-07 — WHITEBOARD ROULETTE Trainer: Staff Level 6')


def test_flattened_pasted_week_reaches_durable_calendar_task_without_email_or_model(handoff, monkeypatch):
    import level6_calendar_batch as batch
    store, cid, registry = handoff
    conv=chat.get_conversation('alice',cid)
    conv['messages']=[{'id':'previous','role':'assistant','content':
        'What event, date, and start time should I use? Include an end time or duration, or say all day.'}]
    conv['calendar_create_draft']={'title':'wrong old event'}
    chat._write('alice',conv)
    calls=[]
    async def observe(*args,**kwargs):
        assert kwargs['mode']=='level6_calendar_batch'
        payload=kwargs['pasted_workout_task']
        tid=dev_bot.submit_level6_calendar_batch_task('alice',registry['calendar'],store,cid,
                                                     request_id=kwargs['request_id'],pasted_task=payload)
        calls.append((tid,payload))
        yield {'type':'status','status':'complete','content':'Ready for exact Calendar review.'}
    monkeypatch.setattr(chat,'_stream_writable_bot_task',observe)
    async def run(text=PASTED):
        return [frame async for frame in chat.stream_chat('alice',cid,text,request_id='pasted-request')]
    assert asyncio.run(run())[-1]['content']=='Ready for exact Calendar review.'
    intents=batch.pasted_intents_from_task(calls[0][1])
    assert len(intents)==6 and intents[0]['start']=='2026-11-02' and intents[-1]['start']=='2026-11-07'
    assert all(i['all_day'] for i in intents)
    assert intents[1]['title']=='Level 6 Workout: BALANCE, CORE, & AGILITY +'
    assert asyncio.run(run())[-1]['content']=='Ready for exact Calendar review.'
    assert calls[1][0]==calls[0][0] and len(store.tasks_for_conversation(cid,'alice'))==1
    with pytest.raises(chat.ChatUnavailable,match='already used'):
        asyncio.run(run(PASTED.replace('ARMS & SHOULDERS','ANOTHER WORKOUT')))
    with pytest.raises(chat.ChatUnavailable,match='already used'):
        asyncio.run(run(PASTED[:PASTED.index('Saturday 2026')]))


@pytest.mark.parametrize('fault',['missing_day','wrong_date','wrong_weekday','duplicate_day','timed'])
def test_pasted_week_invalid_rows_never_submit(handoff,fault):
    store,cid,_=handoff
    text=PASTED
    if fault=='missing_day':text=text[:text.index('Saturday 2026')]
    elif fault=='wrong_date':text=text.replace('2026-11-04','2026-12-04')
    elif fault=='wrong_weekday':text=text.replace('Tuesday 2026','Monday 2026')
    elif fault=='duplicate_day':text=text.replace('2026-11-03','2026-11-02')
    else:text=text.replace('Trainer: Staff Level 6','Trainer: Staff Level 6 at 8:30am')
    async def run():return [frame async for frame in chat.stream_chat('alice',cid,text,request_id='invalid')]
    frames=asyncio.run(run())
    assert frames[-1]['status']=='complete' and store.tasks_for_conversation(cid,'alice')==[]


def test_pasted_workout_contract_binds_exact_dates_and_titles():
    import level6_calendar_batch as batch
    from job_contracts import validate_request_route,JobContractError
    payload=batch.pasted_task(PASTED)
    validate_request_route(PASTED,'level6',payload)
    with pytest.raises(JobContractError):validate_request_route(PASTED,'level6',serve.LEVEL6_CALENDAR_BATCH_REQUEST)
    with pytest.raises(JobContractError):validate_request_route(PASTED,'level6',payload.replace('ARMS & SHOULDERS','WRONG'))
    for text in ('Do not '+PASTED,'Explain '+PASTED,PASTED.replace('my calendar','Bob\'s calendar'),
                 PASTED.replace('my calendar','my calendar and send a message')):
        assert batch.pasted_task(text) is None


@pytest.mark.parametrize('fault',['decline','approve','cancel','claim','policy','owner','account','destination','conflict'])
def test_pasted_executor_reviews_exact_all_day_events_without_browser_and_skips_matches(handoff,monkeypatch,fault):
    approved = fault != 'decline'
    from types import SimpleNamespace
    import level6_calendar_batch as batch
    import connectors,task_store,level6_weekly
    store,cid,registry=handoff
    payload=batch.pasted_task(PASTED)
    tid=dev_bot.submit_level6_calendar_batch_task('alice',registry['calendar'],store,cid,request_id='execute',pasted_task=payload)
    store.claim_next('calendar-worker')
    monkeypatch.setattr(task_store,'CloudTaskStore',lambda *a,**kw:store)
    monkeypatch.setattr(bots,'get_bot',lambda bid:registry.get(bid))
    monkeypatch.setattr(level6_weekly,'run_weekly',lambda **kw:pytest.fail('Pasted facts must not be replaced by the current Facebook week'))
    existing=[];writes=[];reviews=[];results=[];live={'calendar':'primary','connected_at':123}
    def create(event,**kwargs):
        assert reviews and approved
        assert kwargs['expected_binding']=={'calendar':'primary','connected_at':123}
        writes.append(event);existing.append(event)
        return {'id':'fixture-event'}
    connector=SimpleNamespace(calendar=lambda owner:SimpleNamespace(events=lambda **kw:list(existing)),
        preferred_calendar=lambda owner:live['calendar'],status=lambda *a:live,
        calendar_writer=lambda owner:SimpleNamespace(create_event=create))
    monkeypatch.setattr(connectors,'default_store',lambda:connector)
    def review(*a,**kw):
        reviews.append(kw['detail'])
        if fault=='cancel':store.request_cancel(tid)
        if fault=='claim':store.set_status(tid,'running',run_id='replaced-claim')
        if fault=='policy':registry['calendar']['policy']={'cal:create':'deny'}
        if fault=='owner':registry['calendar']['owner']='bob'
        if fault=='account':live['connected_at']=456
        if fault=='destination':live['calendar']='other'
        if fault=='conflict':existing.append({'summary':'Level 6 Workout: WRONG','start':{'date':'2026-11-02'},'end':{'date':'2026-11-03'}})
        return approved
    monkeypatch.setattr(serve,'_request_in_process_approval',review)
    ctx=SimpleNamespace(bot_owner='alice',bot_id='calendar',policy=registry['calendar']['policy'])
    serve._run_level6_calendar_batch_task(ctx,'alice',payload,tid,lambda *a:None,on_result=results.append)
    assert len(reviews)==1 and 'not independently verified' in reviews[0]
    assert '2026-11-02 (all day): Level 6 Workout: FULL BODY STRENGTH CIRCUIT' in reviews[0]
    assert len(writes)==(6 if fault=='approve' else 0)
    if fault=='approve':
        assert writes[0]['start']=={'date':'2026-11-02'} and writes[-1]['end']=={'date':'2026-11-08'}
        serve._run_level6_calendar_batch_task(ctx,'alice',payload,tid,lambda *a:None,on_result=results.append)
        assert len(writes)==6 and len(reviews)==1 and 'already' in results[-1]['final_response']


def test_pasted_payload_accepts_multiline_phone_copy_and_rejects_payload_overrides():
    import level6_calendar_batch as batch
    lines=batch.pasted_week(PASTED)
    multiline='Can you add these to my calendar\n```text\n#L6Workout\n🏋️ Level 6 — Workout Week\n'+'\n'.join(lines)+'\n```'
    assert batch.pasted_week(multiline)==lines
    payload=batch.pasted_task(PASTED)
    with pytest.raises(batch.CalendarBatchError):batch.pasted_intents_from_task(payload[:-1]+',"calendar":"other"}')
