"""Exercise real route auth/pairing and the engine's host-side tool boundary."""
import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import connections_api
from test_fitness_profile import profile_db
from fitness_connections import FitnessConnections

@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET','fitness-api-key')
    c=FitnessConnections(tmp_path/'fitness.sqlite3')
    monkeypatch.setattr(connections_api,'_fitness',lambda:c)
    def user(request):
        owner=request.headers.get('x-test-user')
        if not owner: raise HTTPException(401)
        return owner
    monkeypatch.setattr(connections_api,'_require_user',user)
    app=FastAPI(); app.include_router(connections_api.router)
    return TestClient(app),c

def test_owner_reads_and_pair_start_require_auth(api):
    client,_=api
    assert client.post('/api/connections/samsung_health/pair').status_code==401
    assert client.get('/api/connections/fitness/read').status_code==401
    assert client.post('/api/connections/fitness/samsung_health/disconnect').status_code==401

def test_fitness_tool_returns_only_granted_owner_profile(api,monkeypatch,profile_db):
    import chat_service, fitness_profile
    _,store = api
    fitness_profile.update('alice', {'age':42,'goal':'endurance'}, 'I am 42 and my goal is endurance')
    fitness_profile.update('bob', {'age':60,'goal':'strength'}, 'I am 60 and my goal is strength')
    monkeypatch.setattr('fitness_connections.FitnessConnections',lambda:store)
    session = object.__new__(chat_service.EngineSession)
    session.fitness_owner='alice'; session.allowed_tools={'fitness_read'}
    ok,result = session._handle_fitness_read({'owner':'bob','provider':'samsung_health','collection':'workout'})
    assert ok and result['fitness_profile']['age'] == 42 and result['fitness_profile']['goal'] == 'endurance'
    assert result['fitness_profile_status'] == 'ok'
    session.allowed_tools=set()
    ok,result = session._handle_fitness_read({'collection':'workout'})
    assert not ok and 'fitness_profile' not in result

def test_pairing_upload_token_is_not_read_authority(api):
    client,c=api
    pair=client.post('/api/connections/samsung_health/pair',headers={'x-test-user':'alice'}).json()
    result=client.post('/api/connections/samsung_health/exchange',json=pair)
    assert result.status_code==200
    assert result.headers['cache-control']=='no-store'
    token=result.json()['device_token']
    assert client.get('/api/connections/fitness/read',headers={'authorization':'Bearer '+token}).status_code==401
    assert client.post('/api/connections/samsung_health/sync',json={'records':[]}).status_code==401
    assert client.post('/api/connections/samsung_health/sync',headers={'authorization':'Bearer '+token},json={'records':[]}).status_code==200
    client.post('/api/connections/fitness/samsung_health/disconnect',headers={'x-test-user':'alice'})
    assert client.post('/api/connections/samsung_health/sync',headers={'authorization':'Bearer '+token},json={'records':[]}).status_code==400

def test_bad_and_oversized_upload_bodies(api):
    client,_=api
    assert client.post('/api/connections/samsung_health/exchange',content='[]').status_code==400
    assert client.post('/api/connections/samsung_health/exchange',content='x'*256001).status_code==413

def test_fitness_host_does_not_trust_frame_owner(api,monkeypatch):
    import chat_service
    client,c=api
    monkeypatch.setattr('fitness_connections.FitnessConnections',lambda:c)
    session=object.__new__(chat_service.EngineSession)
    session.fitness_owner='alice'; session.allowed_tools={'fitness_read'}
    ok,result=session._handle_fitness_read({'provider':'samsung_health','owner':'bob'})
    assert ok and result['sources']['samsung_health']['status']=='disconnected'
    session.allowed_tools=set()
    ok,result=session._handle_fitness_read({'owner':'alice'})
    assert not ok and 'not granted' in result['error']

