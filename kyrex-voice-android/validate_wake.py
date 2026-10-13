"""Exercise the shipped wake model with synthetic positives and negatives.

This verifies the exact encoded keyword/model combination. It is not a
measurement of accuracy, power consumption or real-room false positives.
"""
import tempfile
import wave
from pathlib import Path
import os
import sys
# Disable telemetry before ONNX imports, including runtime initialization events.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
import numpy as np
import sherpa_onnx
import soundfile as sf
import onnxruntime as ort
# Synthetic test speech stays local, including inference-library diagnostics.
ort.disable_telemetry_events()
from piper import PiperVoice, SynthesisConfig
from prepare_assets import archive
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'kyrex-voice/windows'))
from speech_filter import WakeSpeechFilter

# Neural speech provides repeatable standalone-word and unrelated-speech checks. These are smoke checks, not an
# accuracy benchmark. Neither Piper nor its fixture voice is included in the APK.
voice_url = 'https://huggingface.co/rhasspy/piper-voices/resolve/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/lessac/medium/en_US-lessac-medium.onnx'
model = archive('piper-lessac.onnx', voice_url,
    '5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f')
config = archive('piper-lessac.onnx.json', voice_url + '.json',
    'efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0')
voice = PiperVoice.load(str(model), config_path=str(config))

root = Path(__file__).resolve().parent / 'app/src/main/assets/wake'
# None marks a known near-name confusion, reported rather than counted as a pass.
# Lessac medium preserves word-initial consonants more clearly in these fixtures.
cases = [('kai rex', 1.0, True), ('kai rex', 0.8, True), ('kai rex', 1.2, True),
         ('hey kai rex', 1.0, True),
         ('today is monday', 1.0, False), ('hey there', 1.0, False),
         ('what is next on my calendar', 1.0, False), ('rex is sleeping', 1.0, False),
         ('hey kyle', 1.0, False), ('hey alex', 1.0, False), ('kai', 1.0, False), ('hey my friend', 1.0, False), ('rex', 1.0, False), ('pie racks', 1.0, False), ('Pyrex', 1.0, False),
         ('Kyle Rex', 1.0, None), ('the car is red', 1.0, False)]
with tempfile.TemporaryDirectory() as directory:
    fixtures = []
    for index, (text, speed, expected) in enumerate(cases):
        wav = Path(directory) / f'{index}.wav'
        with wave.open(str(wav), 'wb') as output:
            # Piper otherwise samples new acoustic noise on every CI run.
            # Fixed synthesis makes this a repeatable model regression check.
            voice.synthesize_wav(text, output, SynthesisConfig(
                length_scale=speed, noise_scale=0, noise_w_scale=0))
        samples, rate = sf.read(wav, dtype='float32')
        samples = np.interp(np.arange(0, len(samples), rate / 16000), np.arange(len(samples)), samples).astype(np.float32)
        # A continuously running detector has already received ambient frames.
        samples = np.concatenate([np.zeros(8000, dtype=np.float32), samples, np.zeros(16000, dtype=np.float32)])
        fixtures.append((samples, expected))
    # Repeatable signal tests, not recordings from a real room or microphone.
    random = np.random.default_rng(20261013)
    clean = fixtures[0][0]
    noise = random.normal(0, 1, len(clean)).astype(np.float32)
    rms = np.sqrt(np.mean(clean * clean))
    fixtures.extend([(clean * 0.2, True),
        (clean + noise * rms / 10, True),  # White noise at 20 dB SNR.
        (clean + 0.025 * np.sin(np.arange(len(clean)) * 2 * np.pi * 80 / 16000).astype(np.float32), True),
        (np.zeros(48000, dtype=np.float32), False),
        (random.normal(0, 0.025, 48000).astype(np.float32), False),
        (0.025 * np.sin(np.arange(48000) * 2 * np.pi * 80 / 16000).astype(np.float32), False)])
    for threshold in [0.19, 0.15]:
        kws = sherpa_onnx.KeywordSpotter(tokens=str(root / 'tokens.txt'),
            encoder=str(root / 'encoder.onnx'), decoder=str(root / 'decoder.onnx'),
            joiner=str(root / 'joiner.onnx'), keywords_file=str(root / 'keywords.txt'),
            num_threads=1, max_active_paths=32, keywords_threshold=threshold, keywords_score=1.0, num_trailing_blanks=1)
        for filtering in (False, True):
            speech = WakeSpeechFilter(filtering, root / 'silero_vad.onnx')
            for index, (samples, expected) in enumerate(fixtures):
                stream = kws.create_stream(); speech.reset()
                matched = False
                for offset in range(0, len(samples), 1600):
                    pcm = samples[offset:offset + 1600]
                    speech.accept_samples(pcm)
                    if filtering and index >= 20 and speech.accepts_match():
                        raise SystemExit(f'Noise-only case {index + 1} was classified as speech.')
                    stream.accept_waveform(16000, pcm)
                    while kws.is_ready(stream):
                        kws.decode_stream(stream)
                        if kws.get_result(stream):
                            if speech.accepts_match(): matched = True; break
                            kws.reset_stream(stream)
                    if matched: break
                print(f'Threshold {threshold}, filter={filtering}, synthetic case {index + 1}: wake={matched}, expected={expected}', flush=True)
                if expected is not None and matched != expected:
                    raise SystemExit(f'Wake model did not meet synthetic case {index + 1} at threshold {threshold} (filter={filtering}).')
print('Bare Kyrex and unrelated-speech checks passed in both modes; near-name confusions were reported separately. Actual-phone validation remains required.')
