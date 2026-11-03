"""Bank protocol, privacy boundary, lifecycle and accounting without bank access."""
import asyncio
from datetime import date, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import bots
import budget_bank as bank
import budget_bank_api as api
import budget_bank_chat as bank_chat
import budget_plaid as plaid
import budget_watch
import chat_service
import connectors
from task_store import TaskWorker
from test_budget_watch import f

CANARY = 'PRIVATE-MERCHANT-LOCATION-ACCOUNT-DESCRIPTION'
REAL_PLAID_CALL = plaid.Plaid.call


@pytest.fixture
def b(f, monkeypatch):
    monkeypatch.setenv('KYREX_PLAID_CLIENT_ID', 'inert-bank-client-fixture')
    monkeypatch.setenv('KYREX_PLAID_SECRET', 'inert-bank-secret-fixture')
    monkeypatch.setenv('KYREX_PLAID_RETURN_URL', 'https://chat.example.test/')
    monkeypatch.setenv('KYREX_PLAID_ENV', 'sandbox')
    calls, pages = [], []
    response = {'added': [], 'modified': [], 'removed': [], 'next_cursor': 'fixture-cursor',
                'has_more': False, 'transactions_update_status': 'HISTORICAL_UPDATE_COMPLETE'}
    def call(self, endpoint, **payload):
        calls.append((endpoint, payload))
        if endpoint == '/link/token/create':
            return {'link_token': 'inert-link-fixture', 'hosted_link_url': 'https://secure.plaid.com/hl/inert-fixture'}
        if endpoint == '/link/token/get':
            return {'link_sessions': [{'results': {'item_add_results': [{'public_token': 'inert-public-fixture'}]}}]}
        if endpoint == '/item/public_token/exchange': return {'access_token': 'inert-access-fixture'}
        if endpoint == '/accounts/get':
            return {'accounts': [
                {'account_id': 'cash-fixture', 'type': 'depository', 'subtype': 'checking', 'name': CANARY,
                 'balances': {'current': 999999, 'available': 888888}, 'mask': '9876'},
                {'account_id': 'card-fixture', 'type': 'credit', 'subtype': 'credit card', 'name': CANARY},
                {'account_id': 'retirement-fixture', 'type': 'investment', 'subtype': '401k', 'name': CANARY}]}
        if endpoint == '/transactions/sync':
            value = pages.pop(0) if pages else response
            if isinstance(value, Exception): raise value
            return value
        if endpoint == '/item/remove': return {}
        raise AssertionError(endpoint)
    monkeypatch.setattr(plaid.Plaid, 'call', call)
    def update(identity, **values):
        f.registry[identity].update(values)
        return f.registry[identity]
    monkeypatch.setattr(bots, 'update_bot', update)
    f.app.include_router(api.router)
    store = bank.BankStore(f.tasks)
    link = store.start('alice')
    store.finish('alice', link['flow'])
    today = date.today().isoformat()
    start = (date.today() - timedelta(days=13)).isoformat()
    def setup(**overrides):
        values = {'enabled': True, 'bot_id': 'budget', 'selected': [plaid.digest('cash-fixture'), plaid.digest('card-fixture')],
            'start_date': start, 'starting_balance': '1000.00', 'currency': 'USD', 'time_zone': 'UTC', 'interval_seconds': 21600}
        values.update(overrides)
        return store.configure('alice', store.get('alice')['revision'], **values)
    def tx(identity='purchase', **values):
        return {'transaction_id': identity, 'account_id': 'cash-fixture', 'date': today, 'amount': Decimal('145.00'),
            'iso_currency_code': 'USD', 'pending': False, 'merchant_name': CANARY, 'name': CANARY,
            'location': {'address': CANARY}, 'account_owner': CANARY,
            'personal_finance_category': {'primary': 'FOOD_AND_DRINK', 'detailed': 'FOOD_AND_DRINK_GROCERIES'}, **values}
    return SimpleNamespace(**locals())


def test_link_is_server_bound_single_exchange_and_no_banking_secrets_in_views(b):
    create = b.calls[0][1]
    assert create['products'] == ['transactions']
    assert not ({'optional_products', 'additional_consented_products', 'webhook'} & set(create))
    assert create['user']['client_user_id'] != 'alice'
    assert create['hosted_link']['completion_redirect_uri'].startswith('https://chat.example.test/?settings=budget&bank_link=')
    assert b.store.finish('alice', b.link['flow'])['state'] == 'connected'
    assert len([c for c in b.calls if c[0] == '/item/public_token/exchange']) == 1
    with pytest.raises(plaid.BankError): b.store.finish('bob', b.link['flow'])
    public = json.dumps(b.store.view('alice'))
    assert CANARY not in public and 'inert-access' not in public and 'cash-fixture' not in public
    assert '999999' not in public and '9876' not in public
    assert not b.store.view('bob')['accounts']
    with pytest.raises(plaid.BankError): b.store.start('alice')


