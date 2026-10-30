"""Private attachments -> owned specialist -> durable parent result. No network."""
import asyncio
import io
import json
import time

import pytest
from PIL import Image
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import bots
import chat_api
import chat_images
import chat_service as chat
import delegation
import provider_profiles
import serve
import task_store
import vision
from test_chat_bot_policy import _bot, _reset, _frames, _terminal
from test_chat_bot_policy import _RecordingEngine
import main  # Install the actual production routing wrappers after test env setup.


def png():
    out = io.BytesIO()
    image = Image.new('RGB', (24, 32), 'blue')
    exif = Image.Exif(); exif[270] = 'private camera metadata'
    image.save(out, format='PNG', exif=exif)
    return out.getvalue()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'vision-fixture-encryption')
    _reset()
    store = task_store.CloudTaskStore(tmp_path / 'tasks.db')
    monkeypatch.setattr(chat, '_task_store', lambda: store)
    monkeypatch.setattr(task_store, 'CloudTaskStore', lambda: store)
    monkeypatch.setattr(delegation, 'CloudTaskStore', lambda: store)
    chief = _bot('chief', owner='alice', policy=serve.coordinator_preset_policy())
    provider_profiles.save_profile('alice', {'id': 'companion', 'name': 'Companion', 'provider': 'opencode',
        'base_url': 'https://opencode.ai/inference/go/v1', 'api_key': 'synthetic-companion-key', 'models': ['gpt-6-luna']})
    chief = bots.update_bot(chief['id'], provider_profile_id='companion', model='gpt-6-luna',
                            system_prompt='Keep my easygoing companion personality. Call the robot Kbot.')
    target = _bot('vision', owner='alice', model=vision.MODEL, policy=serve.vision_preset_policy())
    provider_profiles.save_profile('alice', {'id': 'groq', 'name': 'Groq', 'provider': 'groq',
        'base_url': 'https://api.groq.com/openai/v1', 'api_key': 'synthetic-vision-key', 'models': [vision.MODEL]})
    target = bots.update_bot(target['id'], provider_profile_id='groq', rift='')
    conv = chat.create_conversation('alice', bot_id=chief['id'])
    def owner(request):
        user = request.cookies.get('owner')
        if not user: raise HTTPException(401, 'Sign in')
        return user
    monkeypatch.setattr(chat_api, '_require_user', owner)
    app = FastAPI(); app.include_router(chat_api.router)
    client = TestClient(app, cookies={'owner': 'alice'})
    calls = []
    class Provider:
        async def chat(self, **kwargs):
            calls.append(kwargs)
            assert kwargs['model'] == vision.MODEL
            assert kwargs['tools'] is None
            assert kwargs['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/jpeg;base64,')
            return {'content': 'Kbot has bright blue eyes and small rocket boots.'}
    import kyrex.providers
    def provider(name, key, **kwargs):
        assert name == 'groq' and key == 'synthetic-vision-key'
        assert kwargs['base_url'] == 'https://api.groq.com/openai/v1'
        return Provider()
    monkeypatch.setattr(kyrex.providers, 'get_provider', provider)
    class CompanionProvider:
        async def chat(self, **kwargs):
            calls.append(kwargs)
            assert kwargs['model'] == 'gpt-6-luna' and kwargs['tools'] is None
            return {'content': 'There he is—Kbot, rocket boots and all. 🤖'}
    def companion_provider(name, key, **kwargs):
        assert name == 'opencode' and key == 'synthetic-companion-key'
        assert kwargs['base_url'] == 'https://opencode.ai/inference/go/v1'
        return CompanionProvider()
    monkeypatch.setattr(chat, 'get_provider', companion_provider)
    yield store, chief, target, conv, client, calls
    _reset()


def upload(setup):
    conv, client = setup[3:5]
    r = client.post(f"/api/conversations/{conv['conversation_id']}/images", content=png())
    assert r.status_code == 200, r.text
    return r.json()