def test_fitness_host_uses_explicit_local_day_and_session_owner(api,monkeypatch):
    import chat_service
    _,c=api; calls=[]
    monkeypatch.setattr('fitness_connections.FitnessConnections',lambda:c)
    monkeypatch.setattr(c,'read',lambda owner,**kwargs: calls.append((owner,kwargs)) or {})
    session=object.__new__(chat_service.EngineSession)
    session.fitness_owner='alice'; session.allowed_tools={'fitness_read'}
    ok,_=session._handle_fitness_read({'owner':'bob','start':'2026-10-08','end':'2026-10-08',
                                     'collection':'workout'})
    assert ok and calls == [('alice',{'provider':'all','start':'2026-10-08','end':'2026-10-08',
                                    'collection':'workout','time_zone':'America/New_York'})]
    ok,_=session._handle_fitness_read({'timezone':'Europe/London'})
    assert ok and calls[-1][1]['time_zone']=='Europe/London'
    ok,_=session._handle_fitness_read({'timezone':[]})
    assert not ok and len(calls)==2

def test_fitness_chart_callback_is_owner_scoped_and_does_not_run_when_denied(api,monkeypatch):
    from datetime import datetime,timedelta,timezone
    import chat_service
    _,c=api; start=datetime.now(timezone.utc)-timedelta(hours=1)
    token=c.pair(c.begin('alice','samsung_health')['pairing_code'])['device_token']
    c.upload(token,[{'type':'workout','id':'owner-session','origin':'com.sec.android.app.shealth',
        'start':start.isoformat(),'end':(start+timedelta(minutes=30)).isoformat(),'exercise_type':0}])
    monkeypatch.setattr('fitness_connections.FitnessConnections',lambda:c)
    session=object.__new__(chat_service.EngineSession)
    session.fitness_owner='alice'; session.allowed_tools={'fitness_read'}; reports=[]
    session._workout_callback=reports.append
    ok,_=session._handle_fitness_read({'provider':'samsung_health','collection':'workout','owner':'bob'})
    assert ok and len(reports)==1 and reports[0]['sessions'][0]['needs_sync']
    session.allowed_tools=set()
    ok,_=session._handle_fitness_read({'owner':'alice'})
    assert not ok and len(reports)==1


def test_sleep_callback_owner_scope_and_model_dates(api,monkeypatch):
    from datetime import datetime,timedelta,timezone
    import chat_service
    _,c=api;end=datetime.now(timezone.utc)-timedelta(hours=2);start=end-timedelta(hours=8)
    token=c.pair(c.begin('alice','samsung_health')['pairing_code'])['device_token']
    c.upload(token,[{'type':'sleep','id':'owner-night','origin':'com.sec.android.app.shealth',
        'start':start.isoformat(),'end':end.isoformat(),'duration_seconds':25000}])
    monkeypatch.setattr('fitness_connections.FitnessConnections',lambda:c)
    session=object.__new__(chat_service.EngineSession)
    session.fitness_owner='alice';session.allowed_tools={'fitness_read'};reports=[]
    session._sleep_callback=reports.append
    ok,_=session._handle_fitness_read({'collection':'sleep','provider':'samsung_health','owner':'bob'})
    assert ok and any(n['metrics'].get('total_sleep_seconds')==25000 for n in reports[0]['sources'][0]['nights'])
    session.allowed_tools=set()
    assert not session._handle_fitness_read({'collection':'sleep'})[0]
    assert len(reports)==1

def test_workout_metrics_round_trip_through_phone_and_owner_routes(api):
    from datetime import datetime,timedelta,timezone
    client,c=api
    token=c.pair(c.begin('alice','samsung_health')['pairing_code'])['device_token']
    start=datetime.now(timezone.utc)-timedelta(hours=1)
    row={'type':'workout','id':'session','origin':'com.sec.android.app.shealth',
         'start':start.isoformat(),'end':(start+timedelta(minutes=30)).isoformat(),
         'exercise_type':0,'session_metrics':{'active_calories_kcal':245.5,'heart_rate_avg_bpm':132}}
    response=client.post('/api/connections/samsung_health/sync',
        headers={'authorization':'Bearer '+token},json={'records':[row],'complete':True})
    assert response.status_code==200
    response=client.get('/api/connections/fitness/read?provider=samsung_health&collection=workout',
                        headers={'x-test-user':'alice'})
    metrics=response.json()['sources']['samsung_health']['records'][0]['session_metrics']
    assert metrics['active_calories_kcal']==245.5 and metrics['heart_rate_avg_bpm']==132
    assert metrics['duration_seconds']==1800
    assert client.get('/api/connections/fitness/read?provider=samsung_health',
                      headers={'x-test-user':'bob'}).json()['sources']['samsung_health']['records']==[]

