"""Bounded renewal of the unchanged lease; never claim or replay on recovery."""
import threading
from types import SimpleNamespace
import sys
import pytest
import requests
from client import Client, VoiceError, VoiceSessionLost, VoiceConnectionUnavailable
from companion import Controller, ListeningConnection


class Response:
    def __init__(self, code, reason=''):
        self.status_code = code; self.headers = {'X-Voice-Reason': reason}; self.closed = False
    def close(self): self.closed = True
    def json(self): return {'detail': 'untrusted provider diagnostic'}


@pytest.mark.parametrize('code,error', [(408, VoiceConnectionUnavailable), (429, VoiceConnectionUnavailable), (500, VoiceConnectionUnavailable), (503, VoiceConnectionUnavailable), (401, VoiceSessionLost), (403, VoiceSessionLost), (404, VoiceSessionLost), (409, VoiceSessionLost)])
def test_heartbeat_classifies_failures_and_uses_short_timeout(code, error):
    response = Response(code, 'moved'); calls = []
    def request(*args, **kwargs): calls.append((args, kwargs)); return response
    client = Client('https://chat.kyrex.dev', 'test-only-token', SimpleNamespace(request=request))
    client.lease = 'test-only-same-lease'
    with pytest.raises(error) as caught: client.json('heartbeat')
    assert response.closed and len(calls) == 1
    assert 'untrusted' not in str(caught.value)
    assert calls[0][1]['timeout'] == (5, 5)
    assert calls[0][1]['headers']['X-Voice-Lease'] == 'test-only-same-lease'


def test_transport_timeout_is_transient_but_bad_tls_stops():
    def request(*args, **kwargs): raise requests.Timeout()
    client = Client('https://chat.kyrex.dev', 'test-only-token', SimpleNamespace(request=request))
    with pytest.raises(VoiceConnectionUnavailable): client.json('heartbeat')
    def bad_tls(*args, **kwargs): raise requests.exceptions.SSLError()
    client.transport.request = bad_tls
    with pytest.raises(VoiceError, match='secure') as caught: client.json('heartbeat')
    assert not isinstance(caught.value, VoiceConnectionUnavailable)


class ScriptedStop:
    def __init__(self, ticks): self.ticks = ticks; self.ended = False
    def wait(self, duration):
        assert duration == 15
        if not self.ticks: return True
        self.ticks -= 1; return self.ended
    def is_set(self): return self.ended
    def set(self): self.ended = True


def test_transient_failure_pauses_then_renews_same_lease_without_claim(monkeypatch):
    clock = [0.0]; monkeypatch.setattr('companion.time.monotonic', lambda: clock[0])
    connection = ListeningConnection(); phases = []; stop = ScriptedStop(3)
    controller = Controller(lambda message, **extra: phases.append(message), None)
    calls = []
    def json(path):
        assert path == 'heartbeat'; calls.append(path)
        if len(calls) == 1: raise VoiceConnectionUnavailable('offline')
        assert not connection.healthy.is_set()
        assert connection.epoch == 1
        clock[0] = 15.0
        stop.ticks = 0
        return {'expires_at': 60}
    controller.heartbeat(SimpleNamespace(json=json), stop, connection)
    assert len(calls) == 2 and connection.healthy.is_set() and not stop.ended
    assert any('Reconnecting' in message for message in phases)
    assert any('restored' in message for message in phases)


def test_confirmed_takeover_and_revocation_never_recover_or_reclaim():
    for error in (VoiceSessionLost('Listening switched to another voice session.'), VoiceSessionLost('Pairing revoked.')):
        connection = ListeningConnection(); phases = []; stop = ScriptedStop(5); calls = []
        def json(path): calls.append(path); raise error
        Controller(lambda message, **extra: phases.append(message), None).heartbeat(SimpleNamespace(json=json), stop, connection)
        assert stop.ended and not connection.healthy.is_set()
        assert calls == ['heartbeat'] and phases == [str(error)]


