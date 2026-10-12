"""Exercise the shipped wake model with synthetic positives and negatives.

This verifies the exact encoded keyword/model combination. It is not a
measurement of accuracy, power consumption or real-room false positives.
"""
import tempfile
import wave
from pathlib import Path
import numpy as np
import sherpa_onnx
import soundfile as sf
from piper import PiperVoice, SynthesisConfig
from prepare_assets import archive

# Neural speech avoids the formant artifacts of espeak, which this model does
# not reliably recognize at faster speeds. These are smoke checks, not an
# accuracy benchmark. Neither Piper nor its voice is included in the APK.
voice_url = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx'
model = archive('piper-amy.onnx', voice_url,
    'a5a91abb7de0f104358a25aded480ddacf1ff0762886325886ec406a2e86aab3')
config = archive('piper-amy.onnx.json', voice_url + '.json',
    '2250a9a605b8dc35a116717fadc5056695dd809e34a15d02f72a0f52d53d3ebb')
voice = PiperVoice.load(str(model), config_path=str(config))

root = Path(__file__).resolve().parent / 'app/src/main/assets/wake'
cases = [('hey kai rex', 1.0, True), ('hey kai rex', 0.8, True), ('hey kai rex', 1.2, True),
         ('today is monday', 1.0, False), ('hey there', 1.0, False),
         ('what is next on my calendar', 1.0, False), ('rex is sleeping', 1.0, False),
         ('hey kyle', 1.0, False), ('hey alex', 1.0, False), ('kai rex', 1.0, False), ('hey my friend', 1.0, False)]
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
    for threshold in [0.25, 0.15]:
        kws = sherpa_onnx.KeywordSpotter(tokens=str(root / 'tokens.txt'),
            encoder=str(root / 'encoder.onnx'), decoder=str(root / 'decoder.onnx'),
            joiner=str(root / 'joiner.onnx'), keywords_file=str(root / 'keywords.txt'),
            num_threads=1, keywords_threshold=threshold, keywords_score=1.5, num_trailing_blanks=2)
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
            if matched != expected:
                raise SystemExit(f'Wake model did not meet synthetic case {index + 1} at threshold {threshold}.')
print('Both wake modes passed synthetic checks; actual-phone validation remains required.')
