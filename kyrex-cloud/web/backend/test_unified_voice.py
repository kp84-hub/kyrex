import array
from concurrent.futures import ThreadPoolExecutor
import io
import json
import sqlite3
import wave
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import voice_api as api
import chat_service
from voice_devices import VoiceDevices
from voice_speech import VoiceSpeech, SpeechError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR',str(tmp_path));monkeypatch.setenv('WEB_SESSION_SECRET','test-only-encryption-secret')
    def owner(request):
        if not request.headers.get('x-owner'): raise HTTPException(401)
        return request.headers['x-owner']
    monkeypatch.setattr(api,'owner',owner)
    def thread(user):
        cid='voice-'+user;VoiceDevices().remember(user,cid);return cid
    monkeypatch.setattr(api,'voice_conversation',thread)
    monkeypatch.setattr(chat_service,'get_conversation',lambda user,cid: {'bot_id':'overwatcher'} if cid=='voice-'+user else None)
    app=FastAPI();app.include_router(api.router)
    return TestClient(app)


def browser(owner='alice',identity='a'*32): return {'x-owner':owner,'X-Voice-Client':identity}


def claim(client,headers,takeover=True):
    value=client.post('/api/voice/listen',headers=headers,json={'name':'Test device','takeover':takeover})
    assert value.status_code==200,value.text
    return {**headers,'X-Voice-Lease':value.json()['lease']}


def test_browser_identity_and_account_settings_are_shared_without_exposing_key(client):
    key='test-only-api-key-1234567890'
    result=client.post('/api/connections/voice/settings',headers={'x-owner':'alice'},json={'engine':'openai','voice':'marin','api_key':key})
    assert result.status_code==200 and key not in result.text
    response=client.get('/api/voice/settings',headers={'x-owner':'alice'})
    assert response.json()['voice']=='marin' and response.json()['configured']
    assert client.get('/api/voice/settings',headers={'x-owner':'bob'}).json()['configured'] is False
    assert key.encode() not in VoiceSpeech().path.read_bytes()
    assert client.get('/api/voice/session',headers={'x-owner':'alice'}).status_code==400
    assert client.get('/api/voice/session',headers=browser()).json()['conversation_id']=='voice-alice'
    assert client.post('/api/connections/voice/settings',headers={'x-owner':'alice','Origin':'https://foreign.example'},json={'engine':'device','voice':'cedar'}).status_code==403


def test_multiple_native_clients_and_individual_revocation(client):
    tokens=[];ids=[]
    for platform in ('android','windows'):
        code=client.post('/api/connections/voice/connect',headers={'x-owner':'alice'},json={'name':platform,'platform':platform}).json()['pairing_code']
        pair=client.post('/api/voice/pair',json={'pairing_code':code}).json();tokens.append(pair['device_token']);ids.append(pair['device_id'])
    assert len(client.get('/api/connections/voice',headers={'x-owner':'alice'}).json()['devices'])==2
    for token in tokens: assert VoiceDevices().resolve(token)==('alice','voice-alice')
    client.post('/api/connections/voice/devices/'+ids[0]+'/disconnect',headers={'x-owner':'bob'})
    assert VoiceDevices().resolve(tokens[0])[0]=='alice'
    client.post('/api/connections/voice/devices/'+ids[0]+'/disconnect',headers={'x-owner':'alice'})
    with pytest.raises(ValueError):VoiceDevices().resolve(tokens[0])
    assert VoiceDevices().resolve(tokens[1])[0]=='alice'
    assert client.get('/api/voice/settings',headers={'Authorization':'Bearer invalid','x-owner':'alice'}).status_code==401


def test_takeover_stale_stop_and_heartbeat_never_reacquire(client):
    a=claim(client,browser());b=browser(identity='b'*32)
    assert client.post('/api/voice/listen',headers=b,json={'name':'PC'}).status_code==409
    b=claim(client,b)
    assert client.post('/api/voice/heartbeat',headers=a).status_code==409
    client.post('/api/voice/stop',headers=a)
    assert client.post('/api/voice/heartbeat',headers=b).status_code==200
    assert client.post('/api/voice/heartbeat',headers={**b,'x-owner':'bob'}).status_code==409
    with VoiceDevices().db() as db:db.execute('UPDATE voice_leases SET expires=0')
    assert client.post('/api/voice/heartbeat',headers=b).status_code==409
    assert client.post('/api/voice/chat',headers=b,json={'message':'Send a message','request_id':'a'*32}).status_code==409