def test_configure_explicitly_switches_off_receipts_and_narrows_selected_bot(b):
    view = b.setup()
    assert view['private_mode'] and view['privacy'] == 'category_totals_only'
    assert b.f.registry['budget']['policy'] == bank.POLICY
    assert not b.f.store.settings('alice')['enabled']
    assert bank.private_bot('alice', 'budget')
    with pytest.raises(ValueError, match='keeps email receipt scanning off'):
        b.f.store.configure('alice', enabled=True, bot_id='budget')
    assert b.store.get('alice')['private']['starting_balance'] == '1000.00'
    with pytest.raises(plaid.BankError, match='history requested'):
        b.setup(start_date='2000-01-01')


def test_normalizer_has_closed_fields_enums_and_decimal_arithmetic(b):
    selected = {a['id']: a for a in b.store.get('alice')['private']['accounts']}
    value = plaid.transaction(b.tx(amount=Decimal('0.29')), selected)
    assert value['amount_minor'] == 29 and value['category'] == 'groceries'
    assert set(value) == {'identity', 'account', 'kind', 'date', 'amount_minor', 'currency', 'category', 'pending'}
    assert CANARY not in json.dumps(value)
    assert plaid.transaction(b.tx(personal_finance_category={'primary': CANARY}), selected)['category'] == 'other'
    assert plaid.transaction(b.tx(account_id='unselected'), selected) is None
    for amount in [True, 'NaN', 'Infinity', '10.001', None]:
        with pytest.raises(plaid.BankError): plaid.transaction(b.tx(amount=amount), selected)
    for values in [{'date': CANARY}, {'iso_currency_code': CANARY}, {'pending': 'false'}]:
        with pytest.raises(plaid.BankError): plaid.transaction(b.tx(**values), selected)


def test_posted_spending_refunds_income_transfers_cards_and_cash_estimate(b):
    b.setup()
    b.response['added'] = [b.tx(), b.tx('card-food', account_id='card-fixture', amount=62,
            personal_finance_category={'primary': 'FOOD_AND_DRINK'}),
        b.tx('refund', amount=-5), b.tx('salary', amount=-500, personal_finance_category={'primary': 'INCOME'}),
        b.tx('transfer', amount=100, personal_finance_category={'primary': 'TRANSFER_OUT'}),
        b.tx('card-payment', amount=200, personal_finance_category={'primary': 'LOAN_PAYMENTS'}),
        b.tx('pending', amount=9, pending=True)]
    revision = b.store.get('alice')['revision']
    assert b.store.sync('alice', revision) == 7
    snapshot = b.store.model_view('alice')
    assert snapshot['totals_minor'] == {'USD': 20200}
    assert snapshot['income_minor'] == {'USD': 50000}
    assert snapshot['cash_estimate']['estimated_minor'] == 106000
    assert snapshot['pending_count'] == 1
    assert snapshot['history_loaded'] and snapshot['latest_attempt_ok']
    assert snapshot['trend']['current_spending_minor'] == {'USD': 20200}
    assert snapshot['trend']['start_date_covers_both_periods']
    serialized = json.dumps(snapshot)
    for forbidden in [CANARY, 'cash-fixture', 'card-fixture', 'inert-access', 'merchant', 'account_owner', '9876']:
        assert forbidden not in serialized
    # Normalized transaction payloads and grants are encrypted at rest.
    raw = b.f.tasks.db_path.read_bytes()
    for forbidden in [CANARY.encode(), b'inert-access-fixture', b'cash-fixture', b'1000.00']:
        assert forbidden not in raw


def test_pending_to_posted_modifications_removals_and_restart_deduplicate(b):
    b.setup()
    b.response['added'] = [b.tx('pending', pending=True)]
    revision = b.store.get('alice')['revision']; b.store.sync('alice', revision)
    assert b.store.model_view('alice')['totals_minor'] == {}
    b.response.update(added=[b.tx('posted', amount=150)], removed=[{'transaction_id': 'pending'}])
    b.store.sync('alice', revision)
    b.response.update(added=[], removed=[], modified=[b.tx('posted', amount=151)])
    bank.BankStore(b.f.tasks).sync('alice', revision)
    assert b.store.model_view('alice')['totals_minor'] == {'USD': 15100}
    with b.store.db() as db:
        assert db.execute('SELECT count(*) FROM budget_bank_transactions').fetchone()[0] == 1


