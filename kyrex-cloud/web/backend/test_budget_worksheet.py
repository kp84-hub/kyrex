"""Worksheet projection and real Chat/API persistence without live accounts."""
import asyncio
from datetime import date, timedelta
import json
from types import SimpleNamespace

import pytest

import budget_api
import budget_bank_chat
import budget_worksheet as worksheet
import budget_worksheet_api as api
import chat_service
from test_budget_bank import b
from test_budget_watch import f

PATH = '/api/automations/budget/worksheet'
ORIGIN = {'origin': 'http://testserver'}


def saved(store, owner='alice'):
    with store.db() as db:
        return store._plan(db, owner, 'USD')[1]


def test_manual_cash_flow_orders_bills_before_income_and_excludes_overdue_income():
    today = date(2026, 11, 20)
    plan = {'currency': 'USD', 'balance': {'date': '2026-11-20', 'amount_minor': 50000}, 'rows': {}}
    commands = [
        'Internet is $80 on the 25th every month and Payday is $2,200 on 2026-11-25',
        'Add Old deposit as income $500 on 2026-11-19',
        'Mark Internet paid',
    ]
    for command in commands[:2]:
        _, actions = worksheet.command(command, today)
        for action in actions:
            worksheet._change(plan, today, action)
    value = worksheet.project(plan, today)
    assert value['current_minor'] == 50000
    assert value['next_income'] == '2026-11-25'
    assert value['before_income_minor'] == value['low_minor'] == 42000
    assert value['chart'][5]['balance_minor'] == 262000
    assert len(value['chart']) == 30

    worksheet._change(plan, today, worksheet.command(commands[2], today)[1][0])
    value = worksheet.project(plan, today)
    assert value['current_minor'] == value['before_income_minor'] == 42000
    assert value['chart'][5]['expense'] == 0
    assert next(row for row in value['rows'] if row['title'] == 'Internet')['next_due'] == '2026-12-25'


def test_monthly_anchor_remains_the_same_across_short_months():
    today = date(2027, 2, 27)
    _, actions = worksheet.command('Rent is $100 on the 31st every month', today)
    plan = {'rows': {}}
    worksheet._change(plan, today, actions[0])
    row = next(iter(plan['rows'].values()))
    assert list(worksheet._dates(row, date(2027, 4, 30))) == [
        date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)]


@pytest.mark.parametrize(('text', 'due', 'amount_minor'), [
    ('Add Club 6 35$ on Nov. 29th', '2026-11-29', 3500),
    ('Add Insurance $125 on November 24', '2026-11-24', 12500),
    ('Add Utilities 42.50 $ on December 1st, 2026', '2026-12-01', 4250),
    ('Add Club dues $35 on Nov. 1st', '2027-11-01', 3500),
    ('Add Club dues $35 on November 1, 2026', '2026-11-01', 3500),
    ('“Add Club 6 $35 on 2026-11-29.', '2026-11-29', 3500),
    ('“Add Club 6 $35 on 2026-11-29.”', '2026-11-29', 3500),
    ('"Add Club 6 $35 on 2026-11-29".', '2026-11-29', 3500),
])
def test_named_budget_dates_and_currency_symbol_placement(text, due, amount_minor):
    today = date(2026, 11, 20)
    intent, actions = worksheet.command(text, today)
    assert intent == 'write'
    plan = {'rows': {}}
    worksheet._change(plan, today, actions[0])
    row = next(iter(plan['rows'].values()))
    assert (row['date'], row['amount_minor'], row['kind']) == (due, amount_minor, 'expense')
    if 'Club 6' in text:
        assert row['title'] == 'Club 6'


def test_bad_explicit_budget_add_stays_in_the_worksheet_handler():
    today = date(2026, 11, 20)
    assert worksheet.command('Add Club dues 35$ on a someday', today) == ('guide', [])
    assert worksheet.command('Add a workout to my calendar', today) == (None, [])
    assert worksheet.command('“Add Club dues $35 on November 29”?', today) == (None, [])
    with pytest.raises(ValueError, match='Choose a valid due date'):
        worksheet.command('Add Club dues $35 on November 31st', today)