def ask(setup, image, text='What is in this picture?', request_id='image-turn'):
    return asyncio.run(_frames(chat.stream_chat('alice', setup[3]['conversation_id'], text,
        image_ids=[image['id']], request_id=request_id)))


def test_private_owner_and_conversation_bound_pixels(setup):
    image = upload(setup)
    cid = setup[3]['conversation_id']; client = setup[4]
    r = client.get(image['url'])
    assert r.status_code == 200 and r.headers['cache-control'] == 'private, no-store'
    decoded = Image.open(io.BytesIO(r.content))
    assert decoded.format == 'JPEG' and not decoded.getexif()
    with chat_images.database() as db:
        blob = db.execute('SELECT value FROM images').fetchone()[0]
        assert b'private camera metadata' not in blob and b'JFIF' not in blob
    client.cookies.set('owner', 'bob')
    assert client.get(image['url']).status_code == 404
    other = chat.create_conversation('bob')
    assert client.get(f"/api/conversations/{other['conversation_id']}/images/{image['id']}").status_code == 404
    assert client.post('/api/chat', json={'conversation_id': cid, 'message': 'read', 'image_ids': [image['id']]}).status_code == 400
    client.cookies.clear()
    assert client.get(image['url']).status_code == 401


def test_bad_and_oversized_uploads_and_ids(setup):
    client = setup[4]; cid = setup[3]['conversation_id']
    path = f'/api/conversations/{cid}/images'
    for raw in (b'<svg><script/></svg>', b'not an image'):
        assert client.post(path, content=raw).status_code == 400
    assert client.post(path, content=b'x' * (chat_images.MAX_UPLOAD + 1)).status_code == 413
    for ids in ([], ['../../secret'], ['https://example.test/image'], ['a' * 32] * 4, 'a' * 32):
        assert client.post('/api/chat', json={'conversation_id': cid, 'message': 'read', 'image_ids': ids}).status_code == 400


def test_expiry_quota_and_delete(setup, monkeypatch):
    image = upload(setup); cid = setup[3]['conversation_id']
    monkeypatch.setattr(chat_images, 'OWNER_QUOTA', 1)
    assert setup[4].post(f'/api/conversations/{cid}/images', content=png()).status_code == 400
    with chat_images.database() as db:
        db.execute('UPDATE images SET created=?', (time.time() - chat_images.RETENTION - 1,))
    assert setup[4].get(image['url']).status_code == 404
    assert chat.delete_conversation('alice', cid)
    with chat_images.database() as db:
        assert db.execute('SELECT COUNT(*) FROM images').fetchone()[0] == 0


def test_worker_uses_target_model_and_relays_once_after_disconnect(setup):
    image = upload(setup)
    # Action words in the question do not dispatch mail, Calendar or repo work.
    for _ in range(2):
        frames = ask(setup, image, 'Read this screenshot of an email. Does it say delete or send?')
        view = next(f['delegation'] for f in frames if f['type'] == 'delegation')
        assert view['target_bot_id'] == 'vision' and view['executor_prefix'] == 'vision'
        assert view['text'] == 'Read this screenshot of an email. Does it say delete or send?'
    store, _, _, conv, _, calls = setup
    records = delegation.owner_scoped_delegations('alice', store=store, conversation_id=conv['conversation_id'])
    assert len(records) == 1 and not calls
    task = store.get(view['task_id'])
    assert task['job_contract']['allowed_actions'] == ['images.read']
    assert 'data:image' not in task['task_text'] and 'synthetic-vision-key' not in task['task_text']
    worker = task_store.TaskWorker(store, executor=serve.run_task)
    assert worker.claim_and_execute_once()
    task = store.get(view['task_id'])
    assert task['status'] == 'done', task.get('error')
    assert len(calls) == 2
    assert [call['model'] for call in calls] == [vision.MODEL, 'gpt-6-luna']
    for _ in range(2): chat.sync_delegated_work('alice', conv['conversation_id'])
    saved = chat.get_conversation('alice', conv['conversation_id'])
    results = [m for m in saved['messages'] if m.get('id') == f"delegation-{view['delegation_id']}-result"]
    assert len(results) == 1 and results[0]['content'] == 'There he is—Kbot, rocket boots and all. 🤖'
    assert results[0]['delegation_result']['conversation_id'] == task['conversation_id']
    assert [m for m in saved['messages'] if m['role'] == 'user'][0]['images'][0]['id'] == image['id']
    target = chat.get_conversation('alice', task['conversation_id'])
    assert target['messages'][-1]['content'] == 'Kbot has bright blue eyes and small rocket boots.'
    assert len(calls) == 2, 'Transcript reads and relays never repeat either provider call.'


