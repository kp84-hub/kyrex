"""Reviewed email delivery: real store, fake TLS/IMAP, zero live messages."""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import agent_inbox
import connectors
import connections_api
import email_chat
import email_send as mail
import email_send_api as api
import chat_service as chat
import bots
import serve
import provider_profiles
from test_zoho_inbox import Fake, EMAIL, PASSWORD, HEADER


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'reviewed-email-test-only')
    monkeypatch.setenv('KYREX_PROVIDER', 'openai')
    monkeypatch.setenv('KYREX_API_KEY', 'llm-test-only')
    monkeypatch.setenv('KYREX_MODEL', 'test-model')
    monkeypatch.setattr(chat, '_data_dir', lambda: tmp_path)
    monkeypatch.setattr(chat.chat_memory, 'context', lambda owner: '')
    monkeypatch.setattr(bots, 'BOTS_FILE', str(tmp_path / 'bots.json'))
    inbox = agent_inbox.AgentInboxStore(tmp_path / 'inbox.db')
    fake = Fake()
    inbox.zoho_factory = fake.factory
    inbox.connect_zoho('alice', EMAIL, PASSWORD, 'imappro.zoho.com')
    fake.headers[20] = b'From: Office <office@example.com>\r\nReply-To: bookings@example.com\r\nSubject: Appointment\r\nMessage-ID: <parent@office.example>\r\nReferences: <older@office.example>\r\n\r\n'
    fake.calls.clear()
    monkeypatch.setattr(agent_inbox, 'default_store', lambda: inbox)
    calls = []
    behavior = {'failure': None, 'login': None, 'data': None}

    class SMTP:
        def __init__(self, host, **kw):
            assert host == 'smtppro.zoho.com' and kw['port'] == 465
            assert kw['context'].check_hostname and kw['context'].verify_mode == 2
            calls.append(('open', host))
        def login(self, sender, password):
            assert sender == EMAIL and password == PASSWORD
            calls.append(('login', sender))
            if behavior['login']: behavior['login']()
            if behavior['failure'] == 'login': raise RuntimeError(PASSWORD)
        def mail(self, sender):
            calls.append(('from', sender)); return 250, b'ok'
        def rcpt(self, to):
            calls.append(('to', to)); return 250, b'ok'
        def data(self, body):
            calls.append(('data', body))
            if behavior['data']: behavior['data']()
            if behavior['failure'] == 'data': raise RuntimeError(PASSWORD)
            return 250, b'queued'
        def quit(self): calls.append(('quit',))

    drafts = mail.DraftStore(tmp_path / 'drafts.db', inbox,
        lambda store, owner, payload: mail.smtp_send(store, owner, payload, SMTP))
    monkeypatch.setattr(mail, 'DraftStore', lambda: drafts)
    monkeypatch.setattr(connections_api, '_require_user', lambda r: r.headers.get('x-test-owner', 'alice'))
    app = FastAPI()
    app.include_router(api.router)
    yield drafts, inbox, fake, calls, behavior, TestClient(app), tmp_path
    chat.close_all_engine_sessions()


def draft(setup, **kw):
    return setup[0].prepare('alice', 'conversation', 'office@example.com', 'Appointment inquiry',
        'Could you offer a time next Tuesday? Private appointment request.', **kw)


def test_preparation_restart_cancel_and_owner_isolation(setup):
    drafts, inbox, fake, calls, _, client, _ = setup
    view = draft(setup, request_key='request-one')
    assert not calls and not fake.calls
    assert view['state'] == 'draft'
    assert b'Private appointment request' not in drafts.path.read_bytes()
    assert drafts.path.stat().st_mode & 0o777 == 0o600
    assert PASSWORD not in json.dumps(view)
    assert draft(setup, request_key='request-one')['id'] == view['id']
    with pytest.raises(mail.EmailSendError): drafts.get('bob', view['id'])
    assert client.get('/api/email-drafts/' + view['id'], headers={'x-test-owner': 'bob'}).status_code == 409
    assert drafts.decide('alice', view['id'], view['revision'], 'cancel')['state'] == 'cancelled'
    assert drafts.decide('alice', view['id'], view['revision'], 'send')['state'] == 'cancelled'
    assert not calls