def test_long_outage_stops_at_deadline_without_another_request(monkeypatch):
    clock = [0.0]; monkeypatch.setattr('companion.time.monotonic', lambda: clock[0])
    connection = ListeningConnection(); phases = []; stop = ScriptedStop(10); calls = []
    def json(path):
        calls.append(path)
        if len(calls) == 2: clock[0] = 90.0
        raise VoiceConnectionUnavailable('offline')
    Controller(lambda message, **extra: phases.append(message), None).heartbeat(SimpleNamespace(json=json), stop, connection)
    assert stop.ended and not connection.healthy.is_set()
    assert len(calls) == 2 and 'could not recover' in phases[-1]


def test_stop_during_successful_renewal_cannot_restore_listening():
    connection = ListeningConnection(); connection.pause(); phases = []; stop = ScriptedStop(5)
    def json(path): stop.set(); return {'expires_at': 60}
    Controller(lambda message, **extra: phases.append(message), None).heartbeat(SimpleNamespace(json=json), stop, connection)
    assert not connection.healthy.is_set() and phases == []


def test_paused_connection_never_opens_command_microphone(monkeypatch):
    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(RawInputStream=lambda **kwargs: pytest.fail('Paused mic opened')))
    monkeypatch.setitem(sys.modules, 'winsound', SimpleNamespace(Beep=lambda *_: pytest.fail('Paused capture beeped')))
    connection = ListeningConnection(); connection.pause()
    assert Controller(lambda *_args, **_kwargs: None, None).record_command(threading.Event(), connection=connection) is None


def test_connection_loss_discards_partial_recording_and_closes_microphone(monkeypatch):
    import numpy
    connection = ListeningConnection(); closed = []; reads = []
    class Mic:
        def __enter__(self): return self
        def __exit__(self, *_): closed.append(True)
        def read(self, count):
            reads.append(count); connection.pause()
            return (numpy.full(count, 3000, dtype=numpy.int16).tobytes(), False)
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(RawInputStream=lambda **kwargs: Mic()))
    monkeypatch.setitem(sys.modules, 'winsound', SimpleNamespace(Beep=lambda *_: None))
    result = Controller(lambda *_args, **_kwargs: None, None).record_command(threading.Event(), connection=connection)
    assert result is None and reads == [1600] and closed == [True]


def test_recovered_wake_discards_old_frames_without_submitting(monkeypatch):
    import numpy
    import companion
    connection = ListeningConnection(); closes = []; opens = []
    monkeypatch.setattr(companion, 'ListeningConnection', lambda: connection)
    class Mic:
        def __enter__(self): opens.append(True); return self
        def __exit__(self, *_): closes.append(True)
        def read(self, count):
            if len(opens) == 1:
                connection.pause(); connection.healthy.set()
            return (numpy.zeros(count, dtype=numpy.int16).tobytes(), False)
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(RawInputStream=lambda **kwargs: Mic()))
    monkeypatch.setattr(companion, 'spotter', lambda: SimpleNamespace(create_stream=lambda: SimpleNamespace(accept_waveform=lambda *_: None), is_ready=lambda _: False, get_result=lambda _: True))
    calls = []
    class FakeClient:
        def __init__(self, *_): pass
        def json(self, path, **kwargs): calls.append(path); assert path == 'session'; return {'settings': {'configured': True}}
        def claim(self): calls.append('claim')
        def stop(self): calls.append('stop')
    monkeypatch.setattr(companion, 'Client', FakeClient)
    controller = Controller(lambda *_args, **_kwargs: None, SimpleNamespace(load=lambda: {'origin': 'https://chat.kyrex.dev', 'token': 'test-only-token'}))
    controller.heartbeat = lambda *_: None
    captures = []
    def record(stopped, following_up, active_connection): captures.append(True); stopped.set(); return None
    controller.record_command = record
    controller.run(True, threading.Event())
    assert len(opens) == len(closes) == 2 and captures == [True]
    assert calls == ['session', 'claim', 'stop']