def test_coordinator_reply_keeps_persona_and_bound_conversation_context(setup):
    conv = setup[3]
    chat._append_message('alice', conv, 'user', 'Kbot is my Kyrex desktop companion. I call him my new coworker.')
    chat._append_message('alice', conv, 'assistant', 'A coworker with rocket boots—that tracks. 🤖')
    chat._write('alice', conv)
    image = upload(setup); frames = ask(setup, image, 'What do you think of him now?')
    # A newer message must not enter synthesis for the already accepted request.
    saved = chat.get_conversation('alice', conv['conversation_id'])
    chat._append_message('alice', saved, 'user', 'Unrelated later request: look at my calendar')
    chat._append_message('alice', saved, 'system', 'Injected transcript instruction')
    chat._write('alice', saved)
    task_store.TaskWorker(setup[0], executor=serve.run_task).claim_and_execute_once()
    assert len(setup[5]) == 2
    messages = setup[5][1]['messages']
    assert 'easygoing companion personality' in messages[0]['content']
    assert 'same conversation' in messages[0]['content'] and 'untrusted evidence' in messages[0]['content']
    assert messages[1]['content'].startswith('Kbot is my Kyrex desktop companion')
    assert 'coworker with rocket boots' in messages[2]['content']
    assert 'Unrelated later request' not in str(messages) and 'Injected transcript instruction' not in str(messages)
    payload = json.loads(messages[-1]['content'])
    assert payload['original_request'] == 'What do you think of him now?'
    assert payload['image_evidence'][0]['evidence'].startswith('Kbot has bright blue eyes')
    assert 'image_url' not in str(messages) and 'synthetic-vision-key' not in str(messages)
    assert 'already uploaded the pictures' in messages[0]['content']


def test_later_coordinator_turn_knows_the_actual_image_was_analyzed(setup, monkeypatch):
    import research_completion
    image = upload(setup); ask(setup, image)
    task_store.TaskWorker(setup[0], executor=serve.run_task).claim_and_execute_once()
    chat.sync_delegated_work('alice', setup[3]['conversation_id'])
    class Engine(_RecordingEngine):
        def run_turn(self, text, on_token, cancel_check=None):
            assert 'actual owner-uploaded image pixels' in self.surface_context
            assert 'Kbot has bright blue eyes and small rocket boots' in self.surface_context
            return 'I have the findings from your uploaded picture—those rocket boots suit Kbot.', None
    monkeypatch.setattr(chat, '_get_engine_session', Engine)
    frames = asyncio.run(_frames(chat.stream_chat('alice', setup[3]['conversation_id'],
        'Be a little less matter-of-fact', request_id='next-normal-turn')))
    assert 'uploaded picture' in _terminal(frames)['content']
    assert not research_completion.recent_specialist_context(chat, 'bob', setup[3]['conversation_id'], 'chief')