def test_page_failure_rolls_back_rows_and_cursor_then_replays_from_original(b):
    b.setup()
    b.pages.extend([{**b.response, 'added': [b.tx()], 'next_cursor': 'page-two', 'has_more': True},
                    plaid.BankError('inert-private-provider-error')])
    revision = b.store.get('alice')['revision']
    with pytest.raises(plaid.BankError): b.store.sync('alice', revision)
    assert b.store.get('alice')['private']['cursor'] == ''
    assert b.store.model_view('alice')['totals_minor'] == {}
    b.response['added'] = [b.tx()]
    b.store.sync('alice', revision)
    assert b.calls[-1][1]['cursor'] == ''
    assert b.store.model_view('alice')['totals_minor'] == {'USD': 14500}


def test_pagination_bound_continues_as_incomplete_and_new_selection_restarts(b):
    b.setup()
    b.pages.extend([{**b.response, 'next_cursor': f'page-{i}', 'has_more': True} for i in range(10)])
    revision = b.store.get('alice')['revision']; b.store.sync('alice', revision)
    assert b.store.get('alice')['private']['cursor'] == ''
    assert b.store.get('alice')['private']['batch']['cursor'] == 'page-9'
    assert not b.store.model_view('alice')['history_loaded']
    assert b.store.get('alice')['next_scan'] <= __import__('time').time() + 31
    b.setup(selected=[plaid.digest('cash-fixture')])
    assert b.store.get('alice')['private']['cursor'] == ''
    assert 'batch' not in b.store.get('alice')['private']


def test_pause_scope_change_and_foreign_accounts_fence_inflight_sync(b):
    b.setup()
    revision = b.store.get('alice')['revision']
    b.response['added'] = [b.tx()]
    calls = []
    def active():
        calls.append(True)
        if len(calls) == 2: b.setup(enabled=False)
    with pytest.raises(plaid.BankError): b.store.sync('alice', revision, active)
    assert b.store.model_view('alice')['totals_minor'] == {}
    with pytest.raises(plaid.BankError): b.setup(selected=['foreign-account-reference'])
    with pytest.raises(plaid.BankError): b.setup(starting_balance='NaN')


def test_durable_worker_uses_existing_executor_without_model_or_private_results(b):
    b.setup()
    b.response['added'] = [b.tx()]
    budget_watch.submit_due(b.f.tasks); budget_watch.submit_due(b.f.tasks)
    assert len(b.f.tasks.list_tasks()) == 1
    worker = TaskWorker(b.f.tasks, worker_id='bank-fixture', send=lambda *a: 'fixture', edit=lambda *a: None)
    assert worker.claim_and_execute_once()
    task = b.f.tasks.list_tasks()[0]
    assert task['status'] == 'done', task
    assert b.store.model_view('alice')['totals_minor'] == {'USD': 14500}
    serialized = json.dumps(task, default=str)
    assert CANARY not in serialized and '14500' not in serialized and 'inert-access' not in serialized


def test_disconnect_blocks_sync_clears_data_and_preserves_private_mode(b):
    b.setup(); revision = b.store.get('alice')['revision']; b.store.sync('alice', revision)
    view = b.store.disconnect('alice', revision)
    assert view['state'] == 'disconnected' and view['private_mode'] and not view['enabled']
    assert bank.private_bot('alice', 'budget')
    with pytest.raises(plaid.BankError): b.store.sync('alice', revision)
    assert b.store.model_view('alice')['status'] == 'disconnected'
    assert 'access_token' not in b.store.get('alice')['private']
    assert b.calls[-1][0] == '/item/remove'


def test_revocation_failure_stops_locally_retains_only_sealed_retry_credential(b, monkeypatch):
    b.setup()
    real = plaid.Plaid.call
    def fail(self, endpoint, **kwargs):
        if endpoint == '/item/remove': raise plaid.BankError('inert transport failure')
        return real(self, endpoint, **kwargs)
    monkeypatch.setattr(plaid.Plaid, 'call', fail)
    with pytest.raises(plaid.BankError, match='Provider disconnection is pending'):
        b.store.disconnect('alice', b.store.get('alice')['revision'])
    row = b.store.get('alice')
    assert not row['enabled'] and row['state'] == 'disconnect_pending'
    assert set(row['private']) == {'access_token', 'binding', 'environment', 'private_mode'}
    monkeypatch.setattr(plaid.Plaid, 'call', real)
    assert b.store.disconnect('alice', row['revision'])['state'] == 'disconnected'


