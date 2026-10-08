"""Offline regressions for natural scheduling and field-preserving updates."""
import asyncio
import copy
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
import cal_writer
import cal_editor
import connectors
import calendar_editor_executor
import calendar_writer_executor
import bots
import chat_service
import dev_bot
import delegation
import serve
import shared_connected_tools as shared
from task_store import CloudTaskStore


@pytest.fixture(autouse=True)
def install_shared():
    shared.install(chat_service, dev_bot)


def test_original_create_and_duration_followup_use_ny_date():
    now = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)  # still Oct 8 in New York
    for text in ('Haper doctor appointment on Oct. 8th at 3:45 PM',
                 'Lets add Haper doctor appointment to the calendar for 3:45pm today'):
        with pytest.raises(cal_writer.CalendarClarification) as error:
            cal_writer.parse_create_request(text, now=now)
        assert 'How long' in str(error.value)
        intent = cal_writer.parse_create_request('30 minutes', pending=error.value.draft, now=datetime(2026,10,10))
        assert intent == {'title':'Haper doctor appointment', 'start':'2026-10-08T15:45:00', 'end':'2026-10-08T16:15:00', 'all_day':False}
        assert serve.natural_calendar_command(text) is None
    assert cal_writer.parse_create_request('create Meeting on 2026-10-08 from 09:00 to 10:00')['start'].endswith('09:00:00')


def execute(monkeypatch, executor, task, decision='ALLOW\nAPPROVED\n', owner='alice'):
    output = io.StringIO()
    monkeypatch.setattr('sys.argv', ['executor', '--task', json.dumps(task) if isinstance(task, dict) else task])
    monkeypatch.setattr('sys.stdin', io.StringIO(decision))
    monkeypatch.setattr('sys.stdout', output)
    monkeypatch.setenv('KYREX_BOT_OWNER', owner)
    executor.main()
    frames = [line.split(':',1) for line in output.getvalue().splitlines()]
    return {kind:json.loads(value) for kind,value in frames}


def event_fixture():
    return {'id':'tb77n72a1191vru1qm71hok4b4', 'etag':'"v1"', 'summary':'Haper doctor appointment',
            'description':'Bring insurance card', 'location':'Old location',
            'start':{'dateTime':'2026-10-08T15:45:00-04:00','timeZone':'America/New_York'},
            'end':{'dateTime':'2026-10-08T16:15:00-04:00','timeZone':'America/New_York'},
            'attendees':[{'email':'example@example.invalid'}], 'recurrence':['RRULE:FREQ=WEEKLY'], 'reminders':{'useDefault':True}}


class Store:
    def __init__(self):
        self.event = event_fixture()
        self.calls = []
        self.write = True
    def calendar_write_available(self, owner):
        assert owner == 'alice'
        return self.write
    def calendar_read_available(self, owner):
        assert owner == 'alice'
        return True
    def route_capability(self, owner, capability, provider):
        assert owner == 'alice' and capability in {'calendar.update','calendar.delete'}
        return {'bot_role':'calendar_editor'}
    def status(self, owner, provider):
        return {'scopes':[connectors.GOOGLE_CALENDAR_WRITE_SCOPE] if self.write else [connectors.GOOGLE_CALENDAR_READ_SCOPE]}
    def access_token(self, owner, provider):
        return 'fake-token-not-for-network'
    def preferred_calendar(self, owner, provider):
        assert owner == 'alice'
        return 'primary'
    def calendar_editor(self, owner):
        return connectors.CalendarEdit(self, owner, transport=self.transport)
    def calendar(self, owner):
        return NS(events=lambda **kw: [copy.deepcopy(self.event)])
    def transport(self, method, url, token, params=None, body=None, headers=None):
        self.calls.append((method, body, headers))
        assert url.endswith('/events/' + self.event['id'])
        if method == 'GET':
            return copy.deepcopy(self.event)
        assert method == 'PATCH'  # no POST, PUT, or DELETE may occur
        assert headers == {'If-Match':'"v1"'}
        self.event.update(body)
        self.event['etag'] = '"v2"'
        return copy.deepcopy(self.event)


