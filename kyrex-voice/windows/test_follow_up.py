"""Companion turns use one lease and never upload a silent follow-up."""
import sys
import threading
from types import SimpleNamespace
import pytest
import companion


@pytest.mark.parametrize('seconds,turns', [(0, 1), (8, 2), (15, 2)])
def test_talk_follow_ups_reuse_session_and_end_on_silence(monkeypatch, seconds, turns):
    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace())
    created = []
    class FakeClient:
        def __init__(self, *_):
            self.claims = 0; self.requests = []; self.transcripts = 0; self.released = False; created.append(self)
        def claim(self): self.claims += 1
        def json(self, path, **kwargs):
            if path == 'session': return {'settings': {'configured': True}}
            if path == 'settings': return {'engine': 'device', 'follow_up_seconds': seconds}
            if path == 'transcribe': self.transcripts += 1; return {'text': 'And tomorrow?'}
        def ask(self, text, stopped): self.requests.append(text); return 'Tomorrow is clear.'
        def stop(self): self.released = True
    monkeypatch.setattr(companion, 'Client', FakeClient)
    class TestController(companion.Controller):
        def heartbeat(self, *_): pass
        def record_command(self, stopped, follow_up_seconds=0, connection=None):
            self.windows.append(follow_up_seconds)
            return [b'\x01\x00' * 3200] if len(self.windows) <= turns else None
        def speak_device(self, reply, stopped): self.replies.append(reply)
    controller = TestController(lambda *_args, **_kwargs: None, SimpleNamespace(load=lambda: {'origin': 'https://chat.kyrex.dev', 'token': 'test-only-token'}))
    controller.windows = []; controller.replies = []
    controller.run(False, threading.Event())
    client = created[0]
    assert client.claims == 1 and client.released
    assert len(client.requests) == client.transcripts == len(controller.replies) == turns
    assert controller.windows == ([0] if seconds == 0 else [0, seconds, seconds])


def test_stop_after_reply_prevents_follow_up_capture(monkeypatch):
    monkeypatch.setitem(sys.modules, 'numpy', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace())
    class FakeClient:
        def __init__(self, *_): pass
        def claim(self): pass
        def json(self, path, **kwargs):
            return {'settings': {'configured': True}} if path == 'session' else {'text': 'Hello', 'engine': 'device', 'follow_up_seconds': 8}
        def ask(self, *_): return 'Hello'
        def stop(self): pass
    monkeypatch.setattr(companion, 'Client', FakeClient)
    controller = companion.Controller(lambda *_args, **_kwargs: None, SimpleNamespace(load=lambda: {'origin': 'https://chat.kyrex.dev', 'token': 'test-only-token'}))
    captures = []
    controller.heartbeat = lambda *_: None
    controller.record_command = lambda *_: captures.append(True) or [b'\x01\x00' * 3200]
    controller.speak_device = lambda reply, stopped: stopped.set()
    controller.run(False, threading.Event())
    assert len(captures) == 1