def test_fitness_wait_reports_stages_without_health_data(monkeypatch):
    import chat_service
    session = object.__new__(chat_service.EngineSession)
    stages = []
    session._progress_callback = stages.append
    session._handle_fitness_read = lambda frame: (True, {'private_health': 123})
    assert session._wait_fitness_read({}) == (True, {'private_health': 123})
    assert stages == [{'stage': 'Reading connected fitness data…'},
                      {'stage': 'Fitness read finished; preparing reply…'}]
    assert '123' not in json.dumps(stages)


def test_current_owner_workout_day_overrides_a_stale_model_date(api,monkeypatch):
    from datetime import datetime,timezone
    import chat_service,workout_report
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None): return datetime(2026,10,9,11,14,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(workout_report,'datetime',Clock)
    _,store=api; calls=[]
    monkeypatch.setattr('fitness_connections.FitnessConnections',lambda:store)
    monkeypatch.setattr(store,'read',lambda owner,**args: calls.append((owner,args)) or {})
    session=object.__new__(chat_service.EngineSession)
    session.fitness_owner='alice'; session.allowed_tools={'fitness_read'}
    session._fitness_request_text="Pull my workout today, including heart rate and calories."
    ok,_=session._handle_fitness_read({'owner':'bob','collection':'workout',
        'start':'2026-10-08','end':'2026-10-08'})
    assert ok and calls[-1][0]=='alice'
    assert calls[-1][1]['start']==calls[-1][1]['end']=='2026-10-09'
    session._fitness_request_text='Compare my workouts today and yesterday.'
    ok,_=session._handle_fitness_read({'collection':'workout','start':'2026-10-08','end':'2026-10-09'})
    assert ok and calls[-1][1]['start']=='2026-10-08'

@pytest.mark.parametrize('cancelled', [False, True])
def test_stalled_fitness_read_times_out_or_cancels_promptly(monkeypatch, cancelled):
    import chat_service
    import threading
    import time
    session = object.__new__(chat_service.EngineSession)
    release = threading.Event()
    session._handle_fitness_read = lambda frame: (release.wait(2), {})
    interrupted = []
    session.interrupt = lambda: interrupted.append(True)
    session.close = lambda: interrupted.append(True)
    monkeypatch.setattr(chat_service, 'FITNESS_READ_TIMEOUT', 0.03)
    started = time.monotonic()
    try:
        if cancelled:
            ok, result = session._wait_fitness_read({}, lambda: True)
            assert not ok and 'cancelled' in result['error']
        else:
            with pytest.raises(chat_service.EngineSessionError, match='Fitness data read timed out'):
                session._wait_fitness_read({})
        assert time.monotonic() - started < 1
        assert interrupted == [True]
    finally:
        release.set()

def test_sync_skipped_records_reach_owner_coverage(api):
    client,c=api
    code=c.begin('alice','samsung_health')['pairing_code']
    token=c.pair(code)['device_token']
    result=client.post('/api/connections/samsung_health/sync',
        headers={'authorization':'Bearer '+token},
        json={'records':[], 'complete':True, 'skipped_records':3})
    assert result.status_code==200
    result=client.get('/api/connections/fitness/read?provider=samsung_health',headers={'x-test-user':'alice'})
    assert result.status_code==200
    source=result.json()['sources']['samsung_health']
    assert source['incomplete'] and source['skipped_records']==3

def test_oura_callback_forwards_issuer_and_shows_only_safe_errors(api,monkeypatch):
    from fitness_connections import FitnessError, MODERN_ISSUER
    client,c=api; calls=[]
    def complete(*args,**kwargs):
        calls.append((args,kwargs))
        raise FitnessError('No supported Oura permissions were granted. Connect again.')
    monkeypatch.setattr(c,'complete',complete)
    response=client.get('/api/connections/oura/callback',params={'state':'private-state','code':'private-code','iss':MODERN_ISSUER})
    assert calls[0][1]['issuer']==MODERN_ISSUER
    assert 'No supported Oura permissions' in response.text
    assert 'private-code' not in response.text and 'private-state' not in response.text
    assert response.headers['cache-control']=='no-store'
    monkeypatch.setattr(c,'complete',lambda *a,**kw: (_ for _ in ()).throw(FitnessError('secret-body')))
    response=client.get('/api/connections/oura/callback')
    assert 'secret-body' not in response.text and 'not completed' in response.text