@pytest.mark.parametrize('field', ['notes','location'])
def test_update_patches_only_requested_fields_and_preserves_notes(monkeypatch, field):
    store = Store()
    monkeypatch.setattr(connectors, 'default_store', lambda:store)
    original = copy.deepcopy(store.event)
    intent = {'op':'update','event_id':store.event['id'],field:'7608 Purfoy Rd, Fuquay-Varina, NC 27526'}
    frames = execute(monkeypatch, calendar_editor_executor, intent)
    assert frames['KYREX_RESULT_JSON']['status'] == 'ok'
    assert frames['KYREX_OPERATION']['op'] == 'cal.update'
    assert frames['KYREX_APPROVAL']['tier'] == 1
    patch = store.calls[-1][1]
    assert set(patch) == {'description' if field == 'notes' else 'location'}
    if field == 'notes':
        assert patch['description'] == original['description'] + '\n' + intent[field]
    for key in original.keys() - patch.keys() - {'etag'}:
        assert store.event[key] == original[key]
    assert [call[0] for call in store.calls] == ['GET','PATCH']


@pytest.mark.parametrize('decision,owner,write,expected_methods', [
    ('DENY\n','alice',True,[]), ('ALLOW\nDENIED\n','alice',True,['GET']),
    ('ALLOW\nAPPROVED\n','',True,[]), ('ALLOW\nAPPROVED\n','alice',False,[]),
])
def test_update_gate_and_scope_fail_closed(monkeypatch, decision, owner, write, expected_methods):
    store = Store()
    store.write = write
    monkeypatch.setattr(connectors, 'default_store', lambda:store)
    frames = execute(monkeypatch, calendar_editor_executor, {'op':'update','event_id':store.event['id'],'notes':'New note'}, decision, owner)
    assert frames['KYREX_RESULT_JSON']['status'] == 'error'
    assert [c[0] for c in store.calls] == expected_methods


def test_conflicting_version_reports_failure_without_success_receipt(monkeypatch):
    store = Store()
    base = store.transport
    def transport(method, *args, **kwargs):
        if method == 'PATCH':
            raise connectors.ConnectorUnavailable('HTTP 412 event changed')
        return base(method, *args, **kwargs)
    store.transport = transport
    monkeypatch.setattr(connectors, 'default_store', lambda:store)
    frames = execute(monkeypatch, calendar_editor_executor, {'op':'update','event_id':store.event['id'],'notes':'New note'})
    assert frames['KYREX_RESULT_JSON']['status'] == 'error'
    assert 'Reload' in frames['KYREX_RESULT_JSON']['errors'][0]
    assert store.event['description'] == 'Bring insurance card'


def test_ambiguous_target_and_unsupported_fields_never_write(monkeypatch):
    store = Store()
    monkeypatch.setattr(connectors, 'default_store', lambda:store)
    store.calendar = lambda owner:NS(events=lambda **kw:[store.event, {**store.event,'id':'different123'}])
    with pytest.raises(delegation.DelegationError, match='2 events'):
        shared._delegated_calendar_payload('alice', {}, 'add notes "address" to Haper doctor appointment')
    with pytest.raises(cal_editor.CalendarEditorError):
        cal_editor.validate_update_intent({'event_id':store.event['id'],'notes':'address','start':'changed'})
    assert store.calls == []


def test_notes_followup_and_explicit_replacement():
    context = {'event_id':event_fixture()['id']}
    with pytest.raises(cal_editor.CalendarEditClarification) as error:
        cal_editor.normalize_update_request('can you add notes to it. like the address? I will provide it if needed', context=context)
    intent = cal_editor.normalize_update_request('here is the address 7608 Purfoy Rd, Fuquay-Varina, NC 27526', pending=error.value.draft)
    assert intent['event_id'] == context['event_id']
    assert intent['notes'].startswith('7608 Purfoy')
    assert cal_editor.build_update_patch(event_fixture(), {**intent,'replace_notes':True}) == {'description':intent['notes']}


