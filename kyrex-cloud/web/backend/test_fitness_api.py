"""Exercise real route auth/pairing and the engine's host-side tool boundary."""
import json
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import connections_api
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
