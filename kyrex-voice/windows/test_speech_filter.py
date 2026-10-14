import numpy as np
import sherpa_onnx
from speech_filter import WakeSpeechFilter


def test_noise_rejected_and_speech_tail_expires_without_accumulating_segments(monkeypatch):
    class Vad:
        speech = False
        segments = 0
        def __init__(self, *_args, **_kwargs): pass
        def reset(self): self.speech = False; self.segments = 0
        def accept_waveform(self, samples): self.segments += 1
        def is_speech_detected(self): return self.speech
        def empty(self): return self.segments == 0
        def pop(self): self.segments -= 1
    monkeypatch.setattr(sherpa_onnx, 'VoiceActivityDetector', Vad)
    gate = WakeSpeechFilter(True, 'unused-test-only-model')
    gate.accept_samples(np.zeros(1600, dtype=np.float32)); assert not gate.accepts_match()
    gate.vad.speech = True
    gate.accept_samples(np.ones(1600, dtype=np.float32)); assert gate.accepts_match()
    gate.vad.speech = False
    gate.accept_samples(np.zeros(7500, dtype=np.float32)); assert gate.accepts_match()
    gate.accept_samples(np.zeros(500, dtype=np.float32)); assert not gate.accepts_match()
    assert gate.vad.empty()
    gate.reset(); assert not gate.accepts_match()


def test_off_does_not_initialize_a_speech_model(monkeypatch):
    def unexpected(*_args, **_kwargs): raise AssertionError('Off initialized VAD')
    monkeypatch.setattr(sherpa_onnx, 'VoiceActivityDetector', unexpected)
    gate = WakeSpeechFilter(False, 'unused-test-only-model')
    gate.accept_samples(np.zeros(48000, dtype=np.float32))
    assert gate.accepts_match()