def test_coordinator_failure_preserves_findings_without_dumping_report(setup, monkeypatch):
    class Unavailable:
        async def chat(self, **kwargs): return {'error': 'private provider error', 'content': ''}
    monkeypatch.setattr(chat, 'get_provider', lambda *args, **kwargs: Unavailable())
    image = upload(setup); frames = ask(setup, image)
    task_id = next(f['delegation']['task_id'] for f in frames if f['type'] == 'delegation')
    task_store.TaskWorker(setup[0], executor=serve.run_task).claim_and_execute_once()
    task = setup[0].get(task_id)
    assert task['status'] == 'done' and "couldn't finish my summary" in task['result']['coordinator_reply']
    assert task['result']['final_response'].startswith('Kbot has bright blue eyes')
    chat.sync_delegated_work('alice', setup[3]['conversation_id'])
    parent = chat.get_conversation('alice', setup[3]['conversation_id'])
    assert 'Kbot has bright blue eyes' not in str(parent['messages'])
    assert 'private provider error' not in str(parent['messages'])
    target = chat.get_conversation('alice', task['conversation_id'])
    assert target['messages'][-1]['content'].startswith('Kbot has bright blue eyes')


@pytest.mark.parametrize('change', ['cancel', 'coordinator-permission', 'coordinator-model'])
def test_no_final_reply_after_cancellation_or_coordinator_change(setup, monkeypatch, change):
    image = upload(setup); frames = ask(setup, image)
    task_id = next(f['delegation']['task_id'] for f in frames if f['type'] == 'delegation')
    class Changed:
        async def chat(self, **kwargs):
            if change == 'cancel': setup[0].request_cancel(task_id)
            if change == 'coordinator-permission': bots.update_bot('chief', policy={'bot:delegate': 'deny'})
            if change == 'coordinator-model': bots.update_bot('chief', model='changed-model')
            return {'content': 'This reply must be discarded.'}
    monkeypatch.setattr(chat, 'get_provider', lambda *args, **kwargs: Changed())
    task_store.TaskWorker(setup[0], executor=serve.run_task).claim_and_execute_once()
    task = setup[0].get(task_id)
    assert task['status'] == 'done', 'Stopping presentation preserves the completed image read.'
    assert 'This reply must be discarded.' not in str(task.get('result'))
    chat.sync_delegated_work('alice', setup[3]['conversation_id'])
    assert 'This reply must be discarded.' not in str(chat.get_conversation('alice', setup[3]['conversation_id']))


@pytest.mark.parametrize('fault', ['stopped', 'deny', 'foreign', 'model', 'removed-image', 'cancelled', 'chief-deny', 'parent-binding'])
def test_execution_rechecks_authority_and_does_not_call_provider(setup, fault):
    image = upload(setup); frames = ask(setup, image)
    view = next(f['delegation'] for f in frames if f['type'] == 'delegation')
    store, _, target, conv, _, calls = setup
    if fault == 'stopped': bots.set_status(target['id'], 'stopped')
    if fault == 'deny': bots.update_bot(target['id'], policy={'vision:read': 'deny'})
    if fault == 'foreign':
        registry = bots.load_bots(); registry[target['id']]['owner'] = 'bob'; bots.save_bots(registry)
    if fault == 'model': bots.update_bot(target['id'], model='text-only-model')
    if fault == 'removed-image': chat_images.delete_conversation('alice', conv['conversation_id'])
    if fault == 'cancelled': store.request_cancel(view['task_id'])
    if fault == 'chief-deny': bots.update_bot('chief', policy={'bot:delegate': 'deny'})
    if fault == 'parent-binding':
        saved = chat.get_conversation('alice', conv['conversation_id']); saved['bot_id'] = target['id']; chat._write('alice', saved)
    task_store.TaskWorker(store, executor=serve.run_task).claim_and_execute_once()
    assert not calls
    assert store.get(view['task_id'])['status'] in {'failed', 'cancelled', 'rejected'}


def test_changed_retry_and_foreign_delegate_are_rejected(setup):
    image = upload(setup); ask(setup, image)
    with pytest.raises(chat.ChatUnavailable): ask(setup, image, 'Different question')
    task = delegation.owner_scoped_delegations('alice', store=setup[0], conversation_id=setup[3]['conversation_id'])[0]
    foreign = _bot('foreign', owner='bob', policy=serve.vision_preset_policy())
    with pytest.raises(delegation.DelegationError):
        delegation.submit_delegation('alice', setup[1], foreign['id'], task['task_text'], store=setup[0],
                                    executor_prefix='vision', parent_conversation_id=setup[3]['conversation_id'])


