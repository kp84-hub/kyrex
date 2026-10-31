"""Public-data paper trading: arithmetic, task fences, privacy, recovery and UI API."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from types import SimpleNamespace
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import bots
import bot_provider
import chat_service as chat
import crypto_paper as paper
import crypto_paper_api as api
import paper_market as market
import task_store
from task_store import CloudTaskStore, TaskWorker
import serve


@pytest.fixture
def f(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('WEB_SESSION_SECRET', 'paper-only-test-encryption')
    monkeypatch.setattr(task_store, 'DATA_DIR', tmp_path)
    tasks = CloudTaskStore()
    monkeypatch.setattr(task_store, 'CloudTaskStore', lambda *a, **k: tasks)
    monkeypatch.setattr(chat, '_task_store', lambda: tasks)
    rift = tmp_path / 'rift'; rift.mkdir()
    bot = {'id':'crypto','owner':'alice','name':'Crypto Bot','status':'running','policy':dict(paper.POLICY),
           'rift':str(rift),'model':'chosen-model','provider_profile_id':'chosen-profile'}
    registry = {'crypto': bot}
    monkeypatch.setattr(bots, 'load_bots', lambda: registry)
    resolved = {'provider':'openai','api_key':'paper-fixture-only','base_url':'https://provider.example/v1','headers':{},'model':'chosen-model'}
    monkeypatch.setattr(bot_provider, 'resolve_bot_provider', lambda *a: dict(resolved))
    store = paper.PaperStore(tasks)
    cfg = store.configure('alice', bot_id='crypto', interval_seconds=900, strategy=paper.STRATEGY, time_zone='UTC')
    return SimpleNamespace(**locals())


def quote(price='100', when=None):
    return {'source':'Kraken public ticker','retrieved_at':time.time() if when is None else when,
            'prices':{s:{'bid':str(price),'ask':str(Decimal(price) * Decimal('1.001'))} for s in market.PAIRS}}


def pick(side='buy', symbol='BTC', amount='25', reason='Public data supports this simulated decision.'):
    return {'action':side,'symbol':symbol,'notional_usd':amount if side!='hold' else '0','reason':reason}


def task(f, slot=None):
    cfg = f.store.settings('alice'); now = time.time()
    with f.store.db() as db: db.execute('UPDATE crypto_paper_settings SET next_tick=? WHERE owner=?', (now-1,'alice'))
    paper.submit_due(f.tasks, now)
    result = f.tasks.claim_next('paper-worker')
    assert result
    return result


def apply(f, job=None, choice=None, price='100'):
    job = job or task(f); cfg = f.store.settings('alice')
    before = quote(price)
    result = f.store.apply('alice', cfg, job, choice or pick(), before, quote(price), {'model':'chosen-model','provider_profile_id':'chosen-profile'})
    return result, job


def test_long_only_decimal_accounting_and_replay(f):
    before = f.store.state('alice')
    record, job = apply(f)
    state = f.store.state('alice'); fill = record['fill']
    assert fill['simulated'] and Decimal(fill['price_usd']) > Decimal('100.1')
    assert Decimal(state['cash']) + Decimal(fill['notional_usd']) + Decimal(fill['fee_usd']) == 1000
    assert Decimal(state['positions']['BTC']['quantity']) > 0
    assert Decimal(state['fees']) > 0 and Decimal(state['equity']) < 1000
    assert state['positions']['ETH']['quantity'] == '0'
    again = f.store.apply('alice', f.store.settings('alice'), job, pick(), quote(), quote(), {})
    assert again == record and f.store.state('alice') == state
    assert f.store.dashboard('bob')['tick_count'] == 0 and f.store.state('bob') == before
    assert b'Public data supports' not in f.tasks.db_path.read_bytes()
    assert b'chosen-model' not in f.tasks.db_path.read_bytes(), 'Ledger model identity is sealed.'


def test_sale_without_holdings_is_recorded_without_a_fill(f):
    record, _ = apply(f, choice=pick('sell'))
    assert record['fill'] is None and 'holdings' in record['outcome']
    assert f.store.state('alice')['cash'] == '1000'


@pytest.mark.parametrize('symbol,buy_price,context_price,execution_price', [
    ('ADA','0.195','0.1941','0.193714'),
    ('DOGE','0.0754','0.0752','0.0751'),
    ('BTC','77900','77500','77400'),
])
def test_cent_rounded_exit_caps_to_holdings_at_fresh_price_and_replays_once(
        f, symbol, buy_price, context_price, execution_price):
    bought, job = apply(f, choice=pick(symbol=symbol), price=buy_price)
    finish_paper_tick(f, job)
    before = f.store.state('alice')
    held = Decimal(before['positions'][symbol]['quantity'])
    context = quote(context_price); execution = quote(execution_price)
    estimate = (held * paper.fill_price(context['prices'][symbol], 'sell')).quantize(Decimal('.01'))
    choice = pick('sell', symbol=symbol, amount=str(estimate), reason='Exit the existing paper position.')
    assert estimate / paper.fill_price(execution['prices'][symbol], 'sell') > held
    job = task(f); cfg = f.store.settings('alice')
    sold = f.store.apply('alice', cfg, job, choice, context, execution, {})
    fill = sold['fill']; after = f.store.state('alice')
    assert sold['outcome'] == 'paper_fill' and sold['decision'] == choice
    assert Decimal(fill['quantity']) == held and 0 < Decimal(fill['notional_usd']) < estimate <= 25
    assert Decimal(fill['price_usd']) == paper.fill_price(execution['prices'][symbol], 'sell').quantize(paper.STEP)
    assert Decimal(after['positions'][symbol]['quantity']) == Decimal(after['positions'][symbol]['basis']) == 0
    assert Decimal(after['cash']) == Decimal(before['cash']) + Decimal(fill['notional_usd']) - Decimal(fill['fee_usd'])
    assert Decimal(after['fees']) == Decimal(bought['fill']['fee_usd']) + Decimal(fill['fee_usd'])
    assert Decimal(after['realized_pnl']) == Decimal(fill['notional_usd']) - Decimal(fill['fee_usd']) - Decimal(before['positions'][symbol]['basis'])
    assert f.store.apply('alice', cfg, job, choice, quote(execution_price), quote(execution_price), {}) == sold
    assert f.store.state('alice') == after and f.store.dashboard('alice')['tick_count'] == 2
    assert f.store.dashboard('bob')['tick_count'] == 0 and f.store.state('bob')['cash'] == '1000'


def test_exit_keeps_order_cap_and_closes_a_sub_dollar_remainder(f):
    _, job = apply(f)
    finish_paper_tick(f, job)
    _, job = apply(f)
    finish_paper_tick(f, job)
    before = f.store.state('alice')['positions']['BTC']
    sold, job = apply(f, choice=pick('sell'))
    remaining = f.store.state('alice')['positions']['BTC']
    assert Decimal(sold['fill']['notional_usd']) <= 25
    assert Decimal(sold['fill']['quantity']) < Decimal(before['quantity'])
    assert Decimal(remaining['quantity']) > 0 and Decimal(remaining['basis']) > 0
    finish_paper_tick(f, job)
    _, job = apply(f, choice=pick('sell', amount='24'))
    finish_paper_tick(f, job)
    remaining = f.store.state('alice')['positions']['BTC']
    assert 0 < Decimal(remaining['quantity']) * paper.fill_price(quote()['prices']['BTC'], 'sell') < 1
    sold, job = apply(f, choice=pick('sell', amount='1'))
    assert 0 < Decimal(sold['fill']['notional_usd']) < 1
    assert Decimal(sold['fill']['quantity']) == Decimal(remaining['quantity'])
    position = f.store.state('alice')['positions']['BTC']
    assert Decimal(position['quantity']) == Decimal(position['basis']) == 0
    finish_paper_tick(f, job)
    empty, _ = apply(f, choice=pick('sell'))
    assert empty['fill'] is None and 'holdings' in empty['outcome']


def test_zero_notional_dust_never_records_a_fill(f):
    import connectors
    state = paper.initial_state()
    state['positions']['ADA'] = {'quantity':'0.000000000001','basis':'0.00000001'}
    with f.store.db() as db:
        db.execute('INSERT INTO crypto_paper_state VALUES (?,?)', ('alice', connectors.seal_tokens(state)))
    record, _ = apply(f, choice=pick('sell', symbol='ADA'), price='0.2')
    assert record['fill'] is None and 'precision' in record['outcome']
    assert f.store.state('alice')['positions'] == state['positions']
    assert f.store.state('alice')['cash'] == '1000'


@pytest.mark.parametrize('change', [{'action':'withdraw'}, {'symbol':'NOTACOIN'}, {'notional_usd':'NaN'},
    {'notional_usd':'-1'}, {'notional_usd':'26'}, {'notional_usd':25}, {'leverage':2}, {'reason':''},
    {'action':'hold','notional_usd':'1'}, {'price':'1'}])
def test_model_cannot_change_supported_operations_or_limits(f, change):
    with pytest.raises(ValueError): paper.decision({**pick(), **change})
    assert f.store.dashboard('alice')['tick_count'] == 0


@pytest.mark.parametrize('fault', ['pause','revision','cancel','claim','owner','bot','executor','payload'])
def test_atomic_commit_rechecks_claim_owner_and_live_settings(f, fault):
    job = task(f); cfg = f.store.settings('alice')
    if fault=='pause': f.store.pause('alice')
    if fault=='revision': f.store.configure('alice',bot_id='crypto',interval_seconds=900,strategy='Hold cash.',time_zone='UTC')
    if fault=='cancel': f.tasks.request_cancel(job['task_id'])
    if fault=='claim': f.tasks.set_status(job['task_id'],'running',run_id='replacement')
    if fault=='owner': f.tasks.set_status(job['task_id'],'running',chat_id='bob')
    if fault=='bot': f.bot['policy']['crypto:paper']='deny'
    if fault=='executor': f.tasks.set_status(job['task_id'],'running',executor_prefix='repo')
    if fault=='payload': f.tasks.set_status(job['task_id'],'running',task_text='changed')
    with pytest.raises(ValueError): f.store.apply('alice',cfg,job,pick(),quote(),quote(),{})
    assert f.store.state('alice')['cash']=='1000' and f.store.dashboard('alice')['tick_count']==0


def test_duplicate_schedule_and_cross_thread_finalize_only_fill_once(f):
    with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(lambda _: paper.submit_due(f.tasks), range(4)))
    assert len(f.tasks.list_tasks())==1
    job=f.tasks.claim_next('worker')
    def finalize(_): return f.store.apply('alice',f.store.settings('alice'),job,pick(),quote(),quote(),{})
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(finalize,range(4)))
    assert all(r==results[0] for r in results) and f.store.dashboard('alice')['tick_count']==1


def test_stale_or_crossed_quotes_cannot_fill(f):
    job=task(f); cfg=f.store.settings('alice')
    for bad in (quote(when=time.time()-61), quote(when=time.time()+60), {**quote(),'prices':{}}):
        with pytest.raises(ValueError): f.store.apply('alice',cfg,job,pick(),quote(),bad,{})
    bad=quote(); bad['prices']['BTC']['ask']='99'
    with pytest.raises(ValueError): f.store.apply('alice',cfg,job,pick(),quote(),bad,{})
    assert f.store.dashboard('alice')['tick_count']==0


def test_position_turnover_and_loss_guard(f):
    for index in range(10):
        job=task(f); apply(f,job,pick(symbol='BTC' if index<5 else 'ETH'))
        f.tasks.set_status(job['task_id'],'done')
        # Advance only the schedule clock to a new unique tick, never replay a fill.
        with f.store.db() as db: db.execute('UPDATE crypto_paper_settings SET revision=? WHERE owner=?', (__import__('uuid').uuid4().hex,'alice'))
    history=f.store.dashboard('alice')['ticks']
    assert any('Position limit' in r['outcome'] for r in history)
    state=f.store.state('alice')
    assert all(Decimal(p['quantity'])*100<=100 for p in state['positions'].values())
    job=task(f); record,_=apply(f,job,pick('hold'),price='1')
    assert record['outcome']=='loss_guard' and not f.store.settings('alice')['enabled']
    assert f.store.state('alice')['auto_halted']
    with pytest.raises(ValueError): f.store.configure('alice',bot_id='crypto',interval_seconds=900,strategy='Restart',time_zone='UTC')


def test_daily_publication_recovers_without_duplicate_fill_or_thread(f, monkeypatch):
    _,job=apply(f)
    state=f.store.state('alice');state['last_report_day']='2000-01-01'
    import connectors
    with f.store.db() as db: db.execute('UPDATE crypto_paper_state SET data=? WHERE owner=?',(connectors.seal_tokens(state),'alice'))
    f.tasks.set_status(job['task_id'],'done')
    with f.store.db() as db: db.execute('UPDATE crypto_paper_settings SET revision=? WHERE owner=?',(__import__('uuid').uuid4().hex,'alice'))
    apply(f,choice=pick('hold'))
    from chat_notifications import NotificationStore
    notices=NotificationStore(f.tmp_path/'notices.db')
    monkeypatch.setattr('chat_notifications.NotificationStore',lambda:notices)
    f.store.publish_reports('alice');f.store.publish_reports('alice')
    conv=chat.get_conversation('alice',f.cfg['conversation_id'])
    assert len([m for m in conv['messages'] if m['id'].startswith('crypto-paper-daily-')])==1
    assert len(notices.list('alice')['notifications'])==1
    assert len([c for c in chat.list_conversations('alice') if c.get('bot_id')=='crypto'])==1
    assert f.store.dashboard('alice')['tick_count']==2


def test_worker_uses_one_bound_model_and_only_public_inputs(f,monkeypatch):
    calls=[]
    class Provider:
        async def chat(self,**kw): calls.append(kw);return {'content':json.dumps(pick())}
    import kyrex.providers
    monkeypatch.setattr(kyrex.providers,'get_provider',lambda *a,**k:Provider())
    monkeypatch.setattr(market,'context',lambda *a:quote())
    monkeypatch.setattr(market,'quotes',lambda *a:quote())
    paper.submit_due(f.tasks)
    assert TaskWorker(f.tasks,executor=serve.run_task).claim_and_execute_once()
    job=f.tasks.list_tasks()[0]
    assert job['status']=='done',job.get('error')
    assert len(calls)==1 and calls[0]['model']=='chosen-model' and calls[0]['tools'] is None
    payload=json.loads(calls[0]['messages'][1]['content'])
    assert set(payload)=={'market','paper_portfolio'}
    assert f.store.dashboard('alice')['tick_count']==1


def test_model_failure_or_changed_model_never_trades(f,monkeypatch):
    import kyrex.providers
    class Provider:
        async def chat(self,**kw):
            f.resolved['model']='changed-model'
            return {'content':json.dumps(pick())}
    monkeypatch.setattr(kyrex.providers,'get_provider',lambda *a,**k:Provider())
    with pytest.raises(ValueError): asyncio.run(paper.choose('alice',f.bot,f.cfg,quote(),paper.metrics(f.store.state('alice'))))
    assert f.store.dashboard('alice')['tick_count']==0


def test_host_read_permission_does_not_grant_other_connectors(f):
    from bot_capabilities import derive_bot_capabilities
    caps=derive_bot_capabilities(paper.POLICY)
    assert 'crypto_read' in caps['tools']
    assert not {'budget_read','fitness_read','github_read','delegate_task','mcp_read'} & set(caps['tools'])
    session=object.__new__(chat.EngineSession);session.crypto_owner='alice';session.bot_id='crypto';session.allowed_tools={'crypto_read'}
    ok, result = session._handle_crypto_read({})
    assert ok and 'active_preset' not in result
    assert result['experiment']['candle_interval_minutes'] == 60
    assert result['experiment']['strategy'] == paper.STRATEGY
    f.bot['policy']['crypto:read']='deny'
    assert not session._handle_crypto_read({})[0]


def test_api_owner_auth_csrf_strict_paper_only_and_pause(f,monkeypatch):
    import connections_api
    def user(request):
        from fastapi import HTTPException
        if request.headers.get('x-user') not in {'alice','bob'}: raise HTTPException(401,'Sign in')
        return request.headers['x-user']
    monkeypatch.setattr(connections_api,'_require_user',user)
    app=FastAPI();app.include_router(api.router);client=TestClient(app)
    assert client.get('/api/automations/crypto-paper').status_code==401
    headers={'x-user':'alice'}
    base='/api/automations/crypto-paper'
    assert client.get(base,headers={'x-user':'bob'}).json()['configured'] is False
    body={'bot_id':'crypto','interval_seconds':900,'strategy':'Hold cash.','time_zone':'UTC'}
    assert client.put(base,headers={**headers,'origin':'https://foreign.example'},json=body).status_code==403
    assert client.put(base,headers=headers,json={**body,'live':True}).status_code==422
    assert client.put(base,headers=headers,json={**body,'owner':'bob'}).status_code==422
    assert client.put(base,headers={'x-user':'bob'},json=body).status_code==409
    assert client.put(base,headers=headers,json={**body,'symbols':[]}).status_code==422
    assert client.put(base,headers=headers,json={**body,'symbols':['BTC','BTC']}).status_code==409
    assert client.put(base,headers=headers,json={**body,'symbols':['NOTACOIN']}).status_code==409
    result = client.put(base,headers=headers,json={**body,'symbols':['SOL','BTC']})
    assert result.status_code==200 and result.json()['settings']['symbols']==['BTC','SOL']
    f.bot['status']='paused'
    assert client.post(base+'/pause',headers=headers,json={}).status_code==200
    assert not f.store.settings('alice')['enabled']
    assert client.post(base+'/orders',headers=headers,json={'side':'buy'}).status_code==404


def test_daily_buy_cap_is_gross_not_net_and_sales_do_not_restore_it(f):
    for side,amount in [('buy','25'),('sell','24')]*10:
        job=task(f);apply(f,job,pick(side,amount=amount))
        f.tasks.set_status(job['task_id'],'done')
        with f.store.db() as db: db.execute('UPDATE crypto_paper_settings SET revision=? WHERE owner=?',(__import__('uuid').uuid4().hex,'alice'))
    rows=f.store.dashboard('alice')['ticks']
    buys=sum((Decimal(row['fill']['notional_usd']) for row in rows if row['fill'] and row['fill']['side']=='buy'),Decimal('0'))
    turnover=sum((Decimal(row['fill']['notional_usd']) for row in rows if row['fill']),Decimal('0'))
    assert buys<=200 and turnover>200 and any('buying limit' in row['outcome'] for row in rows)
    assert Decimal(f.store.activity('alice')['buy_remaining_usd']) == 0
    assert Decimal(f.store.state('alice')['realized_pnl'])<0, 'Round trips at a fixed market lose costs.'
    assert all(Decimal(p['quantity'])>=0 and Decimal(p['basis'])>=0 for p in f.store.state('alice')['positions'].values())


def test_paged_history_keeps_all_saved_decisions(f):
    import connectors
    with f.store.db() as db:
        for i in range(115):
            db.execute('INSERT INTO crypto_paper_ticks VALUES (?,?,?,?,?)',('alice',str(i),i,i,connectors.seal_tokens({'marker':i,'metrics':{'realized_pnl_usd':'0'}})))
    first=f.store.dashboard('alice');second=f.store.dashboard('alice',first['next_offset']);third=f.store.dashboard('alice',second['next_offset'])
    rows=first['ticks']+second['ticks']+third['ticks']
    assert len(rows)==115 and len({r['task_id'] for r in rows})==115 and third['next_offset'] is None
    assert first['tick_count']==115 and first['history_truncated']
    assert f.store.dashboard('bob')['ticks']==[]


def test_two_processes_finalize_one_atomic_paper_fill(f):
    import multiprocessing
    ctx=multiprocessing.get_context('fork'); gate=ctx.Barrier(2); output=ctx.Queue();job=task(f)
    def commit():
        local=paper.PaperStore(CloudTaskStore(f.tasks.db_path))
        gate.wait(8)
        try:
            local.apply('alice',f.cfg,job,pick(),quote(),quote(),{})
            output.put('ok')
        except Exception as exc: output.put(type(exc).__name__)
    processes=[ctx.Process(target=commit) for _ in range(2)]
    try:
        for process in processes: process.start()
        assert [output.get(timeout=10) for _ in processes]==['ok','ok']
        for process in processes: process.join(5);assert process.exitcode==0
        assert f.store.dashboard('alice')['tick_count']==1
    finally:
        for process in processes:
            if process.is_alive():process.terminate();process.join(5)


def test_public_market_adapter_has_only_get_endpoints_and_closed_candles(monkeypatch):
    calls=[]; now=int(time.time()//3600)*3600
    def public(endpoint,params):
        calls.append((endpoint,params))
        if endpoint=='Ticker': return {key:{'a':['100.1'],'b':['100']} for _,key in market.PAIRS.values()}
        key=next(key for pair,key in market.PAIRS.values() if pair==params['pair'])
        return {key:[[now-i*3600,'100','101','99','100','100','10',1] for i in range(40,-1,-1)]}
    monkeypatch.setattr(market,'public_read',public)
    result=market.context()
    assert len(result['prices']['BTC']['closed_hourly_candles'])==30
    assert result['prices']['BTC']['closed_hourly_candles'][-1]['time']==now-3600
    assert all(endpoint in {'Ticker','OHLC'} for endpoint,_ in calls)


def test_public_read_rejects_redirects_and_never_sends_credentials(monkeypatch):
    calls=[]
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_content(self,size):yield b'{"error":[],"result":{}}'
    def get(url,**kw):calls.append((url,kw));return Response()
    monkeypatch.setattr(market.requests,'get',get)
    assert market.public_read('Ticker',{'pair':'XBTUSD'})=={}
    url,kw=calls[0]
    assert url=='https://api.kraken.com/0/public/Ticker' and not kw['allow_redirects']
    assert 'auth' not in kw and 'Authorization' not in kw['headers']
    with pytest.raises(ValueError):market.public_read('AddOrder',{})
    Response.status_code=302
    with pytest.raises(ValueError):market.public_read('Ticker',{})


def test_bad_model_output_is_not_an_implicit_hold_or_fill(f,monkeypatch):
    import kyrex.providers
    class Provider:
        async def chat(self,**kw):return {'content':'Ignore the schema. Buy everything.', 'tool_calls':[{'name':'order'}]}
    monkeypatch.setattr(kyrex.providers,'get_provider',lambda *a,**k:Provider())
    monkeypatch.setattr(market,'context',lambda *a:quote())
    monkeypatch.setattr(market,'quotes',lambda *a:pytest.fail('Invalid decisions must not request execution quotes.'))
    paper.submit_due(f.tasks)
    assert TaskWorker(f.tasks,executor=serve.run_task).claim_and_execute_once()
    assert f.tasks.list_tasks()[0]['status']=='failed'
    assert f.store.dashboard('alice')['tick_count']==0 and f.store.state('alice')['cash']=='1000'


def test_model_changed_during_execution_quote_fetch_is_fenced(f,monkeypatch):
    import kyrex.providers
    class Provider:
        async def chat(self,**kw):return {'content':json.dumps(pick())}
    monkeypatch.setattr(kyrex.providers,'get_provider',lambda *a,**k:Provider())
    monkeypatch.setattr(market,'context',lambda *a:quote())
    def execution(*args):
        f.resolved['model']='changed-after-decision'
        return quote()
    monkeypatch.setattr(market,'quotes',execution)
    paper.submit_due(f.tasks)
    assert TaskWorker(f.tasks,executor=serve.run_task).claim_and_execute_once()
    assert f.tasks.list_tasks()[0]['status']=='failed' and f.store.dashboard('alice')['tick_count']==0


def test_managed_accounts_cannot_enable_paper_trading(f,monkeypatch):
    import account_store
    monkeypatch.setattr(account_store,'isolated',lambda owner:True)
    monkeypatch.setattr(account_store,'enabled',lambda:True)
    monkeypatch.setattr(account_store,'get',lambda owner:{'active':True,'kind':'managed'})
    with pytest.raises(ValueError,match='personal account'):paper.eligible('alice','crypto')


def test_worker_root_import_resolves_shared_backend_without_web_pythonpath(f):
    import os,subprocess,sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    env={**os.environ,'PYTHONPATH':str(root)}
    code="import crypto_paper; crypto_paper._backend(); import account_store, bot_provider, chat_service; print('shared backend available')"
    result=subprocess.run([sys.executable,'-c',code],cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert 'shared backend available' in result.stdout


@pytest.mark.parametrize('selected', [[], ['BTC','BTC'], ['UNKNOWN'], ['btc'], 'BTC', [None]])
def test_watchlist_rejects_invalid_values_without_changing_settings(f, selected):
    previous = f.store.settings('alice')
    with pytest.raises(ValueError):
        f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy='Wait.', time_zone='UTC', symbols=selected)
    assert f.store.settings('alice') == previous


def test_legacy_portfolio_and_benchmark_survive_expansion(f):
    import connectors
    _, first = apply(f)
    f.tasks.set_status(first['task_id'], 'done')
    state = f.store.state('alice')
    state['positions'] = {s:p for s,p in state['positions'].items() if s in market.LEGACY_SYMBOLS}
    with f.store.db() as db:
        cfg = f.store.settings('alice')
        data = {k:cfg[k] for k in ('bot_id','conversation_id','interval_seconds','strategy','time_zone')}
        data['strategy'] = paper.LEGACY_STRATEGY
        db.execute('UPDATE crypto_paper_settings SET data=? WHERE owner=?', (connectors.seal_tokens(data),'alice'))
        db.execute('UPDATE crypto_paper_state SET data=? WHERE owner=?', (connectors.seal_tokens(state),'alice'))
    assert f.store.settings('alice')['symbols'] == ['BTC','ETH'], 'No silent expansion of an enabled legacy experiment.'
    assert f.store.settings('alice')['candle_interval_minutes'] == 60
    assert f.store.settings('alice')['strategy'] == paper.STRATEGY
    assert f.store.state('alice')['cash'] == state['cash']
    assert f.store.state('alice')['positions']['SOL'] == {'quantity':'0','basis':'0'}
    f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy='Compare selected coins.', time_zone='UTC', symbols=list(market.PAIRS))
    record, _ = apply(f, choice=pick(symbol='SOL'))
    after = f.store.state('alice')
    assert record['fill']['symbol'] == 'SOL'
    assert after['benchmark'] == state['benchmark'] and set(after['benchmark']['positions']) == {'BTC','ETH'}
    assert after['positions']['BTC'] == state['positions']['BTC']
    assert f.store.dashboard('alice')['tick_count'] == 2
    assert f.store.dashboard('bob')['configured'] is False


def test_removed_holding_is_valued_and_sellable_but_cannot_be_bought(f):
    _, first = apply(f, choice=pick(symbol='SOL'))
    f.tasks.set_status(first['task_id'], 'done')
    cfg = f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy='Watch BTC only.', time_zone='UTC', symbols=['BTC'])
    selected = paper.market_symbols(cfg, f.store.state('alice'))
    assert selected == ['BTC','ETH','SOL'], 'Benchmark and held assets remain quoted.'
    def snapshot():
        data = quote(); data['prices'] = {s:data['prices'][s] for s in selected}; return data
    job = task(f)
    with pytest.raises(ValueError, match='watchlist'):
        f.store.apply('alice', cfg, job, pick(symbol='SOL'), snapshot(), snapshot(), {})
    with pytest.raises(ValueError, match='watchlist'):
        f.store.apply('alice', cfg, job, pick(symbol='ETH'), snapshot(), snapshot(), {})
    before = Decimal(f.store.state('alice')['positions']['SOL']['quantity'])
    record = f.store.apply('alice', cfg, job, pick('sell', symbol='SOL', amount='10'), snapshot(), snapshot(), {})
    assert record['fill']['side'] == 'sell'
    assert Decimal(f.store.state('alice')['positions']['SOL']['quantity']) < before


def test_worker_compares_eight_coins_in_one_call_and_buys_selected_altcoin(f, monkeypatch):
    calls=[]; requested=[]
    class Provider:
        async def chat(self, **kw): calls.append(kw); return {'content':json.dumps(pick(symbol='AVAX'))}
    import kyrex.providers
    monkeypatch.setattr(kyrex.providers, 'get_provider', lambda *a, **k:Provider())
    def snapshot(selected, *args): requested.append(selected); return quote()
    monkeypatch.setattr(market, 'context', snapshot)
    monkeypatch.setattr(market, 'quotes', snapshot)
    paper.submit_due(f.tasks)
    assert TaskWorker(f.tasks, executor=serve.run_task).claim_and_execute_once()
    assert f.tasks.list_tasks()[0]['status'] == 'done'
    assert len(calls) == 1 and requested == [list(market.PAIRS), list(market.PAIRS)]
    payload = json.loads(calls[0]['messages'][1]['content'])
    assert set(payload['market']['prices']) == set(market.PAIRS)
    assert 'Selected coins: BTC, ETH, SOL, XRP, ADA, DOGE, LINK, AVAX.' in calls[0]['messages'][0]['content']
    assert f.store.dashboard('alice')['ticks'][0]['fill']['symbol'] == 'AVAX'


def test_missing_selected_coin_prevents_a_fill(f):
    job = task(f); before = quote(); missing = quote(); del missing['prices']['AVAX']
    with pytest.raises(ValueError, match='complete'):
        f.store.apply('alice', f.cfg, job, pick(symbol='SOL'), before, missing, {})
    assert f.store.dashboard('alice')['tick_count'] == 0
    assert f.store.state('alice')['cash'] == '1000'


def test_market_reads_only_selected_pairs_and_checks_all_candles(monkeypatch):
    calls=[]; now=int(time.time()//3600)*3600
    def public(endpoint, params):
        calls.append((endpoint, params))
        if endpoint == 'Ticker':
            return {market.PAIRS[s][1]:{'a':['100.1'],'b':['100']} for s in ('SOL','DOGE')}
        pair, key = next(v for v in market.PAIRS.values() if v[0] == params['pair'])
        return {key:[[now-i*3600,'100','101','99','100','100','10',1] for i in range(30, -1, -1)]}
    monkeypatch.setattr(market, 'public_read', public)
    result = market.context(['DOGE','SOL'])
    assert set(result['prices']) == {'SOL','DOGE'}
    assert all(len(row['closed_hourly_candles']) == 30 for row in result['prices'].values())
    assert len(calls) == 3 and calls[-1] == ('Ticker', {'pair':'SOLUSD,XDGUSD'})


@pytest.mark.parametrize('interval', [1, 15, 0, '5', True, 5.0])
def test_invalid_candle_intervals_cannot_change_running_settings(f, interval):
    previous = f.store.settings('alice')
    with pytest.raises(ValueError, match='candles'):
        f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy=paper.ACTIVE_STRATEGY,
                          time_zone='UTC', candle_interval_minutes=interval)
    assert f.store.settings('alice') == previous


def test_active_settings_keep_history_limits_and_fence_old_tasks(f):
    _, first = apply(f)
    f.tasks.set_status(first['task_id'], 'done')
    f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy=paper.STRATEGY, time_zone='UTC')
    pending = task(f); old_cfg = f.store.settings('alice'); before = f.store.state('alice')
    cfg = f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy=paper.ACTIVE_STRATEGY,
                            time_zone='UTC', candle_interval_minutes=5)
    assert cfg['candle_interval_minutes'] == 5 and cfg['interval_seconds'] == 900
    assert cfg['strategy'] == paper.ACTIVE_STRATEGY and cfg['revision'] != old_cfg['revision']
    assert f.store.state('alice') == before and f.store.dashboard('alice')['tick_count'] == 1
    assert f.store.dashboard('alice')['active_preset'] == {'strategy':paper.ACTIVE_STRATEGY, 'candle_interval_minutes':5}
    with pytest.raises(ValueError, match='changed'):
        f.store.apply('alice', old_cfg, pending, pick(), quote(), quote(), {})
    assert f.store.dashboard('alice')['limits']['max_order_usd'] == '25'
    assert f.store.dashboard('alice')['limits']['pause_loss_usd'] == '50'
    assert f.store.settings('bob')['candle_interval_minutes'] == 60


def test_five_minute_candles_and_signals_exclude_open_bar(monkeypatch):
    now = int(time.time()//300)*300; requests=[]
    def public(endpoint, params):
        requests.append((endpoint, params))
        if endpoint == 'Ticker': return {'SOLUSD':{'a':['131.1'],'b':['131']}}
        rows=[]
        for index in range(31):
            close = 100 + index
            rows.append([now-(30-index)*300, str(close), str(close), str(close-1), str(close), str(close), '1', 1])
        # The still-open candle must not affect any signal.
        rows[-1][1:6] = ['999','1000','998','999','999']
        return {'SOLUSD':rows}
    monkeypatch.setattr(market, 'public_read', public)
    result = market.context(['SOL'], 5); row=result['prices']['SOL']
    assert result['candle_interval_minutes'] == 5 and 'closed_hourly_candles' not in row
    bars=row['closed_five_minute_candles']
    assert len(bars) == 30 and bars[-1]['time'] == now-300 and bars[-1]['close'] == '129'
    assert row['signals']['fast_sma_5'] == '127'
    assert row['signals']['slow_sma_12'] == '123.5'
    assert row['signals']['last_3_closes_rising'] and row['signals']['above_prior_6_bar_high']
    assert Decimal(row['signals']['change_last_3_bars_pct']) == (Decimal(129)/126-1)*100
    assert requests[0] == ('OHLC', {'pair':'SOLUSD','interval':5})


@pytest.mark.parametrize('fault', ['stale', 'gap', 'invalid_high'])
def test_active_market_data_rejects_bad_closed_bars(monkeypatch, fault):
    now = int(time.time()//300)*300
    rows=[[now-i*300,'100','101','99','100','100','1',1] for i in range(31,0,-1)]
    if fault == 'stale':
        for row in rows: row[0] -= 3600
    elif fault == 'gap': rows[-2][0] -= 1
    else: rows[-1][2] = '99'
    def public(endpoint, params):
        assert endpoint == 'OHLC', 'Bad candles must not proceed to ticker or inference.'
        return {'SOLUSD':rows}
    monkeypatch.setattr(market, 'public_read', public)
    with pytest.raises(ValueError): market.context(['SOL'], 5)


def test_active_worker_uses_saved_timeframe_one_model_call_and_records_it(f, monkeypatch):
    f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy=paper.ACTIVE_STRATEGY,
                      time_zone='UTC', candle_interval_minutes=5)
    calls=[]; intervals=[]
    class Provider:
        async def chat(self, **kw): calls.append(kw); return {'content':json.dumps(pick(symbol='SOL', amount='10'))}
    import kyrex.providers
    monkeypatch.setattr(kyrex.providers, 'get_provider', lambda *a, **k:Provider())
    def snapshot(selected, interval): intervals.append(interval); return quote()
    monkeypatch.setattr(market, 'context', snapshot)
    monkeypatch.setattr(market, 'quotes', lambda *a:quote())
    paper.submit_due(f.tasks)
    assert TaskWorker(f.tasks, executor=serve.run_task).claim_and_execute_once()
    assert f.tasks.list_tasks()[0]['status'] == 'done'
    assert len(calls) == 1 and intervals == [5]
    prompt=calls[0]['messages'][0]['content']
    assert 'Candle duration is 5 minutes' in prompt and '1% round trip BEFORE spread' in prompt
    assert paper.ACTIVE_STRATEGY in prompt and calls[0]['tools'] is None
    tick=f.store.dashboard('alice')['ticks'][0]
    assert tick['candle_interval_minutes'] == 5 and Decimal(tick['fill']['notional_usd']) <= 10


def test_worker_executes_a_cent_rounded_exit_with_one_model_call(f, monkeypatch):
    _, job = apply(f, choice=pick(symbol='ADA'), price='0.195')
    finish_paper_tick(f, job)
    calls=[]
    choice=pick('sell', symbol='ADA', amount='24.79', reason='Sell the existing paper position.')
    class Provider:
        async def chat(self, **kw): calls.append(kw); return {'content':json.dumps(choice)}
    import kyrex.providers
    monkeypatch.setattr(kyrex.providers, 'get_provider', lambda *a, **k:Provider())
    monkeypatch.setattr(market, 'context', lambda *a:quote('0.1941'))
    monkeypatch.setattr(market, 'quotes', lambda *a:quote('0.193714'))
    paper.submit_due(f.tasks)
    assert TaskWorker(f.tasks, executor=serve.run_task).claim_and_execute_once()
    assert all(job['status'] == 'done' for job in f.tasks.list_tasks())
    assert len(calls) == 1 and calls[0]['tools'] is None
    assert 'Sell notional_usd is a maximum before fees' in calls[0]['messages'][0]['content']
    tick=f.store.dashboard('alice')['ticks'][0]
    assert tick['decision'] == choice and tick['fill']['side'] == 'sell'
    assert Decimal(tick['fill']['notional_usd']) < Decimal('24.79')
    assert Decimal(f.store.state('alice')['positions']['ADA']['quantity']) == 0


def test_active_api_is_strict_owner_scoped_and_does_not_enable_by_reading(f, monkeypatch):
    import connections_api
    monkeypatch.setattr(connections_api, '_require_user', lambda request:request.headers['x-user'])
    app=FastAPI(); app.include_router(api.router); client=TestClient(app)
    body={'bot_id':'crypto','interval_seconds':900,'strategy':paper.ACTIVE_STRATEGY,'time_zone':'UTC','candle_interval_minutes':5}
    headers={'x-user':'alice'}
    assert client.get('/api/automations/crypto-paper', headers={'x-user':'bob'}).json()['configured'] is False
    assert client.put('/api/automations/crypto-paper', headers={'x-user':'bob'}, json=body).status_code == 409
    assert client.put('/api/automations/crypto-paper', headers=headers, json={**body,'candle_interval_minutes':'5'}).status_code == 422
    assert client.put('/api/automations/crypto-paper', headers=headers, json={**body,'candle_interval_minutes':1}).status_code == 409
    response=client.put('/api/automations/crypto-paper', headers=headers, json=body)
    assert response.status_code == 200 and response.json()['settings']['candle_interval_minutes'] == 5
    assert f.store.dashboard('alice')['tick_count'] == 0, 'Saving is configuration, not an immediate synthetic fill.'


def finish_paper_tick(f, job):
    f.tasks.set_status(job['task_id'], 'done')
    with f.store.db() as db:
        db.execute('UPDATE crypto_paper_settings SET revision=?,next_tick=0 WHERE owner=?', (__import__('uuid').uuid4().hex, 'alice'))


def test_existing_holdings_can_sell_after_buy_budget_is_exhausted(f):
    for symbol in market.PAIRS:
        record, job = apply(f, choice=pick(symbol=symbol))
        assert record['fill'] and record['fill']['side'] == 'buy'
        finish_paper_tick(f, job)
    assert f.store.activity('alice')['buys_usd'] == '200.00000000'
    assert Decimal(f.store.activity('alice')['buy_remaining_usd']) == 0
    before = f.store.state('alice')
    blocked, job = apply(f, choice=pick(amount='1'))
    assert blocked['fill'] is None and 'buying limit' in blocked['outcome']
    assert blocked['daily_activity']['buys_usd'] == '200.00000000'
    finish_paper_tick(f, job)
    sold, job = apply(f, choice=pick('sell', amount='10'))
    after = f.store.state('alice')
    assert sold['fill']['side'] == 'sell' and sold['fill']['simulated']
    assert Decimal(after['positions']['BTC']['quantity']) < Decimal(before['positions']['BTC']['quantity'])
    assert Decimal(after['cash']) > Decimal(before['cash'])
    assert sold['daily_activity'] == f.store.activity('alice')
    assert Decimal(sold['daily_activity']['turnover_usd']) == 200 + Decimal(sold['fill']['notional_usd'])
    assert Decimal(sold['daily_activity']['buy_remaining_usd']) == 0
    again = f.store.apply('alice', f.store.settings('alice'), job, pick('sell', amount='10'), quote(), quote(), {})
    assert again == sold and f.store.state('alice') == after, 'Replays cannot repeat a sale or its daily volume.'
    finish_paper_tick(f, job)
    closed, _ = apply(f, choice=pick('sell', amount='25'))
    assert Decimal(closed['fill']['quantity']) == Decimal(after['positions']['BTC']['quantity'])
    assert Decimal(f.store.state('alice')['positions']['BTC']['quantity']) == 0
    assert Decimal(f.store.activity('alice')['buy_remaining_usd']) == 0
    assert Decimal(f.store.activity('bob')['buys_usd']) == 0


@pytest.mark.parametrize('local_day,hours', [('2026-03-08',23),('2026-11-01',25)])
def test_local_midnight_budget_windows_include_dst_and_legacy_fills(f, local_day, hours):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    import connectors
    zone=ZoneInfo('America/New_York')
    start=datetime.fromisoformat(local_day).replace(tzinfo=zone)
    end=start + timedelta(days=1)
    assert end.timestamp() - start.timestamp() == hours*3600
    def seed(owner, identity, when, side, amount):
        with f.store.db() as db:
            db.execute('INSERT INTO crypto_paper_ticks VALUES (?,?,?,?,?)', (owner,identity,0,when,
                connectors.seal_tokens({'fill':{'side':side,'notional_usd':amount},'legacy':True})))
    seed('alice','yesterday',start.timestamp()-1,'buy','100')
    seed('alice','at-midnight',start.timestamp(),'buy','150')
    seed('alice','sale',start.timestamp()+1,'sell','50')
    seed('alice','tomorrow',end.timestamp(),'buy','100')
    seed('bob','other-owner',start.timestamp()+1,'buy','199')
    current=f.store.activity('alice',{'time_zone':str(zone)},now=start.timestamp()+3600)
    assert current == {'day':local_day,'time_zone':str(zone),'buys_usd':'150.00000000',
        'sells_usd':'50.00000000','turnover_usd':'200.00000000','buy_remaining_usd':'50.00000000'}
    tomorrow=f.store.activity('alice',{'time_zone':str(zone)},now=end.timestamp())
    assert tomorrow['buys_usd'] == '100.00000000' and Decimal(tomorrow['sells_usd']) == 0


def test_model_receives_daily_buy_capacity_and_exit_rules(f, monkeypatch):
    _, job = apply(f)
    finish_paper_tick(f, job)
    calls=[]
    class Provider:
        async def chat(self, **kw): calls.append(kw); return {'content':json.dumps(pick('hold'))}
    import kyrex.providers
    monkeypatch.setattr(kyrex.providers, 'get_provider', lambda *a, **k:Provider())
    monkeypatch.setattr(market, 'context', lambda *a:quote())
    monkeypatch.setattr(market, 'quotes', lambda *a:quote())
    paper.submit_due(f.tasks)
    assert TaskWorker(f.tasks, executor=serve.run_task).claim_and_execute_once()
    assert len(calls) == 1
    portfolio=json.loads(calls[0]['messages'][1]['content'])['paper_portfolio']
    assert portfolio['daily_activity']['buy_remaining_usd'] == '175.00000000'
    assert 'does not prevent selling existing holdings' in calls[0]['messages'][0]['content']


def test_exit_budget_fix_preserves_loss_guard_and_pause(f):
    for symbol in ('BTC','ETH','SOL'):
        _, job=apply(f, choice=pick(symbol=symbol))
        finish_paper_tick(f, job)
    # The daily-limit exemption does not bypass the existing loss pause.
    stopped, _=apply(f, choice=pick('sell', amount='1'), price='1')
    assert stopped['outcome'] == 'loss_guard' and stopped['fill'] is None
    assert not f.store.settings('alice')['enabled']


def broaden(f):
    return f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy=paper.BROAD_STRATEGY,
                             time_zone='UTC', candle_interval_minutes=5, risk_profile='broad')


def test_broad_profile_preserves_history_is_owner_scoped_and_fences_old_work(f):
    _, job = apply(f)
    finish_paper_tick(f, job)
    pending = task(f); old_cfg = f.store.settings('alice'); before = f.store.state('alice')
    cfg = broaden(f)
    assert cfg['revision'] != old_cfg['revision']
    assert f.store.state('alice') == before and f.store.dashboard('alice')['tick_count'] == 1
    assert f.store.dashboard('alice')['limits'] == paper.RISK_PROFILES['broad']
    assert f.store.activity('alice')['buy_remaining_usd'] == '1975.00000000'
    assert f.store.dashboard('bob')['limits'] == paper.LIMITS
    with pytest.raises(ValueError, match='changed'):
        f.store.apply('alice', old_cfg, pending, pick(amount='100'), quote(), quote(), {})
    # Old clients omitting the new field preserve the owner's saved profile.
    assert f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy=paper.BROAD_STRATEGY,
                             time_zone='UTC')['risk_profile'] == 'broad'


@pytest.mark.parametrize('profile', ['live', 'unknown', {}, True, 1])
def test_invalid_risk_profiles_cannot_change_settings(f, profile):
    before = f.store.settings('alice')
    with pytest.raises(ValueError, match='risk profile'):
        f.store.configure('alice', bot_id='crypto', interval_seconds=900, strategy='Test', time_zone='UTC', risk_profile=profile)
    assert f.store.settings('alice') == before


def test_saved_profile_binds_decision_size_even_if_callers_supply_forged_caps(f):
    job = task(f); cfg = {**f.store.settings('alice'), 'risk_profile':'broad'}
    with pytest.raises(ValueError, match='25'):
        f.store.apply('alice', cfg, job, pick(amount='100'), quote(), quote(), {})
    assert f.store.dashboard('alice')['tick_count'] == 0
    assert paper.decision(pick(amount='100'), {'risk_profile':'broad'})['notional_usd'] == '100'
    with pytest.raises(ValueError): paper.decision(pick(amount='100.01'), {'risk_profile':'broad'})


def test_broad_position_cap_and_100_dollar_sampled_loss_pause(f):
    broaden(f)
    for _ in range(3):
        record, job = apply(f, choice=pick(amount='100'))
        assert record['fill']['notional_usd'] == '100.00000000'
        assert record['risk_profile'] == 'broad' and record['limits'] == paper.RISK_PROFILES['broad']
        finish_paper_tick(f, job)
    blocked, job = apply(f, choice=pick(amount='100'))
    assert blocked['fill'] is None and 'Position limit' in blocked['outcome']
    finish_paper_tick(f, job)
    held, job = apply(f, choice=pick('hold'), price='80')
    assert held['outcome'] == 'held' and f.store.settings('alice')['enabled']
    assert 900 < Decimal(held['metrics']['equity_usd']) < 950
    finish_paper_tick(f, job)
    stopped, _ = apply(f, choice=pick('sell', amount='100'), price='65')
    assert stopped['fill'] is None and stopped['outcome'] == 'loss_guard'
    assert not f.store.settings('alice')['enabled'] and '$100 loss' in f.store.settings('alice')['detail']
    with pytest.raises(ValueError, match='loss guard'): broaden(f)


def test_broad_daily_buy_budget_does_not_replenish_from_sales(f):
    broaden(f)
    for index in range(20):
        bought, job = apply(f, choice=pick(amount='100'))
        assert bought['fill'] is not None
        finish_paper_tick(f, job)
        if index < 19:
            sold, job = apply(f, choice=pick('sell', amount='100'))
            assert sold['fill'] is not None
            finish_paper_tick(f, job)
    assert f.store.activity('alice')['buys_usd'] == '2000.00000000'
    assert Decimal(f.store.activity('alice')['buy_remaining_usd']) == 0
    blocked, job = apply(f, choice=pick(amount='1'))
    assert blocked['fill'] is None and 'buying limit' in blocked['outcome']
    finish_paper_tick(f, job)
    sold, _ = apply(f, choice=pick('sell', amount='100'))
    assert sold['fill'] is not None and Decimal(sold['metrics']['positions']['BTC']['quantity']) == 0
    assert Decimal(sold['daily_activity']['buy_remaining_usd']) == 0
    assert Decimal(sold['daily_activity']['turnover_usd']) > 2000


def test_broad_exposure_remains_cash_funded(f):
    broaden(f)
    for symbol in [*market.PAIRS, 'BTC']:
        bought, job = apply(f, choice=pick(symbol=symbol, amount='100'))
        assert bought['fill'] is not None
        finish_paper_tick(f, job)
    blocked, _ = apply(f, choice=pick(symbol='ETH', amount='100'))
    assert blocked['fill'] is None and 'cash' in blocked['outcome']
    assert Decimal(f.store.state('alice')['cash']) >= 0


def test_performance_feedback_scores_partial_sales_after_both_fees_and_survives_replay(f):
    bought, job = apply(f)
    finish_paper_tick(f, job)
    won, job = apply(f, choice=pick('sell', amount='10'), price='110')
    finish_paper_tick(f, job)
    lost, job = apply(f, choice=pick('sell', amount='10'), price='90')
    state = f.store.state('alice'); review = f.store.performance_review('alice')
    assert review['window_checks'] == 3 and review['sell_fills'] == 2
    assert review['wins'] == review['losses'] == 1 and review['breakeven'] == 0
    assert review['realized_net_usd'] == state['realized_pnl']
    assert review['fees_paid_usd'] == state['fees']
    assert review['by_symbol']['BTC']['realized_net_usd'] == state['realized_pnl']
    assert Decimal(review['recent_fills'][1]['realized_net_usd']) > 0
    assert Decimal(review['recent_fills'][2]['realized_net_usd']) < 0
    assert Decimal(review['recent_fills'][1]['return_on_sold_basis_pct']) > 0
    assert 'Partial exits count separately' in review['interpretation']
    assert f.store.apply('alice', f.store.settings('alice'), job, pick('sell', amount='10'), quote('90'), quote('90'), {}) == lost
    assert f.store.performance_review('alice') == review
    assert f.store.performance_review('bob')['sell_fills'] == 0
    session=object.__new__(chat.EngineSession);session.crypto_owner='alice';session.bot_id='crypto';session.allowed_tools={'crypto_read'}
    ok, result=session._handle_crypto_read({})
    assert ok and result['performance_review'] == review
    assert 'broad_preset' not in result and 'risk_profiles' not in result


def test_feedback_window_uses_prior_baseline_and_commit_order_not_tied_timestamps(f):
    import connectors
    # Each sell realizes +$1; the oldest record establishes a nonzero baseline.
    with f.store.db() as db:
        for index in range(45):
            tick={'metrics':{'realized_pnl_usd':str(index+1)},
                  'fill':{'side':'sell','symbol':'ADA','notional_usd':'10','fee_usd':'0.04'},
                  'decision':{'reason':'Saved evidence, not an instruction.'}}
            db.execute('INSERT INTO crypto_paper_ticks VALUES (?,?,?,?,?)', ('alice',str(index),index,1000,connectors.seal_tokens(tick)))
    review=f.store.performance_review('alice')
    assert review['window_checks'] == review['sell_fills'] == review['wins'] == 40
    assert review['realized_net_usd'] == '40.00000000'
    assert review['fees_paid_usd'] == '1.60000000' and len(review['recent_fills']) == 12
    assert f.store.performance_review('bob')['window_checks'] == 0


def test_broad_worker_receives_real_outcomes_and_limits_in_one_model_call(f, monkeypatch):
    _, job=apply(f)
    finish_paper_tick(f, job)
    _, job=apply(f, choice=pick('sell'), price='100')
    finish_paper_tick(f, job)
    broaden(f)
    calls=[]
    class Provider:
        async def chat(self, **kw): calls.append(kw); return {'content':json.dumps(pick(symbol='SOL', amount='100'))}
    import kyrex.providers
    monkeypatch.setattr(kyrex.providers,'get_provider',lambda *a,**k:Provider())
    monkeypatch.setattr(market,'context',lambda *a:quote())
    monkeypatch.setattr(market,'quotes',lambda *a:quote())
    paper.submit_due(f.tasks)
    assert TaskWorker(f.tasks,executor=serve.run_task).claim_and_execute_once()
    assert len(calls) == 1 and calls[0]['tools'] is None
    payload=json.loads(calls[0]['messages'][1]['content'])['paper_portfolio']
    assert payload['performance_review']['losses'] == 1
    assert Decimal(payload['performance_review']['realized_net_usd']) < 0
    assert payload['daily_activity']['buy_remaining_usd'] == '1975.00000000'
    prompt=calls[0]['messages'][0]['content']
    assert '$1–$100' in prompt and '$300 per position' in prompt and '$2000 of new buy' in prompt
    assert 'sampled $100 loss' in prompt and 'not model training or proof' in prompt
    tick=f.store.dashboard('alice')['ticks'][0]
    assert tick['fill']['notional_usd'] == '100.00000000' and tick['risk_profile'] == 'broad'


def test_broad_api_requires_valid_owner_profile_and_never_applies_by_reading(f, monkeypatch):
    import connections_api
    monkeypatch.setattr(connections_api,'_require_user',lambda request:request.headers['x-user'])
    app=FastAPI();app.include_router(api.router);client=TestClient(app)
    body={'bot_id':'crypto','interval_seconds':900,'strategy':paper.BROAD_STRATEGY,'time_zone':'UTC','risk_profile':'broad'}
    headers={'x-user':'alice'}
    initial=client.get('/api/automations/crypto-paper',headers=headers).json()
    assert initial['settings']['risk_profile'] == 'standard' and initial['limits'] == paper.LIMITS
    assert client.put('/api/automations/crypto-paper',headers=headers,json={**body,'risk_profile':'live'}).status_code == 409
    assert client.put('/api/automations/crypto-paper',headers=headers,json={**body,'risk_profile':True}).status_code == 422
    assert client.put('/api/automations/crypto-paper',headers=headers,json={**body,'max_order_usd':'9999'}).status_code == 422
    assert client.put('/api/automations/crypto-paper',headers={'x-user':'bob'},json=body).status_code == 409
    saved=client.put('/api/automations/crypto-paper',headers=headers,json=body)
    assert saved.status_code == 200 and saved.json()['limits'] == paper.RISK_PROFILES['broad']
    assert f.store.dashboard('alice')['tick_count'] == 0