def test_delegation_retains_create_draft_and_new_event_target(monkeypatch, tmp_path):
    connector = Store()
    monkeypatch.setattr(connectors, 'default_store', lambda:connector)
    chief = {'id':'chief','owner':'alice','status':'running','policy':serve.coordinator_preset_policy(),'rift':''}
    target = {'id':'calendar','owner':'alice','status':'running','policy':{},'rift':''}
    monkeypatch.setattr(bots, 'load_bots', lambda:{'chief':chief,'calendar':target})
    store = CloudTaskStore(db_path=tmp_path/'tasks.db')
    first = delegation.submit_delegation('alice',chief,'calendar','add Haper doctor appointment on Oct. 8th at 3:45 PM',store=store,parent_conversation_id='conv')
    task = store.get(first['task_id'])
    assert task['executor_prefix'] == 'cal_write'
    result = execute(monkeypatch,calendar_writer_executor,task['task_text'])['KYREX_RESULT_JSON']
    assert result['status'] == 'needs_details'
    store.complete(first['task_id'], result)
    second = delegation.submit_delegation('alice',chief,'calendar','30 minutes',store=store,parent_conversation_id='conv')
    intent = json.loads(store.get(second['task_id'])['task_text'])
    assert intent['title'] == 'Haper doctor appointment'
    assert intent['start'].endswith('15:45:00')
    store.complete(second['task_id'], {'status':'ok','event_id':connector.event['id'],'final_response':'Created.'})
    third = delegation.submit_delegation('alice',chief,'calendar','add notes to it',store=store,parent_conversation_id='conv')
    result = execute(monkeypatch,calendar_editor_executor,store.get(third['task_id'])['task_text'])['KYREX_RESULT_JSON']
    assert result['status'] == 'needs_details'
    store.complete(third['task_id'], result)
    # Production callers may omit the store; state still comes from the same
    # durable owner/conversation store.
    monkeypatch.setattr('task_store.CloudTaskStore', lambda:store)
    fourth = delegation.submit_delegation('alice',chief,'calendar','here is the address 7608 Purfoy Rd, Fuquay-Varina, NC 27526',parent_conversation_id='conv')
    task = store.get(fourth['task_id'])
    assert task['executor_prefix'] == 'cal_edit'
    result = execute(monkeypatch,calendar_editor_executor,task['task_text'])['KYREX_RESULT_JSON']
    assert result['status'] == 'ok'
    assert connector.event['description'].startswith('Bring insurance card\n7608')
    assert [c[0] for c in connector.calls] == ['GET','PATCH']
    store.complete(fourth['task_id'], result)
    state = shared._calendar_conversation_state('alice','conv',store)
    assert state == {'event_id':connector.event['id']}
    assert shared._delegated_calendar_payload('alice',target,'30 minutes',state=state) is None
    assert shared._calendar_conversation_state('alice','other-conversation',store) == {}
    assert shared._calendar_conversation_state('bob','conv',store) == {}


