"""Natural workout sends prepare exact stored drafts; only the Send card sends."""
import asyncio
import json
from types import SimpleNamespace

import pytest
import chat_service as chat
import device_messages as dm
import messages_send as ms
import serve
import workout_message_followup as followup
from task_store import CloudTaskStore


@pytest.mark.parametrize('text,recipient', [
    ('Send this to L6 Besties', 'L6 Besties'),
    ('Please send that to L6 Besties.', 'L6 Besties'),
    ('Can you send it to L6 Besties?', 'L6 Besties'),
    ('Could you text this message to L6 Besties', 'L6 Besties'),
    ('Send the workout preview to "L6 Besties"', 'L6 Besties'),
    ('Share this preview to Friends and family', 'Friends and family'),
    ('Forward the draft to Other group', 'Other group'),
    ('Send this', ''),
])
def test_natural_followup(text, recipient):
    assert followup.request(text) == recipient


@pytest.mark.parametrize('text', [
    'Add this to my calendar', 'Do not send this to L6 Besties',
    'Send this to L6 Besties and add it to my calendar',
    'Send this to L6 Besties: changed body', 'Text Ethan: Hello',
    'Send my emails to L6 Besties', 'Send the file to L6 Besties',
])
def test_other_intents_are_not_workout_followups(text):
    assert followup.request(text) is None


@pytest.fixture
def manual(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'workout-followup-tests')
    monkeypatch.setenv('KYREX_LEVEL6_PREVIEW_RECIPIENT', 'L6 Besties')
    phone = dm.MessagesStore()
    token = phone.redeem(phone.begin('alice')['pairing_code'])
    phone.sync(token, [
        {'id': 'm1', 'conversation_id': 'group1', 'conversation': 'L6 Besties',
         'sender': 'Friend', 'body': 'Hi', 'direction': 'incoming', 'kind': 'RCS'},
        {'id': 'm2', 'conversation_id': 'group2', 'conversation': 'Other group',
         'sender': 'Other', 'body': 'Hi', 'direction': 'incoming', 'kind': 'RCS'},
    ])
    queue = ms.SendQueue(phone)
    queue.poll(token, True)
    monkeypatch.setattr(chat.messages_send, 'SendQueue', lambda: queue)
    tasks = CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(chat, '_task_store', lambda: tasks)
    conv = chat.create_conversation('alice')
    conv['bot_id'] = 'calendar'
    chat._write('alice', conv)
    cid = conv['conversation_id']
    tid = tasks.submit('calendar', serve.LEVEL6_MESSAGE_PREVIEW_REQUEST,
        executor_prefix='level6', chat_id='alice', conversation_id=cid, resolve_bot=False)
    body = '#L6Workout\n\n🏋️ Level 6 — Workout Week\nMonday 2026-10-12 — LEG DAY\nTrainer: Coach'
    tasks.complete(tid, {'mode': 'level6_preview', 'status': 'no_changes', 'count': 6,
        'lines': [f'Day {i} — Workout — trainer: Coach' for i in range(6)],
        'message_text': body, 'final_response': 'Preview only — nothing sent.\n\n```text\n' + body + '\n```'})
    assert tasks.get(tid)['status'] == 'done'
    monkeypatch.setattr(chat, 'resolve_bot_for_user', lambda *a: {'id': 'calendar', 'owner': 'alice'})
    monkeypatch.setattr(chat, '_resolve_provider', lambda *a, **k: pytest.fail('Follow-up reached provider'))
    monkeypatch.setattr(chat.bot_provider, 'resolve_bot_provider', lambda *a, **k: pytest.fail('Follow-up reached Bot LLM'))
    monkeypatch.setattr(chat.dev_bot, 'submit_calendar_writer_task', lambda *a, **k: pytest.fail('Follow-up created calendar task'))
    return cid, tid, body, phone, queue, token, tasks


def turn(cid, text, request_id='first'):
    async def run():
        return [frame async for frame in chat.stream_chat('alice', cid, text, request_id=request_id)]
    return asyncio.run(run())


def test_manual_preview_gets_draft_and_old_transcript_is_repaired(manual):
    cid, tid, body, _, queue, token, _ = manual
    conv = chat.get_conversation('alice', cid)
    message = conv['messages'][0]
    assert message['message_draft'] == {'recipient': 'L6 Besties', 'text': body}
    stamp = message['created_at']
    del message['message_draft']
    chat._write('alice', conv)
    repaired = chat.get_conversation('alice', cid)['messages']
    assert len(repaired) == 1 and repaired[0]['created_at'] == stamp
    assert repaired[0]['message_draft']['text'] == body
    assert queue.poll(token, True)['command'] is None
    assert chat.get_conversation('bob', cid) is None


def test_exact_screenshot_followup_prepares_without_calendar_or_send(manual):
    cid, tid, body, _, queue, token, _ = manual
    frames = turn(cid, 'Send this to L6 Besties')
    job_id = next(f['send_id'] for f in frames if f['type'] == 'message_send')
    assert 'Nothing has been sent yet' in frames[-1]['content']
    command = queue.poll(token, True)['command']
    assert command['action'] == 'prepare' and command['text'] == body
    assert command['conversation_id'] == 'group1'
    queue.acknowledge(token, job_id, 'prepare', {'conversation_id': 'group1',
        'token': 'a' * 32, 'text': body, 'recipients': ['Friend A', 'Friend B'],
        'name': 'L6 Besties', 'kind': 'RCS'})
    assert queue.poll(token, True)['command'] is None
    repeated = turn(cid, 'Please send this to L6 Besties', 'second')
    assert next(f['send_id'] for f in repeated if f['type'] == 'message_send') == job_id
    assert queue.poll(token, True)['command'] is None
    queue.decide('alice', job_id, 'send')
    assert queue.poll(token, True)['command']['action'] == 'send'
    queue.acknowledge(token, job_id, 'send', {'accepted': True})
    repeated = turn(cid, 'Send this to L6 Besties', 'third')
    assert next(f['send_id'] for f in repeated if f['type'] == 'message_send') == job_id
    assert queue.poll(token, True)['command'] is None, 'accepted sends cannot replay'
    assert 'already been submitted' in repeated[-1]['content']
    saved = chat.get_conversation('alice', cid)['messages']
    assert saved[0]['message_send']['id'] == job_id
    assert saved[-1]['workout_preview_message_id'] == f'task-{tid}-result'