def test_owner_auth_storage_and_stale_edits(f):
    f.registry['budget']['rift'] = str(f.tmp_path)
    cid = budget_api.ensure_conversation('alice', 'budget')
    f.app.include_router(api.router)
    current = f.client.get(PATH)
    assert current.status_code == 200 and current.headers['cache-control'] == 'no-store'
    assert current.json()['source'] == 'none'
    assert chat_service.open_bot_conversation('alice', 'budget')['conversation_id'] == cid
    assert f.client.get(PATH, headers={'x-user': 'bob'}).status_code == 409
    today = current.json()['today']
    payload = {'op': 'balance', 'revision': '', 'amount': '642.85', 'date': today}
    assert f.client.patch(PATH, json=payload).status_code == 403
    result = f.client.patch(PATH, json=payload, headers=ORIGIN)
    assert result.status_code == 200 and result.json()['current_minor'] == 64285
    assert f.client.patch(PATH, json=payload, headers=ORIGIN).status_code == 409
    result = f.client.patch(PATH, headers=ORIGIN, json={'op': 'save',
        'revision': result.json()['revision'], 'title': 'Fixture recurring utility',
        'date': today, 'amount': '32.69', 'repeat': 'weekly'})
    assert result.status_code == 200 and result.json()['chart'][0]['balance_minor'] == 61016
    assert f.client.get(PATH, headers={'x-user': 'bob'}).status_code == 409
    with worksheet.WorksheetStore(f.tasks).db() as db:
        raw = db.execute('SELECT data FROM budget_worksheets').fetchone()['data']
        assert all(value not in raw for value in ('64285', '3269', 'Fixture recurring utility'))
    assert next(row for row in chat_service.list_conversations('alice')
                if row['conversation_id'] == cid)['automation'] == 'budget-worksheet'


def test_bank_baseline_never_deducts_a_settled_bill_twice(b):
    b.setup()
    b.response['added'] = [b.tx()]
    b.store.sync('alice', b.store.get('alice')['revision'])
    cid = budget_api.ensure_conversation('alice', 'budget')
    conv = chat_service.get_conversation('alice', cid)
    store = worksheet.WorksheetStore(b.f.tasks)
    today = store.view('alice', conv)['today']
    store.mutate('alice', conv, [{'op': 'save', 'title': 'Fixture bill', 'date': today,
        'amount': '145.00', 'kind': 'expense', 'repeat': 'once'}])
    before = store.view('alice', conv)
    assert before['source'] == 'bank' and before['current_minor'] == 85500
    store.mutate('alice', conv, [{'op': 'settle', 'title': 'Fixture bill'}])
    after = store.view('alice', conv)
    assert after['current_minor'] == after['chart'][0]['balance_minor'] == 85500
    assert 'accounts' not in after and 'daily' not in after and 'PRIVATE-MERCHANT' not in json.dumps(after)
    b.store.disconnect('alice', b.store.get('alice')['revision'])
    assert store.view('alice', conv)['source'] == 'none'


def test_normal_chat_can_add_and_settle_without_a_provider_and_retries_are_safe(f):
    f.registry['budget']['rift'] = str(f.tmp_path)
    cid = chat_service.create_conversation('alice', title='Existing Budget Bot', bot_id='budget')['conversation_id']
    async def other(*args, **kwargs):
        raise AssertionError('Worksheet turn should not go to the provider.')
        yield {}
    chat = SimpleNamespace(stream_chat=other, get_conversation=chat_service.get_conversation,
        _task_store=lambda: f.tasks, _WORKSPACE_UNSET=chat_service._WORKSPACE_UNSET,
        _append_message=chat_service._append_message, _write=chat_service._write)
    budget_bank_chat.install(chat)

    async def send(text, request):
        return [frame async for frame in chat.stream_chat('alice', cid, text, request_id=request)]

    assert asyncio.run(send('Internet is $80 tomorrow every month and Payday is $2,200 this Friday', 'add'))[-1]['status'] == 'complete'
    assert asyncio.run(send('Mark Internet paid', 'paid'))[-1]['status'] == 'complete'
    assert asyncio.run(send('Mark Internet paid', 'paid'))[-1]['status'] == 'complete'
    assert 'Updated the amount' in asyncio.run(send('Change Internet to $90', 'edit'))[-1]['content']
    store = worksheet.WorksheetStore(f.tasks)
    plan = saved(store)
    bill = next(row for row in plan['rows'].values() if row['title'] == 'Internet')
    assert len(plan['rows']) == 2 and len(bill['settled']) == 1
    assert bill['amount_minor'] == 9000 and next(iter(bill['settled'].values()))['amount_minor'] == -8000
    conv = chat_service.get_conversation('alice', cid)
    assert [m['id'] for m in conv['messages']].count('turn-paid-user') == 1
    assert all(not m.get('budget_private') for m in conv['messages'])
    assert worksheet.command('What did I spend last month?', date.today()) == (None, [])


