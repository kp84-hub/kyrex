"""Exercise the shipped wake model with synthetic positives and negatives.

This verifies the exact encoded keyword/model combination. It is not a
measurement of accuracy, power consumption or real-room false positives.
"""
import tempfile
import wave
from pathlib import Path
import os
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
    for threshold in [0.19, 0.15]:
        kws = sherpa_onnx.KeywordSpotter(tokens=str(root / 'tokens.txt'),
            encoder=str(root / 'encoder.onnx'), decoder=str(root / 'decoder.onnx'),
            joiner=str(root / 'joiner.onnx'), keywords_file=str(root / 'keywords.txt'),
            num_threads=1, max_active_paths=32, keywords_threshold=threshold, keywords_score=1.0, num_trailing_blanks=1)
        for index, (samples, expected) in enumerate(fixtures):
            stream = kws.create_stream()
            matched = False
            for offset in range(0, len(samples), 1600):
                stream.accept_waveform(16000, samples[offset:offset + 1600])
                while kws.is_ready(stream):
                    kws.decode_stream(stream)
                    if kws.get_result(stream):
                        matched = True
                        break
                if matched:
                    break
            print(f'Threshold {threshold}, synthetic case {index + 1}: wake={matched}, expected={expected}', flush=True)
            if expected is not None and matched != expected:
                raise SystemExit(f'Wake model did not meet synthetic case {index + 1} at threshold {threshold}.')
print('Bare Kyrex and unrelated-speech checks passed in both modes; near-name confusions were reported separately. Actual-phone validation remains required.')