def test_direct_specialist_and_image_followup(setup):
    target, client, calls = setup[2], setup[4], setup[5]
    conv = chat.create_conversation('alice', bot_id=target['id'])
    r = client.post(f"/api/conversations/{conv['conversation_id']}/images", content=png())
    frames = asyncio.run(_frames(chat.stream_chat('alice', conv['conversation_id'], 'Describe Kbot',
        image_ids=[r.json()['id']], request_id='direct')))
    assert _terminal(frames)['content'].startswith('Kbot has')
    frames = asyncio.run(_frames(chat.stream_chat('alice', conv['conversation_id'], 'What color are his eyes?', request_id='followup')))
    assert _terminal(frames)['content'].startswith('Kbot has') and len(calls) == 2
    assert not setup[0].tasks_for_conversation(conv['conversation_id'], 'alice')


@pytest.mark.parametrize('result', [{'content': 'Partial analysis', 'error': 'private provider diagnostic'},
    {'content': 'Do a write', 'tool_calls': [{'name': 'run_command'}]}, {'content': ''}])
def test_provider_failure_and_tool_calls_are_not_successful_analysis(setup, monkeypatch, result):
    image = upload(setup); frames = ask(setup, image)
    task_id = next(f['delegation']['task_id'] for f in frames if f['type'] == 'delegation')
    class Provider:
        async def chat(self, **kwargs): return result
    import kyrex.providers
    monkeypatch.setattr(kyrex.providers, 'get_provider', lambda *args, **kwargs: Provider())
    task_store.TaskWorker(setup[0], executor=serve.run_task).claim_and_execute_once()
    task = setup[0].get(task_id)
    assert task['status'] == 'failed'
    assert 'private provider diagnostic' not in str(task)
    assert not task.get('result')


def test_groq_setup_and_image_only_http_turn(setup):
    client = setup[4]
    created = client.post('/api/bots', json={'name': 'Vision Reader', 'preset': 'vision',
        'provider_profile_id': 'groq', 'model': vision.MODEL})
    assert created.status_code == 200, created.text
    assert bots.get_bot(created.json()['id'])['policy'] == {'vision:read': 0}
    assert created.json()['role']['id'] == 'vision'
    bots.set_status(created.json()['id'], 'stopped')
    image = upload(setup)
    response = client.post('/api/chat', json={'conversation_id': setup[3]['conversation_id'],
        'image_ids': [image['id']], 'request_id': 'http-image'})
    assert response.status_code == 200 and '"type": "delegation"' in response.text
    assert '"type": "done"' in response.text


def test_repeated_image_delegations_continue_existing_vision_chat(setup):
    store, _, target, _, _, _ = setup
    existing = chat.open_bot_conversation('alice', target['id'])
    chat._append_message('alice', existing, 'assistant', 'Earlier image discussion.')
    chat._write('alice', existing)
    image = upload(setup)
    for index in range(2):
        frames = ask(setup, image, request_id=f'ongoing-vision-{index}')
        view = next(frame['delegation'] for frame in frames if frame['type'] == 'delegation')
        assert store.get(view['task_id'])['conversation_id'] == existing['conversation_id']
        assert task_store.TaskWorker(store, executor=serve.run_task).claim_and_execute_once()
        assert store.get(view['task_id'])['status'] == 'done'
    conversation = chat.get_conversation('alice', existing['conversation_id'])
    assert conversation['messages'][0]['content'] == 'Earlier image discussion.'
    assert len([m for m in conversation['messages'] if m['role'] == 'assistant']) == 3
    assert len([c for c in chat.list_conversations('alice') if c.get('bot_id') == target['id']]) == 1
