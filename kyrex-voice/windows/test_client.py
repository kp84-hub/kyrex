import json
import threading
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import Client, VoiceError, checked_origin, command_wav


class Response:
    status_code = 200
    headers = {'Content-Type': 'audio/wav'}
    def __init__(self, data=None, lines=()): self.data=data; self.lines=lines; self.closed=False
    def json(self): return self.data
    def close(self): self.closed=True
    def iter_lines(self, **kwargs): return iter(self.lines)
    def iter_content(self, size): return iter([b'audio'])


class Transport:
    def __init__(self, response): self.response=response; self.calls=[]
    def request(self, method, url, **kwargs): self.calls.append((method,url,kwargs)); return self.response


def test_native_sends_only_one_text_request_with_lease():
    response=Response(lines=[b'data: '+json.dumps({'type':'done','content':'Your calendar is clear.'}).encode()])
    transport=Transport(response); client=Client('https://chat.kyrex.dev','test-only-native-token',transport); client.lease='test-only-lease'
    assert client.ask('Show my calendar',threading.Event())=='Your calendar is clear.'
    assert len(transport.calls)==1
    _,url,kwargs=transport.calls[0]
    assert url=='https://chat.kyrex.dev/api/voice/chat'
    assert set(kwargs['json'])=={'message','request_id'}
    assert kwargs['headers']=={'Authorization':'Bearer test-only-native-token','X-Voice-Lease':'test-only-lease'}
    assert kwargs['allow_redirects'] is False
    assert response.closed


def test_stop_before_send_and_redirect_failure_never_replay():
    response=Response();response.status_code=302;response.data={'detail':'Voice is unavailable.'}
    transport=Transport(response);client=Client('https://chat.kyrex.dev','test-only-token',transport)
    stopped=threading.Event();stopped.set()
    assert client.ask('Send a message',stopped) is None
    assert not transport.calls
    with pytest.raises(VoiceError): client.ask('Send a message',threading.Event())
    assert len(transport.calls)==1


def test_eof_without_done_reports_saved_request_without_retry():
    transport=Transport(Response(lines=[b'data: {"type":"status"}']))
    with pytest.raises(VoiceError,match='Check voice chat'): Client('https://chat.kyrex.dev','test-only-token',transport).ask('Do a task',threading.Event())
    assert len(transport.calls)==1


@pytest.mark.parametrize('url',['http://chat.kyrex.dev','https://user:password@chat.kyrex.dev','https://chat.kyrex.dev/?token=x','https://chat.kyrex.dev/api'])
def test_origin_validation(url):
    with pytest.raises(VoiceError): checked_origin(url)


def test_command_wav_is_mono_pcm_and_never_includes_wake_frames():
    import io,wave
    raw=command_wav([b'\x01\x00'*1600])
    with wave.open(io.BytesIO(raw)) as wav:
        assert (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes())==(1,2,16000,1600)
