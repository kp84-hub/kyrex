import array
import base64
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


@pytest.fixture
def google_key():
    # Generate ephemeral test material; never commit a private key fixture.
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return json.dumps({'type': 'service_account', 'project_id': 'test-project',
                      'client_email': 'voice@test-project.iam.gserviceaccount.com',
                      'private_key': key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
                      'token_uri': 'https://oauth2.googleapis.com/token'})


def cortex_settings(google_key, **changes):
    return {'engine': 'openai', 'speech_provider': 'google', 'voice': 'en-US-Chirp3-HD-Leda',
            'transcription_provider': 'groq', 'transcription_model': 'whisper-large-v3',
            'credentials': {'google': google_key, 'groq': 'test-only-groq-api-key-1234567890'}, **changes}


def test_configurable_providers_encrypt_credentials_and_preserve_legacy_companions(client, google_key):
    body = cortex_settings(google_key)
    result = client.post('/api/connections/voice/settings', headers={'x-owner': 'alice'}, json=body)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value['configured'] and value['engine'] == 'openai'  # v0.4 clients use the shared speech endpoint.
    assert value['speech_provider'] == 'google' and value['transcription_provider'] == 'groq'
    assert value['credentials'] == {'google': True, 'groq': True, 'openai': False}
    assert 'private_key' not in result.text and body['credentials']['groq'] not in result.text
    assert google_key.encode() not in VoiceSpeech().path.read_bytes()
    assert body['credentials']['groq'].encode() not in VoiceSpeech().path.read_bytes()
    assert 'Leda' in value['options']['voices']['google'][17]
    code = client.post('/api/connections/voice/connect', headers={'x-owner': 'alice'}, json={'platform': 'android'}).json()['pairing_code']
    token = client.post('/api/voice/pair', json={'pairing_code': code}).json()['device_token']
    native = client.get('/api/voice/settings', headers={'Authorization': 'Bearer ' + token})
    assert native.json() == value
    assert client.get('/api/voice/settings', headers={'x-owner': 'bob'}).json()['credentials'] == dict.fromkeys(value['credentials'], False)


def test_legacy_speech_settings_migrate_without_replacing_key_or_voice(client):
    store = VoiceSpeech()
    key = 'test-only-original-openai-key-1234567890'
    sealed = store.box().encrypt(key.encode()).decode()
    with store.db() as db:
        db.execute('INSERT INTO settings VALUES (?,?,?,?)', ('legacy', sealed, 'openai', 'marin'))
    migrated = VoiceSpeech()
    assert migrated.key('legacy') == key
    value = migrated.view('legacy')
    assert value['speech_provider'] == value['transcription_provider'] == 'openai'
    assert value['configured'] and value['voice'] == 'marin'
    migrated.configure('legacy', {'engine': 'device', 'voice': 'marin', 'remove_key': True})
    assert not VoiceSpeech().view('legacy')['credentials']['openai']
    with pytest.raises(SpeechError): VoiceSpeech().key('legacy')


def test_groq_transcription_uses_chosen_model_without_executing_chat(client, monkeypatch, google_key):
    store = VoiceSpeech(); store.configure('alice', cortex_settings(google_key, transcription_model='whisper-large-v3-turbo'))
    calls = []; response = ProviderResponse(b'{"text":"Show my calendar"}')
    def provider(url, **kwargs): calls.append((url, kwargs)); return response
    monkeypatch.setattr('voice_speech.requests.post', provider)
    monkeypatch.setattr(chat_service, 'stream_chat', lambda *a, **k: pytest.fail('Transcription must not execute actions'))
    headers = claim(client, browser())
    result = client.post('/api/voice/transcribe', headers={**headers, 'Content-Type': 'audio/wav'}, content=wav())
    assert result.json() == {'text': 'Show my calendar'}
    assert calls[0][0] == 'https://api.groq.com/openai/v1/audio/transcriptions'
    assert calls[0][1]['data']['model'] == 'whisper-large-v3-turbo'
    assert calls[0][1]['files']['file'] == ('command.wav', wav(), 'audio/wav')
    assert calls[0][1]['allow_redirects'] is False and response.closed


def test_google_speech_and_preview_use_owner_voice_with_bounded_wav(client, monkeypatch, google_key):
    VoiceSpeech().configure('alice', cortex_settings(google_key, voice='en-US-Chirp3-HD-Puck', speaking_rate=0.75))
    calls = []; responses = []
    def provider(url, **kwargs):
        calls.append((url, kwargs))
        payload = {'access_token': 'test-only-short-lived-access-token', 'expires_in': 3600, 'token_type': 'Bearer'} if url.endswith('/token') else {'audioContent': base64.b64encode(wav()).decode()}
        response = ProviderResponse(json.dumps(payload).encode()); responses.append(response); return response
    monkeypatch.setattr('voice_speech.requests.post', provider)
    headers = claim(client, browser())
    result = client.post('/api/voice/speech', headers=headers, json={'text': 'Hello.', 'owner': 'bob', 'voice': 'cedar'})
    assert result.status_code == 200 and result.content == wav()
    assert result.headers['content-type'] == 'audio/wav'
    assert calls[0][0] == 'https://oauth2.googleapis.com/token'
    assert calls[1][0] == 'https://texttospeech.googleapis.com/v1/text:synthesize'
    assert calls[1][1]['json'] == {'input': {'text': 'Hello.'}, 'voice': {'languageCode': 'en-US', 'name': 'en-US-Chirp3-HD-Puck'},
                                 'audioConfig': {'audioEncoding': 'LINEAR16', 'sampleRateHertz': 24000, 'speakingRate': 0.75}}
    assert calls[1][1]['headers']['x-goog-user-project'] == 'test-project'
    assert all(response.closed for response in responses)
    assert all(kwargs['allow_redirects'] is False for _, kwargs in calls)
    assert 'private_key' not in json.dumps(calls[1][1]['json'])
    preview = client.post('/api/connections/voice/preview', headers={'x-owner': 'alice'})
    assert preview.status_code == 200 and preview.headers['cache-control'] == 'no-store'
    assert calls[-1][1]['json']['input']['text'] == "Hi, I'm Kyrex. This is your saved reply voice."
    assert client.post('/api/connections/voice/preview', headers={'x-owner': 'alice', 'Origin': 'https://foreign.example'}).status_code == 403
    count = len(calls)
    assert client.post('/api/connections/voice/preview', headers={'x-owner': 'bob'}).status_code == 400
    assert len(calls) == count