def test_two_simultaneous_submissions_reserve_only_one_turn(client):
    headers=claim(client,browser());store=VoiceDevices();secret=headers['X-Voice-Lease']
    def reserve(i):
        try:store.reserve('alice','browser:'+'a'*32,secret,str(i)*32);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=2) as executor: results=list(executor.map(reserve,[1,2]))
    assert results.count(True)==1
    assert client.post('/api/voice/listen',headers=browser(identity='b'*32),json={'name':'Phone','takeover':True}).status_code==409


def test_legacy_credentials_migrate_without_exposing_or_revoking(tmp_path,monkeypatch):
    monkeypatch.setenv('KYREX_DATA_DIR',str(tmp_path))
    from voice_devices import digest
    path=tmp_path/'voice_devices.sqlite3';token='test-only-native-legacy-token-12345'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE devices (owner TEXT PRIMARY KEY, token TEXT UNIQUE, conversation TEXT, expires REAL)')
        db.execute('INSERT INTO devices VALUES (?,?,?,?)',('alice',digest(token),'voice-alice',9e20))
    assert VoiceDevices().resolve(token)==('alice','voice-alice')
    assert VoiceDevices().view('alice')['devices'][0]['platform']=='android'
    assert token.encode() not in path.read_bytes()


def wav(samples=16000,silent=False):
    output=io.BytesIO()
    with wave.open(output,'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
        f.writeframes(array.array('h',[0 if silent else 1000]*samples).tobytes())
    return output.getvalue()


class ProviderResponse:
    status_code=200;headers={'Content-Type':'audio/wav'}
    def __init__(self,data):self.data=data;self.closed=False
    def iter_content(self,size):return iter([self.data])
    def close(self):self.closed=True


def test_transcription_is_bounded_and_never_executes_chat(client,monkeypatch):
    headers=claim(client,browser());calls=[]
    client.post('/api/connections/voice/settings',headers={'x-owner':'alice'},json={'engine':'openai','voice':'cedar','api_key':'test-only-api-key-1234567890'})
    monkeypatch.setattr(chat_service,'stream_chat',lambda *a,**k:pytest.fail('Transcription must not execute an action'))
    def provider(url,**kwargs):calls.append((url,kwargs));return ProviderResponse(b'{"text":"Show my calendar"}')
    monkeypatch.setattr('voice_speech.requests.post',provider)
    assert client.post('/api/voice/transcribe',headers={**headers,'Content-Type':'audio/wav'},content=wav(silent=True)).status_code==400
    assert not calls
    result=client.post('/api/voice/transcribe',headers={**headers,'Content-Type':'audio/wav'},content=wav())
    assert result.json()=={'text':'Show my calendar'}
    assert len(calls)==1 and calls[0][0]=='https://api.openai.com/v1/audio/transcriptions'
    assert calls[0][1]['allow_redirects'] is False
    assert calls[0][1]['files']['file'][1]==wav()
    assert client.post('/api/voice/transcribe',headers={**headers,'Content-Type':'audio/wav'},content=b'x'*1_000_001).status_code==413
    assert len(calls)==1


def test_shared_speech_uses_saved_voice_and_redacts_provider_failures(client,monkeypatch):
    headers=claim(client,browser());calls=[];key='test-only-api-key-1234567890'
    client.post('/api/connections/voice/settings',headers={'x-owner':'alice'},json={'engine':'openai','voice':'marin','api_key':key})
    response=ProviderResponse(wav())
    def provider(url,**kwargs):calls.append((url,kwargs));return response
    monkeypatch.setattr('voice_speech.requests.post',provider)
    result=client.post('/api/voice/speech',headers=headers,json={'text':'Hello Keith.','voice':'untrusted','owner':'bob'})
    assert result.status_code==200 and result.content==wav()
    assert calls[0][1]['json']['voice']=='marin'
    assert key not in json.dumps(calls[0][1]['json'])
    assert response.closed and result.headers['cache-control']=='no-store'
    response.status_code=401
    result=client.post('/api/voice/speech',headers=headers,json={'text':'Hello.'})
    assert result.status_code==400 and key not in result.text
    assert len(calls)==2


def test_speech_bounds_and_key_removal(client,monkeypatch):
    store=VoiceSpeech();key='test-only-api-key-1234567890'
    store.configure('alice',{'engine':'openai','voice':'cedar','api_key':key})
    response=ProviderResponse(b'x'*(store.MAX_AUDIO+1));monkeypatch.setattr('voice_speech.requests.post',lambda *a,**k:response)
    with pytest.raises(SpeechError):store.speak('alice','Hello')
    assert response.closed
    store.configure('alice',{'engine':'device','voice':'cedar','remove_key':True})
    with pytest.raises(SpeechError):store.key('alice')
    assert not store.view('alice')['configured']
