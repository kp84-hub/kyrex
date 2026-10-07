"""A durable weekly preview stays read-only until its owner confirms Send."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import chat_service as chat
import device_messages as dm
import messages_send as ms
import level6_message_schedule as schedule
import serve


@pytest.fixture
def preview(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'preview-test-secret')
    store = dm.MessagesStore()
    token = store.redeem(store.begin('alice')['pairing_code'])
    store.sync(token, [{'id': 'm1', 'conversation_id': 'group1', 'conversation': 'L6 Besties',
                        'sender': 'Friend', 'body': 'Hi', 'direction': 'incoming', 'kind': 'RCS'}])
    queue = ms.SendQueue(store)
    queue.poll(token, True)
    monkeypatch.setattr(chat.messages_send, 'SendQueue', lambda: queue)
    cid = chat.ensure_level6_preview_conversation('alice', 'L6 Besties')
    tid = schedule.preview_task_id('alice', '2026-10-12')
    body = '#L6Workout\n\nVerified upcoming week'
    task = {'task_id': tid, 'status': 'done', 'result': {
        'mode': 'level6_preview', 'status': 'no_changes', 'count': 6,
        'final_response': f'Preview only — nothing sent.\n\n```text\n{body}\n```',
        'message_text': body}}
    tasks = SimpleNamespace(tasks_for_conversation=lambda *a: [task], get=lambda *a: task)
    monkeypatch.setattr(chat, '_task_store', lambda: tasks)
    return cid, f'task-{tid}-result', body, store, queue, token, task


def test_saved_preview_is_visible_idempotent_and_not_a_phone_command(preview):
    cid, mid, body, store, queue, token, _ = preview
    conv = chat.get_conversation('alice', cid)
    assert conv['messages'][0]['id'] == mid
    assert conv['messages'][0]['message_draft'] == {'recipient': 'L6 Besties', 'text': body}
    assert len(chat.get_conversation('alice', cid)['messages']) == 1
    assert queue.poll(token, True)['command'] is None
    assert chat.ensure_level6_preview_conversation('alice', 'L6 Besties') == cid
    assert chat.get_conversation('bob', cid) is None
    assert chat.ensure_level6_preview_conversation('bob', 'L6 Besties') != cid


def test_prepare_reads_exact_saved_body_and_waits_for_send_confirmation(preview):
    cid, mid, body, store, queue, token, _ = preview
    with pytest.raises(dm.MessagesError):
        chat.prepare_preview_message('bob', cid, mid)
    job = chat.prepare_preview_message('alice', cid, mid)
    assert chat.prepare_preview_message('alice', cid, mid)['id'] == job['id']
    command = queue.poll(token, True)['command']
    assert command['action'] == 'prepare' and command['text'] == body
    queue.acknowledge(token, job['id'], 'prepare', {
        'conversation_id': 'group1', 'token': 'a' * 32, 'text': body,
        'recipients': ['Friend A · +15555550111', 'Friend B · +15555550222'],
        'name': 'L6 Besties', 'kind': 'RCS'})
    assert queue.poll(token, True)['command'] is None
    assert chat.get_conversation('alice', cid)['messages'][0]['message_send']['id'] == job['id']
    queue.decide('alice', job['id'], 'send')
    assert queue.poll(token, True)['command']['action'] == 'send'
    queue.acknowledge(token, job['id'], 'send', {'accepted': True})
    assert chat.prepare_preview_message('alice', cid, mid)['id'] == job['id']
    assert queue.poll(token, True)['command'] is None, 'an accepted send is never automatically replayed'


def test_expired_preview_can_be_prepared_again_without_regenerating_workout(preview):
    cid, mid, body, store, queue, token, _ = preview
    first = chat.prepare_preview_message('alice', cid, mid)
    with store._db() as db:
        db.execute('UPDATE message_sends SET expires=0 WHERE id=?', (first['id'],))
    second = chat.prepare_preview_message('alice', cid, mid)
    assert second['id'] != first['id'] and second['text'] == body
    assert queue.poll(token, True)['command']['action'] == 'prepare'


def test_failed_job_has_no_sendable_draft(preview):
    cid, mid, body, store, queue, token, task = preview
    task['status'] = 'failed'
    task['result'] = {'errors': ['Upcoming week not available yet']}
    message = chat.get_conversation('alice', cid)['messages'][0]
    assert 'message_draft' not in message
    with pytest.raises(dm.MessagesError):
        chat.prepare_preview_message('alice', cid, mid)
    assert queue.poll(token, True)['command'] is None


@pytest.mark.parametrize('upcoming', [True, False])
def test_sunday_executor_requires_upcoming_six_dates_and_never_sends(monkeypatch, upcoming):
    import bots
    import browser_hosts
    import level6_weekly as weekly
    import glofox_api
    calendar = {'id': 'calendar', 'owner': 'alice', 'status': 'running', 'policy': serve.CALENDAR_PRESET}
    browser = {'id': weekly.BROWSER_BOT_ID, 'owner': 'alice', 'status': 'running', 'policy': serve.BROWSER_PRESET}
    monkeypatch.setattr(bots, 'get_bot', {calendar['id']: calendar, browser['id']: browser}.get)
    monkeypatch.setattr(browser_hosts, 'binding_for', lambda *a: 'host')
    monkeypatch.setattr(serve, 'build_context', lambda *a: SimpleNamespace(bot_owner='alice', policy=browser['policy']))
    start = date(2026, 10, 12) if upcoming else date(2026, 10, 5)
    lines = [f'Day {(start + timedelta(days=i)).isoformat()} — Workout — trainer: Coach' for i in range(6)]
    monkeypatch.setattr(weekly, 'run_weekly', lambda **k: lines)
    monkeypatch.setattr(glofox_api, '_week_0830_classes_for_dates', lambda *a: [])
    monkeypatch.setattr(serve, 'browser_host_dispatch', lambda *a, **k: pytest.fail('Scheduled preview tried to send'))
    monkeypatch.setenv('KYREX_LEVEL6_SEND_ENABLED', '1')
    results, replies = [], []
    ctx = SimpleNamespace(bot_owner='alice', bot_id='calendar', policy=calendar['policy'])
    serve._run_level6_facebook_message_task(ctx, 'alice', serve.LEVEL6_MESSAGE_PREVIEW_REQUEST,
        lambda cid, text: replies.append(text), on_result=results.append,
        task_id=schedule.preview_task_id('alice', '2026-10-12'))
    if upcoming:
        assert results[0]['mode'] == 'level6_preview'
        assert results[0]['message_text'].startswith('#L6Workout')
        assert '2026-10-12' in results[0]['message_text']
    else:
        assert results[0]['status'] == 'error' and results[0]['count'] == 0
        assert 'message_text' not in results[0]
        assert 'upcoming workout week' in replies[0]


def test_real_task_store_restart_preserves_one_preview_and_chat_delivery(tmp_path, monkeypatch):
    from task_store import CloudTaskStore
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('KYREX_LEVEL6_PREVIEW_RECIPIENT', 'L6 Besties')
    monkeypatch.setattr(schedule, 'select_bot', lambda owner: {'id': 'calendar', 'rift': '/tmp/calendar'})
    now = datetime(2026, 10, 11, 19, 1, tzinfo=ZoneInfo('America/New_York'))
    store = CloudTaskStore(tmp_path / 'tasks.db')
    assert schedule.submit_due(store, now=now, owner='alice') == 'queued'
    restarted = CloudTaskStore(tmp_path / 'tasks.db')
    assert schedule.submit_due(restarted, now=now, owner='alice') == 'already queued'
    tid = schedule.preview_task_id('alice', '2026-10-12')
    task = restarted.get(tid)
    assert task['task_text'] == serve.LEVEL6_MESSAGE_PREVIEW_REQUEST
    assert task['job_contract']['mode'] == 'level6_preview'
    body = '#L6Workout\nVerified week'
    result = {'mode': 'level6_preview', 'status': 'no_changes', 'count': 6,
              'lines': ['Verified workout'] * 6, 'message_text': body,
              'final_response': 'Preview only — nothing sent.\n' + body}
    assert restarted.complete(tid, result, publish_result=True) == 'done'
    monkeypatch.setattr(chat, '_task_store', lambda: restarted)
    conv = chat.get_conversation('alice', task['conversation_id'])
    assert conv['messages'][0]['message_draft']['text'] == body
    assert 'message_send' not in conv['messages'][0]


def test_prepare_endpoint_requires_owner_session_and_ignores_client_text(preview, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    import chat_api
    def owner(request):
        user = request.headers.get('x-owner')
        if not user:
            raise HTTPException(401)
        return user
    monkeypatch.setattr(chat_api, '_require_user', owner)
    app = FastAPI(); app.include_router(chat_api.router)
    client = TestClient(app)
    cid, mid, body, *_ = preview
    url = f'/api/conversations/{cid}/messages/{mid}/prepare-send'
    assert client.post(url).status_code == 401
    assert client.post(url, headers={'x-owner': 'bob'}).status_code == 400
    result = client.post(url, headers={'x-owner': 'alice'}, json={'text': 'Injected text', 'recipient': 'Other group'})
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    assert result.json()['text'] == body and result.json()['conversation_id'] == 'group1'