def test_independent_credential_removal_keeps_other_provider_and_no_environment_fallback(client, monkeypatch, google_key):
    store = VoiceSpeech(); store.configure('alice', cortex_settings(google_key))
    store.configure('alice', cortex_settings('', credentials={'google': '', 'groq': ''}, remove_credentials=['groq']))
    assert store.view('alice')['credentials'] == {'google': True, 'groq': False, 'openai': False}
    assert store.view('alice')['speech_configured'] and not store.view('alice')['configured']
    monkeypatch.setenv('GROQ_API_KEY', 'test-only-server-secret-not-for-users')
    monkeypatch.setattr('voice_speech.requests.post', lambda *a, **k: pytest.fail('Missing owner credential must not fall back to server credentials'))
    with pytest.raises(SpeechError, match='Groq'): store.transcribe('alice', wav())
    assert VoiceSpeech().key('alice', 'google')
    store.configure('alice', cortex_settings('', engine='device', credentials={'groq': 'test-only-groq-api-key-1234567890'}, remove_credentials=['google']))
    assert store.view('alice')['configured'] and not store.view('alice')['credentials']['google']


@pytest.mark.parametrize('bad', [{'speech_provider': []}, {'transcription_provider': 'custom'}, {'voice': {}},
                                  {'transcription_model': 'unsupported'}, {'speaking_rate': True}, {'speaking_rate': 8},
                                  {'credentials': {'groq': 123}}, {'remove_credentials': [None]}])
def test_invalid_provider_settings_are_rejected_without_mutation(client, google_key, bad):
    store = VoiceSpeech(); original = store.configure('alice', cortex_settings(google_key))
    data = cortex_settings(google_key); data.update(bad)
    result = client.post('/api/connections/voice/settings', headers={'x-owner': 'alice'}, json=data)
    assert result.status_code == 400, result.text
    assert store.view('alice') == original


def test_google_credential_urls_are_never_followed(client, monkeypatch, google_key):
    info = json.loads(google_key); info['token_uri'] = 'http://169.254.169.254/credentials'
    monkeypatch.setattr('voice_speech.requests.post', lambda *a, **k: pytest.fail('Never follow credential-supplied URLs'))
    result = client.post('/api/connections/voice/settings', headers={'x-owner': 'alice'}, json=cortex_settings(json.dumps(info)))
    assert result.status_code == 400 and 'private_key' not in result.text
    assert not VoiceSpeech().view('alice')['credentials']['google']


@pytest.mark.parametrize('payload', [b'{"audioContent":"invalid!"}', b'{"audioContent":"eA=="}', b'{"audioContent":null}'])
def test_bad_google_audio_fails_closed(client, monkeypatch, google_key, payload):
    store = VoiceSpeech(); store.configure('alice', cortex_settings(google_key))
    response = ProviderResponse(payload)
    monkeypatch.setattr('voice_speech_providers.google_headers', lambda key: {'Authorization': 'Bearer test-token'})
    monkeypatch.setattr('voice_speech.requests.post', lambda *a, **k: response)
    with pytest.raises(SpeechError, match='unreadable audio'): store.speak('alice', 'Hello.')
    assert response.closed


def test_provider_quota_is_distinguished_from_temporary_throttle_and_redacted(client, monkeypatch):
    store = VoiceSpeech(); store.configure('alice', {'engine': 'openai', 'voice': 'cedar', 'api_key': 'test-only-openai-key-1234567890'})
    responses = []
    def provider(*a, **k):
        response = ProviderResponse(json.dumps({'error': {'code': 'insufficient_quota', 'message': 'sensitive-provider-debug'}}).encode())
        response.status_code = 429; responses.append(response); return response
    monkeypatch.setattr('voice_speech.requests.post', provider)
    with pytest.raises(SpeechError, match='waiting will not restore') as error: store.speak('alice', 'Hello.')
    assert 'sensitive-provider-debug' not in str(error.value)
    assert len(responses) == 1 and responses[0].closed
    response = ProviderResponse(b'{"error":{"code":"rate_limit_exceeded"}}'); response.status_code = 429
    response.headers = {'Retry-After': '30'}
    monkeypatch.setattr('voice_speech.requests.post', lambda *a, **k: response)
    with pytest.raises(SpeechError, match='at least 30 seconds'): store.speak('alice', 'Hello.')


def test_google_oauth_failure_is_bounded_redacted_and_not_replayed(client, monkeypatch, google_key):
    store = VoiceSpeech(); store.configure('alice', cortex_settings(google_key))
    calls = []; response = ProviderResponse(b'{"error":"invalid_grant","error_description":"private provider debug"}'); response.status_code = 400
    def provider(url, **kwargs): calls.append(url); return response
    monkeypatch.setattr('voice_speech.requests.post', provider)
    with pytest.raises(SpeechError, match='authentication failed') as error: store.speak('alice', 'Hello.')
    assert 'private provider debug' not in str(error.value)
    assert calls == ['https://oauth2.googleapis.com/token'] and response.closed