def test_exact_email_requires_matching_review_and_sends_once(setup):
    drafts, _, _, calls, _, client, _ = setup
    view = draft(setup)
    assert client.get('/api/email-drafts/' + view['id']).headers['cache-control'] == 'no-store'
    changed = drafts.edit('alice', view['id'], view['revision'], 'bookings@example.com', 'Availability', 'Exact reviewed message 🌎')
    assert changed['revision'] != view['revision']
    with pytest.raises(mail.EmailSendError): drafts.decide('alice', view['id'], view['revision'], 'send')
    assert not calls
    sent = drafts.decide('alice', changed['id'], changed['revision'], 'send')
    assert sent['state'] == 'accepted'
    drafts.decide('alice', changed['id'], changed['revision'], 'send')
    assert len([c for c in calls if c[0] == 'data']) == 1
    msg = BytesParser(policy=policy.default).parsebytes(next(c[1] for c in calls if c[0] == 'data'))
    assert msg['From'] == EMAIL and msg['To'] == 'bookings@example.com'
    assert msg['Subject'] == 'Availability' and msg.get_content().rstrip() == 'Exact reviewed message 🌎'
    assert not msg['Bcc'] and not msg['Cc'] and not msg['In-Reply-To']
    assert 'appointment confirmation' in sent['detail'].lower()


@pytest.mark.parametrize('where,state', [('login', 'failed'), ('data', 'unknown')])
def test_provider_errors_never_leak_or_retry(setup, where, state):
    drafts, _, _, calls, behavior, _, _ = setup
    view = draft(setup)
    behavior['failure'] = where
    result = drafts.decide('alice', view['id'], view['revision'], 'send')
    assert result['state'] == state and PASSWORD not in json.dumps(result)
    before = len(calls)
    drafts.decide('alice', view['id'], view['revision'], 'send')
    assert len(calls) == before


def test_concurrent_confirmations_claim_once(setup):
    drafts, _, _, calls, _, _, _ = setup
    view = draft(setup)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: drafts.decide('alice', view['id'], view['revision'], 'send'), range(8)))
    assert len([c for c in calls if c[0] == 'data']) == 1
    assert {v['state'] for v in results} <= {'sending', 'accepted'}


def test_mailbox_change_before_and_during_login_blocks_send(setup):
    drafts, inbox, _, calls, behavior, _, _ = setup
    view = draft(setup)
    inbox.select('alice', 'gmail')
    assert drafts.get('alice', view['id'])['state'] == 'expired'
    with pytest.raises(mail.EmailSendError): drafts.decide('alice', view['id'], view['revision'], 'send')
    assert not calls
    inbox.connect_zoho('alice', EMAIL, PASSWORD, 'imappro.zoho.com')
    view = draft(setup)
    behavior['login'] = lambda: inbox.disconnect('alice')
    assert drafts.decide('alice', view['id'], view['revision'], 'send')['state'] == 'failed'
    assert not any(c[0] == 'data' for c in calls)


def test_source_lease_serializes_disconnect_with_submission(setup):
    drafts, inbox, _, _, behavior, _, _ = setup
    view = draft(setup)
    entered, release, disconnected = threading.Event(), threading.Event(), threading.Event()
    def hold():
        entered.set()
        assert release.wait(3)
    behavior['data'] = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(drafts.decide, 'alice', view['id'], view['revision'], 'send')
        assert entered.wait(3)
        def disconnect():
            inbox.disconnect('alice'); disconnected.set()
        stopped = pool.submit(disconnect)
        assert not disconnected.wait(.03), 'Disconnect must not finish while approved DATA is in flight'
        release.set()
        assert sent.result()['state'] == 'accepted'
        stopped.result()
    assert inbox.view('alice')['source'] == 'none'


