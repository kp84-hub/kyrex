"""Deterministic synthetic fixtures for the real WASM worker regression check."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import wave
import numpy as np
import soundfile as sf
from piper import PiperVoice, SynthesisConfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'kyrex-voice-android'))
from prepare_assets import archive

url = 'https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx'
model = archive('piper-amy.onnx', url, 'a5a91abb7de0f104358a25aded480ddacf1ff0762886325886ec406a2e86aab3')
config = archive('piper-amy.onnx.json', url + '.json', '2250a9a605b8dc35a116717fadc5056695dd809e34a15d02f72a0f52d53d3ebb')
voice = PiperVoice.load(str(model), config_path=str(config))
cases = [('hey kai rex', 1.0, True), ('hey kai rex', 0.8, True), ('hey kai rex', 1.2, True),
         ('today is monday', 1.0, False), ('hey there', 1.0, False),
         ('what is next on my calendar', 1.0, False), ('rex is sleeping', 1.0, False),
         ('hey kyle', 1.0, False), ('hey alex', 1.0, False), ('kai rex', 1.0, False), ('hey my friend', 1.0, False)]
with tempfile.TemporaryDirectory(prefix='kyrex-wasm-check-') as directory:
    out = Path(directory)
    for index, (text, speed, expected) in enumerate(cases):
        wav = out / f'{index}.wav'
        with wave.open(str(wav), 'wb') as target:
            voice.synthesize_wav(text, target, SynthesisConfig(length_scale=speed, noise_scale=0, noise_w_scale=0))
        samples, rate = sf.read(wav, dtype='float32')
        samples = np.interp(np.arange(0, len(samples), rate / 16000), np.arange(len(samples)), samples).astype(np.float32)
        np.concatenate([np.zeros(8000, dtype=np.float32), samples, np.zeros(16000, dtype=np.float32)]).tofile(out / f'{index}.f32')
    (out / 'cases.json').write_text(json.dumps([{'index': i, 'expected': e} for i, (_, _, e) in enumerate(cases)]))
    subprocess.run(['node', str(ROOT / 'kyrex-voice/validate_web_wake.cjs'), directory], check=True)