def test_chat_create_duration_and_notes_address_followups(monkeypatch, tmp_path):
    store = Store()
    monkeypatch.setattr(connectors,'default_store',lambda:store)
    monkeypatch.setattr(chat_service,'_chat_root',lambda:tmp_path/'chat')
    bot = {'id':'calendar','owner':'alice','status':'running','policy':serve.calendar_preset_policy(),
           'rift':str(tmp_path),'model':'test:model'}
    monkeypatch.setattr(chat_service,'resolve_bot_for_user',lambda *a:bot)
    monkeypatch.setattr(chat_service.bot_provider,'resolve_bot_provider',lambda *a:{'provider':'openai','profile':'test','model':'mock','api_key':'offline','base_url':None,'headers':{}})
    conv = chat_service.create_conversation('alice')
    conv['bot_id'] = bot['id']
    chat_service._write('alice',conv)
    submitted = []
    async def submit(user, conv, bot, text, cid, cancel, **kwargs):
        submitted.append(kwargs)
        yield {'type':'status','status':'complete','content':'Prepared for approval.'}
    monkeypatch.setattr(chat_service,'_stream_writable_bot_task',submit)
    async def turn(text):
        return [f async for f in chat_service.stream_chat('alice',conv['conversation_id'],text)]
    first = asyncio.run(turn('Lets add Haper doctor appointment to the calendar for 3:45pm today'))
    assert 'How long' in first[-1]['content']
    assert submitted == []
    asyncio.run(turn('30 minutes'))
    assert submitted[-1]['calendar_intent']['title'] == 'Haper doctor appointment'
    chat_service._remember_calendar_event('alice',conv['conversation_id'],{'status':'ok','event_id':store.event['id']},'2026-10-08T14:00:00')
    question = asyncio.run(turn('can you add notes to it. like the address? I will provide it if needed'))
    assert 'What address' in question[-1]['content']
    asyncio.run(turn('here is the address 7608 Purfoy Rd, Fuquay-Varina, NC 27526'))
    intent = json.loads(submitted[-1]['calendar_delete_payload'])
    assert intent['op'] == 'update'
    assert intent['event_id'] == store.event['id']
    assert intent['notes'].startswith('7608 Purfoy Rd')
    assert store.calls == []  # Chat prepares; only the executor may write after approval


def test_missing_date_and_time_answers_retain_details():
    with pytest.raises(cal_writer.CalendarClarification) as first:
        cal_writer.parse_create_request('add Dentist at 3pm for 30 minutes')
    assert first.value.draft['missing'] == 'date'
    completed = cal_writer.parse_create_request('Oct. 8th',now=datetime(2026,10,8),pending=first.value.draft)
    assert completed['title'] == 'Dentist' and completed['start'] == '2026-10-08T15:00:00'
    with pytest.raises(cal_writer.CalendarClarification) as second:
        cal_writer.parse_create_request('add Dentist on Oct. 8th',now=datetime(2026,10,8))
    assert second.value.draft['missing'] == 'time'
    with pytest.raises(cal_writer.CalendarClarification) as third:
        cal_writer.parse_create_request('3pm',pending=second.value.draft)
    completed = cal_writer.parse_create_request('30 minutes',pending=third.value.draft)
    assert completed['title'] == 'Dentist' and completed['start'] == '2026-10-08T15:00:00'


def test_calendar_updates_do_not_steal_developer_file_edits():
    assert shared._calendar_update_request('update the notes in README.md') is False
    assert shared._calendar_update_request('set location of it to Raleigh') is True
    intent = cal_editor.normalize_update_request('set location of it to Raleigh',context={'event_id':'event12345'})
    assert intent['location'] == 'Raleigh' and intent['event_id'] == 'event12345'
    assert cal_editor.edit_reply('show my calendar', {'field':'notes'}) is False


def test_update_permission_is_exact_and_other_presets_stay_read_only():
    assert serve.calendar_update_granted({'cal:update':1})
    for policy in ({'cal:*':1}, {'*':1}, {'cal:update':0}, {'cal:update':'deny'}):
        assert not serve.calendar_update_granted(policy)
    assert 'cal:update' not in serve.LEVEL6_CALENDAR_PRESET
    assert 'cal:update' not in serve.CALENDAR_READER_PRESET
    assert 'cal:update' not in serve.CALENDAR_WRITER_PRESET


def test_calendar_preference_change_during_approval_cannot_redirect_patch(monkeypatch):
    store = Store()
    monkeypatch.setattr(connectors,'default_store',lambda:store)
    decisions = iter(['ALLOW','APPROVED'])
    def decide():
        result = next(decisions)
        if result == 'APPROVED':
            store.preferred_calendar = lambda *args:'another-calendar'
        return result
    monkeypatch.setattr(calendar_editor_executor,'_read_decision',decide)
    frames = execute(monkeypatch,calendar_editor_executor,{'op':'update','event_id':store.event['id'],'notes':'New note'})
    assert frames['KYREX_RESULT_JSON']['status'] == 'error'
    assert 'preferred calendar changed' in frames['KYREX_RESULT_JSON']['errors'][0]
    assert [call[0] for call in store.calls] == ['GET']