@pytest.mark.parametrize('to,subject,body', [
    ('office@example.com\r\nBcc: attacker@example.com', 'Subject', 'Body'),
    ('a@example.com,b@example.com', 'Subject', 'Body'), ('Office', 'Subject', 'Body'),
    ('a@example.com', 'Subject\nBcc: attacker@example.com', 'Body'),
    ('a@example.com', 'Subject', '\0bad'), ('a@example.com', 'Subject', '🌎' * 6001),
])
def test_invalid_mail_and_header_injection_never_prepare_or_send(setup, to, subject, body):
    with pytest.raises(mail.EmailSendError): setup[0].prepare('alice', 'conversation', to, subject, body)
    assert not setup[3]


def test_threaded_reply_uses_verified_headers_not_supplied_recipient(setup):
    drafts, inbox, fake, calls, _, _, _ = setup
    parent_id = inbox.reader('alice').message(inbox.reader('alice')._id(fake.validity, 20))['id']
    view = drafts.prepare('alice', 'conversation', 'attacker@example.com', 'Fake subject', 'Yes, Tuesday at 10 works.', parent_id=parent_id)
    assert view['to'] == 'bookings@example.com' and view['subject'] == 'Re: Appointment'
    assert view['reply'] and 'Office' in view['original_sender'] and not calls
    with pytest.raises(mail.EmailSendError): drafts.edit('alice', view['id'], view['revision'], 'attacker@example.com', view['subject'], view['body'])
    drafts.decide('alice', view['id'], view['revision'], 'send')
    msg = BytesParser(policy=policy.default).parsebytes(next(c[1] for c in calls if c[0] == 'data'))
    assert msg['To'] == 'bookings@example.com'
    assert msg['In-Reply-To'] == '<parent@office.example>'
    assert msg['References'] == '<older@office.example> <parent@office.example>'


def test_foreign_reference_duplicate_headers_and_forward_text_cannot_supply_reply_target(setup):
    drafts, inbox, fake, _, _, _, _ = setup
    parent = inbox.reader('alice')._id(fake.validity, 20)
    with pytest.raises(connectors.ConnectorError): drafts.prepare('alice', 'conversation', '', 'Reply', 'Body', parent_id='zm1_foreign_88_20')
    fake.headers[20] = b'From: one@example.com\r\nFrom: two@example.com\r\nMessage-ID: <parent@example.com>\r\n\r\n'
    with pytest.raises(connectors.ConnectorError): drafts.prepare('alice', 'conversation', '', 'Reply', 'Body', parent_id=parent)
    fake.headers[20] = HEADER
    with pytest.raises(connectors.ConnectorError): drafts.prepare('alice', 'conversation', '', 'Reply', 'Body', parent_id=parent)


def test_read_reply_requires_exact_sender_and_thread_and_keeps_source_boundaries(setup):
    drafts, inbox, fake, _, _, _, _ = setup
    view = draft(setup)
    drafts.decide('alice', view['id'], view['revision'], 'send')
    with drafts.db() as db:
        row = drafts._row('alice', view['id'], db)
    sent_id = connectors.unseal_tokens(row['sealed'])['message_id']
    fake.headers[20] = ('From: Office <office@example.com>\r\nSubject: Confirmed\r\nIn-Reply-To: ' + sent_id + '\r\n\r\n').encode()
    result = drafts.replies('alice', view['id'])
    assert result['status'] == 'reply' and result['selected']['id'].startswith('zm1_')
    assert all(c[0] not in {'STORE', 'APPEND', 'DELETE'} for c in fake.calls)
    fake.headers[20] = fake.headers[20].replace(b'office@example.com', b'attacker@example.com')
    assert drafts.replies('alice', view['id'])['status'] == 'no_reply'
    inbox.disconnect('alice')
    with pytest.raises(mail.EmailSendError): drafts.replies('alice', view['id'])