def test_host_tool_returns_only_bank_totals_and_rechecks_budget_policy(b):
    b.setup(); b.response['added'] = [b.tx()]; b.store.sync('alice', b.store.get('alice')['revision'])
    session = object.__new__(chat_service.EngineSession)
    session.budget_owner = 'alice'; session.bot_id = 'budget'; session.allowed_tools = ['budget_read']
    ok, value = session._handle_budget_read({})
    assert ok and value['privacy'] == 'category_totals_only'
    assert not {'entries', 'subscriptions', 'accounts'} & set(value)
    b.f.registry['budget']['policy']['budget:read'] = 'deny'
    assert not session._handle_budget_read({})[0]


def test_api_auth_origin_owner_and_no_raw_provider_payloads(b, monkeypatch):
    path = '/api/automations/budget/bank'
    assert b.f.client.get(path).status_code == 200
    assert b.f.client.get(path, headers={'x-user': 'bob'}).json()['state'] == 'disconnected'
    assert b.f.client.post(path + '/finish', json={'flow': b.link['flow']}).status_code == 403
    assert b.f.client.post(path + '/finish', json={'flow': b.link['flow']}, headers={'origin': 'https://evil.example'}).status_code == 403
    result = b.f.client.post(path + '/finish', json={'flow': b.link['flow']}, headers={'origin': 'http://testserver'})
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    assert CANARY not in result.text and 'inert-access' not in result.text
    assert b.f.client.post(path + '/finish', json={'flow': b.link['flow']}, headers={'origin': 'http://testserver', 'x-user': 'bob'}).status_code == 409
    assert b.f.client.post(path + '/finish', json={'flow': b.link['flow'], 'access_token': 'forbidden'}, headers={'origin': 'http://testserver'}).status_code == 422
    import main
    monkeypatch.setattr(budget_watch, 'eligible', lambda *a, **kw: b.f.registry['budget'])
    from test_budget_watch import REAL_OWNER
    monkeypatch.setattr(api.budget_api, 'owner', REAL_OWNER)
    assert TestClient(b.f.app).get(path).status_code == 401


def test_private_model_request_excludes_legacy_context_and_uses_exact_bot_provider(b, monkeypatch):
    b.setup(); b.response['added'] = [b.tx()]; b.store.sync('alice', b.store.get('alice')['revision'])
    calls, saved = [], []
    async def reply(**kwargs): calls.append(kwargs); return {'content': 'Your category totals are ready.'}
    cfg = {'provider': 'fixture', 'model': 'exact-bank-model', 'api_key': 'inert-key', 'base_url': '', 'headers': {}}
    monkeypatch.setattr(bank_chat.bot_provider, 'resolve_bot_provider', lambda *a: cfg)
    monkeypatch.setattr(chat_service, 'get_provider', lambda *a, **kw: SimpleNamespace(chat=reply))
    monkeypatch.setattr(chat_service, '_write', lambda *a: saved.append(a))
    conv = {'conversation_id': 'bank-chat', 'bot_id': 'budget', 'messages': [
        {'role': 'assistant', 'content': CANARY},
        {'role': 'user', 'content': 'Compare this week.', 'budget_private': True}]}
    async def run():
        return [v async for v in bank_chat.stream(chat_service, 'alice', conv, 'How is spending?', 'bank-turn', asyncio.Event())]
    events = asyncio.run(run())
    assert events[-1]['status'] == 'complete'
    assert calls[0]['tools'] is None and calls[0]['model'] == 'exact-bank-model'
    wire = json.dumps(calls[0]['messages'])
    assert CANARY not in wire and 'inert-key' not in wire and 'inert-access' not in wire
    assert 'Compare this week.' in wire and '14500' in wire
    assert conv['messages'][-1]['budget_private'] is True


