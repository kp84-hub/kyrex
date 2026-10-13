"""Local Silero confirmation for wake matches; never trims keyword input."""
class WakeSpeechFilter:
    def __init__(self, enabled, model):
        self.enabled = enabled
        self.vad = None
        if enabled:
            import sherpa_onnx
            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = str(model)
            config.silero_vad.threshold = 0.5
            config.silero_vad.min_silence_duration = 0.1
            config.silero_vad.min_speech_duration = 0.096
            config.silero_vad.max_speech_duration = 5
            config.num_threads = 1
            self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=8)
        self.reset()

    def reset(self):
        self.samples_seen = 0
        self.last_speech = -16000
        if self.vad is not None: self.vad.reset()

    def accept_samples(self, samples):
        self.samples_seen += len(samples)
        if self.vad is not None:
            self.vad.accept_waveform(samples)
            if self.vad.is_speech_detected(): self.last_speech = self.samples_seen
            while not self.vad.empty(): self.vad.pop()

    def accepts_match(self):
        return not self.enabled or self.samples_seen - self.last_speech < 8000
