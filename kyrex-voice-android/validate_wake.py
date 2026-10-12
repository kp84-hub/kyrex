"""Exercise the shipped wake model with synthetic positives and negatives.

This verifies the exact encoded keyword/model combination. It is not a
measurement of accuracy, power consumption or real-room false positives.
"""
import subprocess
import tempfile
from pathlib import Path
import numpy as np
import sherpa_onnx
import soundfile as sf

root = Path(__file__).resolve().parent / 'app/src/main/assets/wake'
kws = sherpa_onnx.KeywordSpotter(tokens=str(root / 'tokens.txt'),
    encoder=str(root / 'encoder.onnx'), decoder=str(root / 'decoder.onnx'),
    joiner=str(root / 'joiner.onnx'), keywords_file=str(root / 'keywords.txt'),
    num_threads=1, keywords_threshold=0.35, keywords_score=1.5)
cases = [('hey kai rex', True), ('hey kih rex', True), ('today is monday', False),
         ('hey there', False), ('what is next on my calendar', False), ('rex is sleeping', False)]
with tempfile.TemporaryDirectory() as directory:
    for index, (text, expected) in enumerate(cases):
        wav = Path(directory) / f'{index}.wav'
        subprocess.run(['espeak-ng', '-v', 'en-us', '-s', '150', '-w', str(wav), text], check=True)
        samples, rate = sf.read(wav, dtype='float32')
        samples = np.interp(np.arange(0, len(samples), rate / 16000), np.arange(len(samples)), samples).astype(np.float32)
        samples = np.concatenate([samples, np.zeros(8000, dtype=np.float32)])
        stream = kws.create_stream()
        matched = False
        for offset in range(0, len(samples), 1600):
            stream.accept_waveform(16000, samples[offset:offset + 1600])
            while kws.is_ready(stream):
                kws.decode_stream(stream)
            if kws.get_result(stream):
                matched = True
                break
        print(f'Synthetic case {index + 1}: wake={matched}, expected={expected}')
        if matched != expected:
            raise SystemExit(f'Wake model did not meet synthetic case {index + 1}.')
print('Shipped wake model synthetic checks passed; actual-phone validation remains required.')