def test_pagination_mutation_discards_staging_across_tasks_and_restarts_original_cursor(b):
    b.setup()
    b.pages.extend([{**b.response, 'added': [b.tx(f'staged-{i}')], 'next_cursor': f'page-{i}', 'has_more': True} for i in range(10)])
    revision = b.store.get('alice')['revision']
    b.store.sync('alice', revision)
    assert b.store.model_view('alice')['totals_minor'] == {}, 'partial pages never publish spending'
    b.pages.append(plaid.BankError('Pagination changed.', 'pagination_changed'))
    b.store.sync('alice', revision)
    assert b.calls[-1][1]['cursor'] == 'page-9'
    assert 'batch' not in b.store.get('alice')['private']
    b.response['added'] = [b.tx('fresh-only')]
    b.store.sync('alice', revision)
    assert b.calls[-1][1]['cursor'] == ''
    assert b.store.model_view('alice')['totals_minor'] == {'USD': 14500}


def test_transport_is_fixed_origin_bounded_no_redirects_and_error_text_is_redacted(b, monkeypatch):
    import httpx
    real_client = httpx.Client
    wire, options = [], []
    def handle(request):
        wire.append(request)
        return httpx.Response(400, json={'error_code': 'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION',
                                         'error_message': CANARY, 'display_message': CANARY})
    def client(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(httpx, 'Client', client)
    # Restore the real adapter method; all HTTP requests still use MockTransport.
    monkeypatch.setattr(plaid.Plaid, 'call', REAL_PLAID_CALL)
    instance = plaid.Plaid()
    with pytest.raises(plaid.BankError) as error:
        instance.call('/transactions/sync', access_token='inert-transport-fixture')
    assert error.value.code == 'pagination_changed' and CANARY not in str(error.value)
    assert str(wire[0].url) == 'https://sandbox.plaid.com/transactions/sync'
    assert options[0]['follow_redirects'] is False and options[0]['trust_env'] is False
    for endpoint in ['/transfer/create', '/auth/get', '/identity/get', 'https://evil.example/accounts/get']:
        with pytest.raises(plaid.BankError): instance.call(endpoint)
    assert len(wire) == 1


def test_environment_changes_and_budget_revocation_block_worker_reads(b, monkeypatch):
    b.setup(); revision = b.store.get('alice')['revision']
    monkeypatch.setenv('KYREX_PLAID_ENV', 'production')
    with pytest.raises(plaid.BankError, match='environment changed'): b.store.sync('alice', revision)
    monkeypatch.setenv('KYREX_PLAID_ENV', 'sandbox')
    b.f.registry['budget']['policy']['budget:read'] = 'deny'
    with pytest.raises(ValueError): b.store.sync('alice', revision)
    assert not [c for c in b.calls if c[0] == '/transactions/sync']


def test_unknown_exchange_never_retries_and_stale_inflight_state_can_be_disconnected(b, monkeypatch):
    b.store.disconnect('alice', b.store.get('alice')['revision'])
    link = b.store.start('alice')
    real = plaid.Plaid.call
    def fail(self, endpoint, **kwargs):
        if endpoint == '/item/public_token/exchange': raise plaid.BankError('Transport lost after send.')
        return real(self, endpoint, **kwargs)
    monkeypatch.setattr(plaid.Plaid, 'call', fail)
    with pytest.raises(plaid.BankError, match='outcome is unknown'): b.store.finish('alice', link['flow'])
    assert b.store.get('alice')['state'] == 'exchange_unknown'
    with pytest.raises(plaid.BankError): b.store.finish('alice', link['flow'])
    row = b.store.get('alice')
    b.store._write('alice', row['revision'], state='exchanging', data={**row['private'], 'exchange_started': 0})
    value = b.store.disconnect('alice', b.store.get('alice')['revision'])
    assert value['state'] == 'disconnected' and 'revoke any Kyrex connection' in value['detail']


def test_private_chat_entry_bypasses_all_native_location_and_receipt_routes(b, monkeypatch):
    b.setup()
    conv = {'conversation_id': 'private-chat', 'bot_id': 'budget', 'messages': [{'role': 'assistant', 'content': CANARY}]}
    monkeypatch.setattr(chat_service, 'get_conversation', lambda *a: conv)
    monkeypatch.setattr(chat_service, 'resolve_bot_for_user', lambda *a: b.f.registry['budget'])
    seen = []
    async def private(*args):
        seen.append(args)
        yield {'type': 'status', 'status': 'complete', 'content': 'Private totals only.'}
    monkeypatch.setattr(bank_chat, 'stream', private)
    import chat_maps
    monkeypatch.setattr(chat_maps, 'route_request', lambda *a: pytest.fail('Private budgets must bypass native routing.'))
    async def run(**kw):
        return [v async for v in chat_service.stream_chat('alice', 'private-chat', 'Read my latest email receipt', request_id='private', **kw)]
    assert asyncio.run(run())[-1]['content'] == 'Private totals only.'
    assert len(seen) == 1
    assert asyncio.run(run(device_location={'latitude': 1, 'longitude': 2}))[-1]['content'] == 'Private totals only.'
    assert len(seen) == 2, 'ordinary attached location is ignored, never sent to the private model'
    with pytest.raises(chat_service.ChatUnavailable, match='filtered budget totals only'):
        asyncio.run(run(image_ids=['bank-statement-image']))


def test_private_chat_cancel_stops_model_and_does_not_save_a_fake_reply(b, monkeypatch):
    b.setup()
    interrupted = []
    async def reply(**kwargs):
        try:
            kwargs['interrupt_event'].set()
            await asyncio.Event().wait()
        finally: interrupted.append(True)
    cfg = {'provider': 'fixture', 'model': 'owner-model', 'api_key': 'inert-key', 'base_url': ''}
    monkeypatch.setattr(bank_chat.bot_provider, 'resolve_bot_provider', lambda *a: cfg)
    monkeypatch.setattr(chat_service, 'get_provider', lambda *a, **kw: SimpleNamespace(chat=reply))
    monkeypatch.setattr(chat_service, '_write', lambda *a: None)
    conv = {'conversation_id': 'private-cancel', 'bot_id': 'budget', 'messages': []}
    async def run():
        return [v async for v in bank_chat.stream(chat_service, 'alice', conv, 'Trend?', 'cancel', asyncio.Event())]
    events = asyncio.run(run())
    assert interrupted and events[-1]['status'] == 'cancelled'
    assert [m['role'] for m in conv['messages']] == ['user']


def test_host_plaid_configuration_is_removed_from_chat_and_headless_children(b, monkeypatch):
    from pathlib import Path
    from unittest.mock import MagicMock, patch
    from test_chat_bot_policy import _spawn_capture
    from headless_agent import HeadlessAgent
    cfg = {'provider': 'openai', 'model': 'inert-model', 'api_key': 'inert-model-key', 'base_url': ''}
    captured = _spawn_capture(b.f.tmp_path, cfg, {'allowed_tools': ['budget_read', 'task_complete']})
    assert not any(k.startswith('KYREX_PLAID_') for k in captured['env'])
    spawned = {}
    process = SimpleNamespace(stdin=MagicMock(), stdout=[], stderr=[])
    def popen(*args, **kwargs): spawned.update(kwargs); return process
    agent = HeadlessAgent(Path('inert-bridge.py'), b.f.tmp_path)
    with patch('headless_agent.subprocess.Popen', side_effect=popen), patch.object(agent, '_send'):
        agent.out_q.put(('stdout', '{"type":"phase","value":"IDLE"}'))
        assert agent.start('read')
    assert not any(k.startswith('KYREX_PLAID_') for k in spawned['env'])


def test_pause_remains_available_after_bot_and_provider_disappear(b, monkeypatch):
    b.setup()
    b.f.registry.clear()
    monkeypatch.delenv('KYREX_PLAID_SECRET')
    assert not b.store.pause('alice', b.store.get('alice')['revision'])['enabled']
    with pytest.raises(plaid.BankError): b.store.pause('bob', b.store.get('alice')['revision'])


def test_two_owners_with_same_native_ids_have_separate_grants_ledgers_and_scans(b):
    b.setup(); b.response['added'] = [b.tx()]
    b.store.sync('alice', b.store.get('alice')['revision'])
    b.f.registry['budget-bob'] = {**b.f.registry['budget'], 'id': 'budget-bob', 'owner': 'bob'}
    flow = b.store.start('bob')['flow']; b.store.finish('bob', flow)
    b.store.configure('bob', b.store.get('bob')['revision'], enabled=True, bot_id='budget-bob',
        selected=[plaid.digest('cash-fixture')], start_date=b.start, starting_balance='2000.00')
    b.response['added'] = [b.tx(amount=300)]
    b.store.sync('bob', b.store.get('bob')['revision'])
    assert b.store.model_view('alice')['totals_minor'] == {'USD': 14500}
    assert b.store.model_view('bob')['totals_minor'] == {'USD': 30000}
    assert b.store.model_view('bob')['cash_estimate']['estimated_minor'] == 170000
    b.f.registry['budget']['status'] = 'paused'
    bank.submit_due(b.f.tasks, __import__('time').time() + 86400)
    tasks = b.f.tasks.list_tasks()
    assert len(tasks) == 1 and tasks[0]['chat_id'] == 'bob'
