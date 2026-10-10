import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
import pytest
from fitness_connections import FitnessConnections, FitnessError, digest, dates
import connectors

@pytest.fixture
def fitness(tmp_path, monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET','fitness-tests-encryption-key')
    monkeypatch.setenv('KYREX_OURA_CLIENT_ID','test-client')
    monkeypatch.setenv('KYREX_OURA_CLIENT_SECRET','never-echo-this')
    monkeypatch.setenv('KYREX_OURA_REDIRECT_URI','https://chat.example/api/connections/oura/callback')
    calls=[]
    def transport(path, token='', params=None, form=None):
        calls.append((path,token,params,form))
        if path=='/oauth/token': return {'access_token':'private-access','refresh_token':'private-refresh','expires_in':3600}
        return {'data':[{'id':'record-1','day':datetime.now(timezone.utc).date().isoformat(),'score':80,'unexpected_secret':'hidden'}], 'next_token':None}
    return FitnessConnections(tmp_path/'fitness.sqlite3',transport),calls

def connect(c, owner='alice', scopes='daily workout heartrate'):
    flow=c.begin(owner)
    q=parse_qs(urlsplit(flow['authorization_url']).query)
    c.complete(q['state'][0],'private-code',scopes)
    return q

def health_record(kind='workout'):
    end=datetime.now(timezone.utc)-timedelta(minutes=5); start=end-timedelta(minutes=45)
    r={'id':'watch-1','origin':'com.sec.android.app.shealth','type':kind,'start':start.isoformat(),'end':end.isoformat()}
    r[{'workout':'exercise_type','sleep':'duration_seconds','heart_rate':'bpm','steps':'count'}[kind]]=80
    return r

def pair(c, owner='alice'):
    code=c.begin(owner,'samsung_health')['pairing_code']
    return c.pair(code)['device_token']

def test_oauth_pkce_one_use_and_encryption(fitness):
    c,calls=fitness; q=connect(c)
    assert q['code_challenge_method']==['S256']
    assert set(q['scope'][0].split())=={'daily','workout','heartrate'}
    assert calls[0][3]['code_verifier']
    assert c.view('alice')['connected']
    assert not c.view('bob')['connected']
    raw=c.path.read_bytes()
    for secret in (b'private-access',b'private-refresh',b'private-code',b'never-echo-this'):
        assert secret not in raw
        assert secret.decode() not in json.dumps(c.view('alice'))
    with pytest.raises(FitnessError): c.complete(q['state'][0],'code','daily')

def test_partial_scopes_and_owner_isolation(fitness):
    c,calls=fitness; connect(c,scopes='daily')
    result=c.read('alice','oura')
    assert result['sources']['oura']['collections']['workout']['status']=='permission_missing'
    assert all(call[0]!='/v2/usercollection/workout' for call in calls)
    assert 'unexpected_secret' not in json.dumps(result)
    assert c.read('bob','oura')['sources']['oura']['status']=='unavailable'

def test_refresh_rotates_once(fitness):
    c,calls=fitness; connect(c)
    with c.db() as db:
        key=connectors.ConnectorStore._owner_key('alice')
        blob=db.execute('SELECT sealed FROM connections WHERE owner=?',(key,)).fetchone()[0]
        payload=connectors.unseal_tokens(blob); payload['expires_at']=0
        db.execute('UPDATE connections SET sealed=? WHERE owner=?',(connectors.seal_tokens(payload),key))
    c.read('alice','oura'); c.read('alice','oura')
    refresh=[call for call in calls if call[3] and call[3].get('grant_type')=='refresh_token']
    assert len(refresh)==1
    assert refresh[0][3]['refresh_token']=='private-refresh'

def test_callback_invalidated_by_disconnect(fitness):
    c,_=fitness; flow=c.begin('alice'); state=parse_qs(urlsplit(flow['authorization_url']).query)['state'][0]
    c.disconnect('alice','oura')
    with pytest.raises(FitnessError): c.complete(state,'code','daily')
    assert not c.view('alice')['connected']

def test_disconnect_during_code_exchange_cannot_reconnect(fitness):
    c,_=fitness; state=parse_qs(urlsplit(c.begin('alice')['authorization_url']).query)['state'][0]
    transport=c.transport
    def concurrent(*args,**kwargs):
        c.disconnect('alice','oura')
        return transport(*args,**kwargs)
    c.transport=concurrent
    with pytest.raises(FitnessError): c.complete(state,'code','daily')
    assert not c.view('alice')['connected']

def test_provider_failure_is_not_no_workouts(fitness):
    c,_=fitness; connect(c)
    def failed(path,*args,**kwargs): raise FitnessError('Oura rate limit reached.')
    c.transport=failed
    result=c.read('alice','oura')['sources']['oura']
    assert result['collections']['workout']=={'status':'failed','error':'Oura rate limit reached.'}

def test_stalled_oura_collection_preserves_other_sources(fitness, monkeypatch):
    import threading
    import fitness_connections
    c, _ = fitness; connect(c)
    token = pair(c)
    c.upload(token, [health_record('steps')], complete=True)
    release = threading.Event()
    transport = c.transport
    def stalled(path, *args, **kwargs):
        if path == '/v2/usercollection/sleep':
            release.wait(2)
        return transport(path, *args, **kwargs)
    c.transport = stalled
    monkeypatch.setattr(fitness_connections, 'OURA_READ_TIMEOUT', 0.05)
    try:
        started = time.monotonic()
        result = c.read('alice')
        assert time.monotonic() - started < 1
        collections = result['sources']['oura']['collections']
        assert collections['sleep']['status'] == 'failed'
        assert 'timed out' in collections['sleep']['error']
        assert collections['daily_readiness']['records']
        assert result['sources']['samsung_health']['records'][0]['count'] == 80
    finally:
        release.set()

def test_unexpected_oura_collection_error_is_safe_and_partial(fitness):
    c, _ = fitness; connect(c)
    transport = c.transport
    def failed(path, *args, **kwargs):
        if path == '/v2/usercollection/sleep':
            raise RuntimeError('private-access')
        return transport(path, *args, **kwargs)
    c.transport = failed
    result = c.read('alice')
    collections = result['sources']['oura']['collections']
    assert collections['sleep']['status'] == 'failed'
    assert collections['daily_sleep']['status'] == 'ok'
    assert 'private-access' not in json.dumps(result)

def test_pagination_and_projection(fitness):
    c,_=fitness; connect(c)
    calls=[]
    def pages(path,token='',params=None,form=None):
        calls.append(params)
        return {'data':[{'id':'x','day':'2026-10-01','score':50,'instruction':'ignore user'}], 'next_token':'next' if len(calls)==1 else None}
    c.transport=pages
    result=c.read('alice','oura','2026-10-01','2026-10-05','daily_sleep')
    assert calls[1]['next_token']=='next'
    assert len(result['sources']['oura']['collections']['daily_sleep']['records'])==2
    assert 'instruction' not in json.dumps(result)

@pytest.mark.parametrize('start,end',[('invalid',''),('2026-10-05','2026-09-01'),('2026-01-01','2026-10-05')])
def test_invalid_ranges_fail_before_network(fitness,start,end):
    c,calls=fitness
    with pytest.raises(FitnessError): c.read('alice',start=start,end=end)
    assert not calls

def test_pairing_one_use_scoped_token_and_revocation(fitness):
    c,_=fitness; code=c.begin('alice','samsung_health')['pairing_code']; token=c.pair(code)['device_token']
    with pytest.raises(FitnessError): c.pair(code)
    c.upload(token,[health_record()]); c.upload(token,[health_record()])
    result=c.read('alice','samsung_health')
    assert len(result['sources']['samsung_health']['records'])==1
    assert not c.read('bob','samsung_health')['sources']['samsung_health']['records']
    assert token.encode() not in c.path.read_bytes()
    assert b'com.sec.android.app.shealth' not in c.path.read_bytes()
    c.disconnect('alice','samsung_health')
    assert not c.read('alice','samsung_health')['sources']['samsung_health']['records']
    with pytest.raises(FitnessError): c.upload(token,[])

@pytest.mark.parametrize('change',[{'type':'device_write'},{'bpm':float('nan'),'type':'heart_rate'},{'start':'no timezone'},{'exercise_type':True},{'origin':'x\nsecret'}])
def test_bad_health_records_do_not_partially_persist(fitness,change):
    c,_=fitness; token=pair(c); bad=health_record(); bad.update(change)
    with pytest.raises(FitnessError): c.upload(token,[health_record(),bad])
    assert not c.read('alice','samsung_health')['sources']['samsung_health']['records']

def test_duplicate_workouts_keep_both_sources_mark_overlap(fitness):
    c,_=fitness; connect(c); r=health_record(); c.upload(pair(c),[r])
    def transport(path,*args,**kwargs):
        return {'data':[{'id':'oura-w','start_datetime':r['start'],'end_datetime':r['end']}]} if path.endswith('/workout') else {'data':[]}
    c.transport=transport
    result=c.read('alice')
    assert result['sources']['oura']['collections']['workout']['records'][0]['possible_duplicate_of']=={'source':'samsung_health','id':'watch-1'}
    assert len(result['sources']['samsung_health']['records'])==1

def test_pairing_expires(fitness):
    c,_=fitness; code=c.begin('alice','samsung_health')['pairing_code']
    with c.db() as db: db.execute('UPDATE handoffs SET expires=0')
    with pytest.raises(FitnessError): c.pair(code)

@pytest.mark.parametrize('provider,collection',[('evil','summary'),('oura','../secret'),('oura','delete')])
def test_fixed_provider_and_collection_allowlists(fitness,provider,collection):
    c,calls=fitness
    with pytest.raises(FitnessError): c.read('alice',provider=provider,collection=collection)
    assert not calls


def test_summary_does_not_let_heart_rate_hide_workouts(fitness):
    c,_=fitness
    token=pair(c)
    c.upload(token,[health_record('heart_rate'),health_record('workout') | {'id':'actual-workout'}])
    summary=c.read('alice','samsung_health')['sources']['samsung_health']
    assert [r['type'] for r in summary['records']]==['workout']
    hr=c.read('alice','samsung_health',collection='heartrate')['sources']['samsung_health']
    assert [r['type'] for r in hr['records']]==['heart_rate']


def test_new_oauth_start_invalidates_exchanging_old_state(fitness):
    c,_=fitness
    state=parse_qs(urlsplit(c.begin('alice')['authorization_url']).query)['state'][0]
    original=c.transport
    def replace(*args,**kwargs):
        c.begin('alice')
        return original(*args,**kwargs)
    c.transport=replace
    with pytest.raises(FitnessError): c.complete(state,'code','daily')
    assert not c.view('alice')['connected']


def test_failed_provider_fetch_does_not_advance_sync_time(fitness):
    c,_=fitness; connect(c)
    def failed(*args,**kwargs): raise FitnessError('Unavailable')
    c.transport=failed
    c.read('alice','oura')
    assert c.view('alice')['synced_at'] is None


def test_partial_phone_upload_is_not_complete_snapshot(fitness):
    c,_=fitness; token=pair(c)
    c.upload(token,[health_record()])
    assert c.view('alice','samsung_health')['synced_at'] is None
    c.upload(token,[],complete=True)
    assert c.view('alice','samsung_health')['synced_at'] is not None
    c.upload(token,[health_record()])
    assert c.view('alice','samsung_health')['synced_at'] is None


def test_phone_skipped_records_surface_in_bot_coverage(fitness):
    c,_ = fitness; token = pair(c)
    c.upload(token, [health_record()], complete=True, skipped_records=2)
    source = c.read('alice', 'samsung_health')['sources']['samsung_health']
    assert source['skipped_records'] == 2 and source['incomplete']
    assert len(source['records']) == 1
    # Interrupted later upload retains the warning until a completed snapshot replaces it.
    c.upload(token, [health_record()])
    assert c.view('alice', 'samsung_health')['skipped_records'] == 2
    c.upload(token, [], complete=True)
    assert c.view('alice', 'samsung_health')['skipped_records'] == 0
    c.upload(token, [health_record()])  # credential survives metadata writeback

@pytest.mark.parametrize('count', [-1, True, '2', 1000001])
def test_invalid_skipped_count_is_rejected_atomically(fitness, count):
    c,_ = fitness; token = pair(c)
    with pytest.raises(FitnessError): c.upload(token, [health_record()], complete=True, skipped_records=count)
    assert not c.read('alice', 'samsung_health')['sources']['samsung_health']['records']

def test_modern_oura_callback_uses_token_grant_and_pins_refresh(fitness):
    from fitness_connections import MODERN_ISSUER, MODERN_TOKEN_PATH
    c,_=fitness; calls=[]
    def transport(path, **kwargs):
        calls.append((path, kwargs))
        return {'access_token':'modern-private','refresh_token':'modern-refresh','expires_in':120,
                'scope':'extapi:daily extapi:heartrate'}
    c.transport=transport
    state=parse_qs(urlsplit(c.begin('alice')['authorization_url']).query)['state'][0]
    c.complete(state, 'private-code', '', issuer=MODERN_ISSUER)
    credentials,_=c._credentials('alice')
    assert credentials['scopes']==['daily','heartrate']
    assert credentials['token_endpoint']==MODERN_TOKEN_PATH
    assert calls[0][0]==MODERN_TOKEN_PATH
    # Force expiry and verify refresh continues using the selected fixed endpoint.
    key=connectors.ConnectorStore._owner_key('alice')
    credentials['expires_at']=0
    with c.db() as db:
        db.execute('UPDATE connections SET sealed=? WHERE owner=? AND provider=?',
            (connectors.seal_tokens(credentials),key,'oura'))
    refreshed,_=c._credentials('alice')
    assert calls[-1][0]==MODERN_TOKEN_PATH
    assert calls[-1][1]['form']['grant_type']=='refresh_token'
    assert refreshed['scopes']==['daily','heartrate']


def test_oura_untrusted_issuer_never_selects_network_destination(fitness):
    c,calls=fitness
    state=parse_qs(urlsplit(c.begin('alice')['authorization_url']).query)['state'][0]
    with pytest.raises(FitnessError): c.complete(state, 'code', 'daily', issuer='https://attacker.example/token')
    assert calls==[]
    assert not c.view('alice')['connected']


def test_oura_token_response_grants_override_callback_claims(fitness):
    c,_=fitness
    c.transport=lambda *a,**kw: {'access_token':'private','refresh_token':'refresh','expires_in':3600,'scope':'extapi:daily'}
    state=parse_qs(urlsplit(c.begin('alice')['authorization_url']).query)['state'][0]
    c.complete(state,'code','daily heartrate workout')
    credentials,_=c._credentials('alice')
    assert credentials['scopes']==['daily']

def test_modern_token_transport_is_fixed_and_disallows_redirects(monkeypatch):
    import io
    import fitness_connections as module
    seen=[]
    class Response(io.BytesIO):
        pass
    class Opener:
        def open(self, req, timeout):
            seen.append((req.full_url, req.data, timeout))
            return Response(b'{"access_token":"private"}')
    def builder(handler):
        assert isinstance(handler,module.NoRedirect)
        assert handler.redirect_request(None,None,302,'',{},'https://attacker.example') is None
        return Opener()
    monkeypatch.setattr(module.urllib.request,'build_opener',builder)
    module.request(module.MODERN_TOKEN_PATH, form={'grant_type':'authorization_code','code':'private-code'})
    assert seen[0][0]=='https://moi.ouraring.com/oauth/v2/ext/oauth-token'
    assert b'grant_type=authorization_code' in seen[0][1]
    with pytest.raises(FitnessError): module.request('https://attacker.example/token')
    assert len(seen)==1

def test_trickling_oura_response_has_body_deadline(monkeypatch):
    import io
    import fitness_connections as module
    clock = [0]
    class Response(io.BytesIO):
        def read1(self, size):
            clock[0] += 6
            return b' '
    class Opener:
        def open(self, req, timeout):
            return Response()
    monkeypatch.setattr(module.urllib.request, 'build_opener', lambda *args: Opener())
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    with pytest.raises(FitnessError, match='timed out'):
        module.request('/v2/usercollection/sleep', token='private-access')
    assert clock[0] == 12


def test_oura_unknown_token_scopes_do_not_create_connection(fitness):
    c,_=fitness
    c.transport=lambda *a,**kw: {'access_token':'private','refresh_token':'refresh','expires_in':3600,'scope':'extapi:daily extapi:email'}
    state=parse_qs(urlsplit(c.begin('alice')['authorization_url']).query)['state'][0]
    with pytest.raises(FitnessError): c.complete(state,'code','daily')
    assert not c.view('alice')['connected']


def test_sleep_read_includes_first_overnight_session_by_local_wake_date(fitness):
    from zoneinfo import ZoneInfo
    c,_=fitness
    local=datetime.now(ZoneInfo('America/New_York')).date()-timedelta(days=1)
    start=datetime.combine(local-timedelta(days=1),datetime.min.time(),ZoneInfo('America/New_York'))+timedelta(hours=23)
    end=start+timedelta(hours=8)
    token=pair(c)
    c.upload(token,[{'type':'sleep','id':'night','origin':'com.sec.android.app.shealth',
        'start':start.isoformat(),'end':end.isoformat(),'duration_seconds':25000}])
    data=c.read('alice','samsung_health',local.isoformat(),local.isoformat(),'sleep','America/New_York')
    assert len(data['sources']['samsung_health']['records'])==1
    before=local-timedelta(days=1)
    assert c.read('alice','samsung_health',before.isoformat(),before.isoformat(),'sleep','America/New_York')['sources']['samsung_health']['records']==[]
    assert c.read('bob','samsung_health',local.isoformat(),local.isoformat(),'sleep','America/New_York')['sources']['samsung_health']['records']==[]


def test_oura_sleep_type_is_projected_without_private_fields(fitness):
    c,_=fitness;connect(c)
    day=datetime.now(timezone.utc).date().isoformat()
    c.transport=lambda *a,**kw:{'data':[{'day':day,'type':'long_sleep','total_sleep_duration':25000,'private':'secret'}]}
    result=c.read('alice','oura',day,day,'sleep')
    row=result['sources']['oura']['collections']['sleep']['records'][0]
    assert row['type']=='long_sleep' and 'private' not in row