def test_retry_after_the_exact_wrong_calendar_clarification(manual):
    cid, _, body, _, queue, token, _ = manual
    conv = chat.get_conversation('alice', cid)
    chat._append_message('alice', conv, 'assistant', followup._LEGACY_CLARIFICATION)
    chat._write('alice', conv)
    assert any(f['type'] == 'message_send' for f in turn(cid, 'Send this to L6 Besties'))
    assert queue.poll(token, True)['command']['text'] == body


def test_newer_answer_or_prose_only_cannot_supply_the_draft(manual):
    cid, _, _, _, queue, token, _ = manual
    conv = chat.get_conversation('alice', cid)
    chat._append_message('alice', conv, 'assistant', '#L6Workout\nModel-generated replacement')
    chat._write('alice', conv)
    frames = turn(cid, 'Send this to L6 Besties')
    assert not any(f['type'] == 'message_send' for f in frames)
    assert 'current saved workout preview' in frames[-1]['content']
    assert queue.poll(token, True)['command'] is None


def test_explicit_recipient_is_resolved_and_never_reuses_another_target(manual):
    cid, _, body, _, queue, token, _ = manual
    frames = turn(cid, 'Send this to Other group')
    job_id = next(f['send_id'] for f in frames if f['type'] == 'message_send')
    command = queue.poll(token, True)['command']
    assert command['conversation_id'] == 'group2' and command['text'] == body
    again = turn(cid, 'Send this', 'same-target')
    assert next(f['send_id'] for f in again if f['type'] == 'message_send') == job_id
    source = chat.get_conversation('alice', cid)['messages'][0]
    with pytest.raises(dm.MessagesError, match='Finish or cancel'):
        chat.prepare_preview_message('alice', cid, source['id'])
    assert queue.get('alice', job_id)['conversation_id'] == 'group2'
    queue.decide('alice', job_id, 'cancel')
    assert queue.poll(token, True)['command'] is None


def test_missing_group_can_be_corrected_without_losing_preview_context(manual):
    cid, _, body, _, queue, token, _ = manual
    missing = turn(cid, 'Send this to Missing group')
    assert 'phone snapshot' in missing[-1]['content']
    assert queue.poll(token, True)['command'] is None
    fixed = turn(cid, 'Send this to L6 Besties', 'fixed-target')
    assert any(f['type'] == 'message_send' for f in fixed)
    assert queue.poll(token, True)['command']['text'] == body


def test_cancelled_or_foreign_bot_turn_cannot_prepare(manual, monkeypatch):
    cid, _, _, _, queue, token, _ = manual
    async def run():
        cancelled = asyncio.Event(); cancelled.set()
        return [f async for f in chat.stream_chat('alice', cid,
            'Send this to L6 Besties', cancel_event=cancelled)]
    assert asyncio.run(run())[-1]['status'] == 'cancelled'
    assert queue.poll(token, True)['command'] is None
    monkeypatch.setattr(chat, 'resolve_bot_for_user', lambda *a: {'id': 'calendar', 'owner': 'bob'})
    with pytest.raises(chat.ChatUnavailable, match='own Bot'):
        turn(cid, 'Send this to L6 Besties')
    assert queue.poll(token, True)['command'] is None


@pytest.mark.parametrize('change', [
    {'chat_id': 'bob'}, {'conversation_id': 'other'}, {'executor_prefix': 'repo'},
    {'task_text': serve.LEVEL6_MESSAGE_REQUEST}, {'status': 'failed'},
])
def test_only_owned_successful_preview_tasks_supply_drafts(manual, change):
    cid, tid, _, _, queue, token, tasks = manual
    task = {**tasks.get(tid), **change}
    assert chat._workout_preview_draft('alice', chat.get_conversation('alice', cid), task, task['result']) is None
    assert queue.poll(token, True)['command'] is None


def test_live_preview_metadata_survives_public_sse(manual, monkeypatch):
    import chat_api, flux
    cid, tid, body, _, queue, token, _ = manual
    monkeypatch.setattr(chat.dev_bot, 'submit_level6_message_task', lambda *a, **k: tid)
    monkeypatch.setattr(flux, 'stream_events', lambda *a, **k: iter([]))
    async def run():
        source = chat._stream_writable_bot_task('alice', chat.get_conversation('alice', cid),
            {}, '#L6Workout preview', cid, asyncio.Event(), mode='level6_message')
        return [frame async for frame in chat_api._drive_stream(source, 'live', cid)]
    frames = asyncio.run(run())
    done = json.loads(frames[-1].split('data: ', 1)[1])
    assert done['message_draft'] == {'recipient': 'L6 Besties', 'text': body}
    assert done['message_id'] == f'task-{tid}-result'
    assert queue.poll(token, True)['command'] is None