def test_api_requires_auth_same_origin_exact_fields_and_revision(setup, monkeypatch):
    drafts, _, _, calls, _, client, _ = setup
    view = draft(setup)
    root = '/api/email-drafts/' + view['id']
    for headers in ({'origin': 'https://attacker.example'}, {'sec-fetch-site': 'cross-site'}):
        assert client.post(root + '/decision', json={'revision': view['revision'], 'decision': 'send'}, headers=headers).status_code == 403
    assert client.post(root + '/decision', json={'decision': 'send'}).status_code == 400
    assert client.post(root + '/decision', json={'revision': view['revision'], 'decision': 'send', 'to': 'attacker@example.com'}).status_code == 400
    assert client.post(root + '/edit', content=b'x' * 32769).status_code == 413
    assert not calls
    def deny(request): raise HTTPException(401, 'Sign in')
    monkeypatch.setattr(connections_api, '_require_user', deny)
    assert client.get(root).status_code == 401
    assert client.post(root + '/decision', json={'revision': view['revision'], 'decision': 'send'}).status_code == 401


def test_crash_after_claim_stays_uncertain_without_retry(setup):
    drafts, _, _, _, _, _, _ = setup
    view = draft(setup)
    calls = []
    def crash(*a):
        calls.append(1); raise SystemExit()
    drafts.sender = crash
    with pytest.raises(SystemExit): drafts.decide('alice', view['id'], view['revision'], 'send')
    with drafts.db() as db:
        db.execute('UPDATE drafts SET updated=? WHERE id=?', (time.time() - 301, view['id']))
    assert drafts.get('alice', view['id'])['state'] == 'unknown'
    assert drafts.decide('alice', view['id'], view['revision'], 'send')['state'] == 'unknown'
    assert len(calls) == 1


def turn(cid, text, **kw):
    async def run(): return [f async for f in chat.stream_chat('alice', cid, text, **kw)]
    return asyncio.run(run())


def test_natural_email_and_send_it_prepare_only_and_survive_reload(setup, monkeypatch):
    drafts, _, _, calls, _, _, _ = setup
    sent = []
    async def reply(**kw):
        sent.append(kw)
        return {'content': json.dumps({'subject': 'Appointment inquiry', 'body': 'Could you offer a Tuesday afternoon appointment?'})}
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=reply))
    monkeypatch.setattr(chat.dev_bot, 'submit_calendar_writer_task', lambda *a, **k: pytest.fail('Email reached calendar'))
    cid = chat.create_conversation('alice')['conversation_id']
    result = turn(cid, 'Email office@example.com asking for an appointment next Tuesday afternoon', request_id='mail-one')
    draft_id = result[-1]['email_draft']['id']
    assert drafts.get('alice', draft_id)['state'] == 'draft' and not calls
    assert PASSWORD not in json.dumps(sent) and EMAIL not in json.dumps(sent)
    assert turn(cid, 'Send it')[-1]['email_draft']['id'] == draft_id and not calls
    assert turn(cid, 'Can you send the email?')[-1]['email_draft']['id'] == draft_id and not calls
    assert email_chat.request('Send me the email about the appointment', chat.get_conversation('alice', cid)) is None
    assert chat.get_conversation('alice', cid)['messages'][-1]['email_draft']['id'] == draft_id
    assert turn(cid, 'What is the email status?')[-1]['content'].startswith('Review')
    turn(cid, 'Email office@example.com asking for an appointment next Tuesday afternoon', request_id='mail-one')
    with drafts.db() as db: assert db.execute('SELECT count(*) FROM drafts').fetchone()[0] == 1


def test_missing_recipient_bad_model_response_and_cancellation_never_send(setup, monkeypatch):
    cid = chat.create_conversation('alice')['conversation_id']
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: pytest.fail('Missing recipient used model'))
    assert 'recipient' in turn(cid, 'Email my dentist asking for an appointment')[-1]['content']
    async def invalid(**kw): return {'content': 'I sent it!'}
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=invalid))
    assert 'valid email draft' in turn(cid, 'Email office@example.com ask for a booking')[-1]['content']
    event = asyncio.Event(); event.set()
    assert turn(cid, 'Email office@example.com ask for a booking', cancel_event=event)[-1]['status'] == 'cancelled'
    assert not setup[3]