def test_existing_private_budget_chat_shares_the_worksheet_without_a_canonical_thread(b):
    b.setup()
    b.f.registry['budget']['rift'] = str(b.f.tmp_path)
    owner = 'alice'
    conv = chat_service.create_conversation(owner, title='Existing Budget Bot', bot_id='budget')
    cid = conv['conversation_id']
    assert cid != worksheet.budget_bank.cid(owner)

    b.f.app.include_router(api.router)
    initial = b.f.client.get(PATH)
    assert initial.status_code == 200
    assert initial.json()['rows'] == []
    assert next(row for row in chat_service.list_conversations(owner)
                if row['conversation_id'] == cid)['automation'] == 'budget-worksheet'
    assert b.f.client.get(PATH, headers={'x-user': 'bob'}).status_code == 409

    async def other(*args, **kwargs):
        raise AssertionError('Private worksheet turns should avoid generic chat.')
        yield {}

    def provider(*args, **kwargs):
        raise AssertionError('Private worksheet turns should avoid the model.')

    chat = SimpleNamespace(stream_chat=other, get_conversation=chat_service.get_conversation,
        _task_store=lambda: b.f.tasks, _WORKSPACE_UNSET=chat_service._WORKSPACE_UNSET,
        _append_message=chat_service._append_message, _write=chat_service._write,
        resolve_bot_for_user=chat_service.resolve_bot_for_user, get_provider=provider)
    budget_bank_chat.install(chat)

    async def send(text, request):
        return [frame async for frame in chat.stream_chat(owner, cid, text, request_id=request)]

    reply = asyncio.run(send('Show my budget worksheet', 'show'))[-1]
    assert reply['status'] == 'complete' and '30-day forecast' in reply['content']
    added = asyncio.run(send('“Add Workshop 18.50$ on Nov. 29th, 2026.', 'add'))[-1]
    assert added['status'] == 'complete' and 'Saved the row' in added['content']
    saved_row = b.f.client.get(PATH).json()['rows'][0]
    assert (saved_row['title'], saved_row['amount_minor'], saved_row['date'], saved_row['kind']) == (
        'Workshop', 1850, '2026-11-29', 'expense')
    guided = asyncio.run(send('Add Workshop 18.50$ with no date', 'missing-date'))[-1]
    assert guided['status'] == 'complete' and 'Include the item, amount, and due date' in guided['content']


def test_open_budget_from_developer_chat_uses_the_bound_bot_and_keeps_summary_private(b):
    b.setup()
    b.response['added'] = [b.tx()]
    b.store.sync('alice', b.store.get('alice')['revision'])
    b.f.registry['budget']['rift'] = str(b.f.tmp_path)
    b.f.registry['developer'] = {
        **b.f.registry['budget'], 'id': 'developer', 'name': 'Developer Bot',
        'policy': {'fs:read': 0},
    }
    owner = 'alice'
    source = chat_service.create_conversation(owner, title='Code chat', bot_id='developer')
    target = budget_api.ensure_conversation(owner, 'budget')

    async def other(*args, **kwargs):
        raise AssertionError('Opening a budget should never dispatch workspace work or call a model.')
        yield {}

    chat = SimpleNamespace(stream_chat=other, get_conversation=chat_service.get_conversation,
        open_bot_conversation=chat_service.open_bot_conversation, _task_store=lambda: b.f.tasks,
        _WORKSPACE_UNSET=chat_service._WORKSPACE_UNSET, BotUnavailable=chat_service.BotUnavailable,
        _append_message=chat_service._append_message, _write=chat_service._write)
    budget_bank_chat.install(chat)

    async def send():
        return [frame async for frame in chat.stream_chat(owner, source['conversation_id'],
            '“Open the budget.”', request_id='budget')]

    reply = asyncio.run(send())[-1]
    assert reply['status'] == 'complete'
    assert reply['budget_worksheet'] == {'conversation_id': target}
    assert '145.00' not in reply['content']
    original = chat_service.get_conversation(owner, source['conversation_id'])['messages'][-1]
    assert original['budget_worksheet'] == {'conversation_id': target}
    assert 'Bank budget' not in original['content']
    saved = chat_service.get_conversation(owner, target)['messages'][-1]
    assert saved.get('budget_private') is True
    assert '**Posted spending** USD 145.00' in saved['content']
    assert '**Estimated cash in selected checking and savings** USD 855.00' in saved['content']
    assert 'PRIVATE-MERCHANT' not in saved['content']
    previews = {row['conversation_id']: row['latest_update'] for row in chat_service.list_conversations(owner)}
    assert previews[target] == 'Budget summary ready.'
    assert '145.00' not in previews[source['conversation_id']]
    asyncio.run(send())
    assert len(chat_service.get_conversation(owner, target)['messages']) == 2


def test_open_budget_without_saved_data_completes_instead_of_dispatching(f):
    owner = 'bob'
    conv = chat_service.create_conversation(owner, title='Chat')
    chat = SimpleNamespace(_task_store=lambda: f.tasks, _append_message=chat_service._append_message,
        _write=chat_service._write, BotUnavailable=chat_service.BotUnavailable)
    reply = budget_bank_chat.open_budget(chat, owner, conv, 'Open the budget', 'budget', None)[-1]
    assert reply['status'] == 'complete' and 'no configured budget' in reply['content'].lower()
    assert 'budget_worksheet' not in reply
