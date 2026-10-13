"""Deterministic synthetic fixtures for the real WASM worker regression check."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import wave
import os
# Disable telemetry before ONNX imports, including runtime initialization events.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
import numpy as np
import soundfile as sf
import onnxruntime as ort
# Synthetic test speech stays local, including inference-library diagnostics.
ort.disable_telemetry_events()
from piper import PiperVoice, SynthesisConfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'kyrex-voice-android'))
from prepare_assets import archive

url = 'https://huggingface.co/rhasspy/piper-voices/resolve/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/lessac/medium/en_US-lessac-medium.onnx'
model = archive('piper-lessac.onnx', url, '5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f')
config = archive('piper-lessac.onnx.json', url + '.json', 'efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0')
voice = PiperVoice.load(str(model), config_path=str(config))
# None marks a known near-name confusion, reported rather than counted as a pass.
# Lessac medium preserves word-initial consonants more clearly in these fixtures.
cases = [('kai rex', 1.0, True), ('kai rex', 0.8, True), ('kai rex', 1.2, True),
         ('hey kai rex', 1.0, True),
         ('today is monday', 1.0, False), ('hey there', 1.0, False),
         ('what is next on my calendar', 1.0, False), ('rex is sleeping', 1.0, False),
         ('hey kyle', 1.0, False), ('hey alex', 1.0, False), ('kai', 1.0, False), ('hey my friend', 1.0, False), ('rex', 1.0, False), ('pie racks', 1.0, False), ('Pyrex', 1.0, False),
         ('Kyle Rex', 1.0, None), ('the car is red', 1.0, False)]
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