def test_engine_draft_is_review_only_owner_scoped_and_durable_before_final_reply(setup, monkeypatch):
    _, _, _, calls, _, _, tmp_path = setup
    provider_profiles.save_profile('alice', {'id': 'test-profile', 'name': 'Test', 'provider': 'openai',
        'base_url': 'https://api.openai.com/v1', 'api_key': 'test-key', 'models': ['test-model']})
    ws = tmp_path / 'rift'; ws.mkdir()
    bots.add_bot('coordinator', 'The Overwatcher', 'openai:test-model', str(ws), owner='alice',
        status='running', policy=serve.coordinator_preset_policy(), provider_profile_id='test-profile')
    cid = chat.create_conversation('alice', bot_id='coordinator')['conversation_id']
    session = object.__new__(chat.EngineSession)
    session.bot_id = 'coordinator'; session.allowed_tools = {'email_draft'}
    session.email_owner = 'alice'; session.email_conversation = cid
    session._email_assistant_identity = 'draft-result'; session._email_request_key = 'native-tool-request'
    ok, result = session._handle_email_draft({'action': 'prepare', 'to': 'office@example.com', 'subject': 'Appointment', 'body': 'Any Tuesday availability?'})
    assert ok and result['state'] == 'draft' and not calls
    assert 'revision' not in result and 'source_revision' not in result
    assert chat.get_conversation('alice', cid)['messages'][-1]['email_draft']['id'] == result['id']
    assert not session._handle_email_draft({'action': 'send', 'draft_id': result['id']})[0]
    session.email_owner = 'bob'
    assert not session._handle_email_draft({'action': 'status', 'draft_id': result['id']})[0]
    session.allowed_tools = set()
    assert not session._handle_email_draft({'action': 'prepare', 'to': 'office@example.com'})[0]
    assert not calls


def test_native_selected_reply_and_read_back_support_followup(setup, monkeypatch):
    drafts, inbox, fake, calls, _, _, _ = setup
    cid = chat.create_conversation('alice')['conversation_id']
    conv = chat.get_conversation('alice', cid)
    conv['gmail_selected'] = {'id': inbox.reader('alice')._id(fake.validity, 20), 'subject': 'Appointment'}
    chat._write('alice', conv)
    async def reply(**kw): return {'content': json.dumps({'subject': 'Reply', 'body': 'Yes, Tuesday at 10 works.'})}
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=reply))
    result = turn(cid, 'Reply to that email saying Tuesday at 10 works')
    view = drafts.get('alice', result[-1]['email_draft']['id'])
    assert view['to'] == 'bookings@example.com' and view['reply'] and not calls
    drafts.decide('alice', view['id'], view['revision'], 'send')
    with drafts.db() as db: payload = connectors.unseal_tokens(drafts._row('alice', view['id'], db)['sealed'])
    fake.headers[20] = ('From: bookings@example.com\r\nSubject: Confirmed appointment\r\nIn-Reply-To: ' + payload['message_id'] + '\r\n\r\n').encode()
    result = turn(cid, 'Check email replies')
    assert result[-1]['email_draft']['id'] == view['id']
    assert 'Reply from bookings@example.com' in result[-1]['content']
    assert chat.get_conversation('alice', cid)['gmail_selected']['subject'] == 'Confirmed appointment'


def test_chat_sse_transports_review_card_and_durable_terminal():
    import chat_api
    async def gen():
        yield {'type': 'email_draft', 'draft_id': 'saved-draft'}
        yield {'type': 'status', 'status': 'complete', 'content': 'Review ready', 'email_draft': {'id': 'saved-draft'}}
    async def run(): return [value async for value in chat_api._drive_stream(gen(), 'request', 'conversation')]
    stream = ''.join(asyncio.run(run()))
    assert '"type": "email_draft"' in stream and '"draft_id": "saved-draft"' in stream
    assert '"email_draft": {"id": "saved-draft"}' in stream


def test_actual_ndjson_engine_routes_only_prepare_to_host(setup):
    import queue
    _, _, _, calls, _, _, _ = setup
    cid = chat.create_conversation('alice')['conversation_id']
    session = object.__new__(chat.EngineSession)
    session._closed = False; session._turn_lock = threading.Lock(); session._stderr_lock = threading.Lock()
    session.stderr_tail = []; session._proc = MagicMock(); session._proc.poll.return_value = None
    session._frames = queue.Queue(); session.surface_context = None; session.delegation_ctx = None
    session.bot_id = None; session.allowed_tools = {'email_draft'}
    session.email_owner = 'alice'; session.email_conversation = cid
    session._email_assistant_identity = 'engine-result'; session._email_request_key = 'engine-request'
    session.denied_requests = []
    session._frames.put({'type': 'confirm_request', 'id': 'prepare', 'value': 'email_draft',
        'action': 'prepare', 'to': 'office@example.com', 'subject': 'Appointment', 'body': 'Any Tuesday availability?'})
    session._frames.put({'type': 'confirm_request', 'id': 'send', 'value': 'email_draft', 'action': 'send'})
    session._frames.put({'type': 'chat_done', 'content': 'Review ready'})
    session._frames.put({'type': 'phase', 'value': 'IDLE'})
    sent = []; session._send = sent.append
    assert session.run_turn('Contact the office', lambda t: None) == ('Review ready', None)
    assert sent[1]['approved'] is True and sent[1]['result']['state'] == 'draft'
    assert sent[2]['approved'] is False and not calls


def test_agentmail_and_personal_gmail_cannot_send(setup):
    drafts, inbox, _, calls, _, _, _ = setup
    inbox.select('alice', 'gmail')
    with pytest.raises(mail.EmailSendError): draft(setup)
    inbox.request = lambda path, key, params=None: {'messages': []} if '/messages' in path else {'inbox_id': 'inb_test', 'email': 'assistant@agentmail.to'}
    inbox.connect('alice', 'inb_test', 'agentmail-key-' + 'x' * 25)
    with pytest.raises(mail.EmailSendError): draft(setup)
    assert not calls


def test_missing_address_followup_and_natural_edits_keep_one_unsent_review(setup, monkeypatch):
    drafts, _, _, calls, _, _, _ = setup
    cid = chat.create_conversation('alice')['conversation_id']
    bodies = []
    async def reply(**kw):
        request = json.loads(kw['messages'][1]['content'])
        bodies.append(request)
        return {'content': json.dumps({'subject': 'Appointment', 'body': 'Wednesday afternoon please.' if request['previous_draft'] else 'Tuesday afternoon please.'})}
    monkeypatch.setattr(chat, 'get_provider', lambda *a, **k: SimpleNamespace(chat=reply))
    assert 'recipient' in turn(cid, 'Email my dentist asking for an appointment Tuesday afternoon')[-1]['content']
    result = turn(cid, 'office@example.com')
    job = drafts.get('alice', result[-1]['email_draft']['id'])
    assert 'my dentist' in bodies[-1]['owner_request'] and 'office@example.com' in bodies[-1]['owner_request']
    assert 'current_local_date' in bodies[-1] and job['body'].startswith('Tuesday')
    result = turn(cid, 'Change it to Wednesday afternoon instead')
    edited = drafts.get('alice', result[-1]['email_draft']['id'])
    assert edited['id'] == job['id'] and edited['revision'] != job['revision']
    assert edited['body'].startswith('Wednesday') and not calls
    assert email_chat.request('Make a calendar event for that appointment', chat.get_conversation('alice', cid)) is None
    drafts.decide('alice', edited['id'], edited['revision'], 'send')
    assert 'Only an unsent draft' in turn(cid, 'Change it to Friday')[-1]['content']
    assert len([c for c in calls if c[0] == 'data']) == 1
