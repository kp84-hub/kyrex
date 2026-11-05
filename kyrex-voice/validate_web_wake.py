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
from prepare_assets import archive, MODEL, MODEL_SHA
import tarfile
import sentencepiece as spm

url = 'https://huggingface.co/rhasspy/piper-voices/resolve/c10ece1aade47bb51c153c893d14e5bf8e5b7117/en/en_US/lessac/medium/en_US-lessac-medium.onnx'
model = archive('piper-lessac.onnx', url, '5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f')
config = archive('piper-lessac.onnx.json', url + '.json', 'efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0')
voice = PiperVoice.load(str(model), config_path=str(config))
# Use this exact model's tokenizer when diagnosing pronunciation coverage.
# The runtime still uses fixed, reviewed paths and never transcribes wake audio.
model_archive = archive('wake-english.tar.bz2',
                        f'https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL}.tar.bz2',
                        MODEL_SHA)
with tarfile.open(model_archive) as source:
    bpe = spm.SentencePieceProcessor(model_proto=source.extractfile(f'{MODEL}/bpe.model').read())
worker = (ROOT / 'kyrex-chat/public/voice-wake-worker.js').read_text()
for name in ('KYREX', 'KAIREX', 'KAI REX', 'KEYREX', 'KEY REX', 'KEE REX', 'KYRICKS', 'KIRRICKS'):
    encoded = " ".join(bpe.encode(name, out_type=str))
    threshold = ' #0.25' if name in ('KYRICKS', 'KIRRICKS') else ''
    assert f"'{encoded} :1.0{threshold} @KYREX'" in worker, f'Missing model tokenization for {name}'
    print(f'Wake BPE {name}: {encoded}', flush=True)
# None marks a known near-name confusion, reported rather than counted as a pass.
# Lessac medium preserves word-initial consonants more clearly in these fixtures.
cases = [(text, speed, True)
         for text in ('kai rex', 'key rex', 'hey kai rex', 'hey key rex')
         for speed in (0.55, 0.65, 0.8, 1.0, 1.2)]
cases += [('kee rex', 1.0, True)]
# Piper length_scale below 1 means FASTER speech. Check normal and quick
# unrelated speech too; more forgiving detection must not just trigger on names.
cases += [(text, speed, False)
          for text in ('today is monday', 'hey there', 'what is next on my calendar',
                       'rex is sleeping', 'hey kyle', 'hey alex', 'kai', 'hey my friend',
                       'rex', 'pie racks', 'Pyrex', 'the car is red', 'tell me the price',
                       'get directions', 'text Eric', 'hey Rick', 'Kyrie', 'Kirk',
                       'hey Siri', 'keep receipts', 'car wreck', 'car wrecks')
          for speed in (0.8, 1.0)]
cases += [('Kyle Rex', 1.0, None)]
with tempfile.TemporaryDirectory(prefix='kyrex-wasm-check-') as directory:
    out = Path(directory)
    for index, (text, speed, expected) in enumerate(cases):
        wav = out / f'{index}.wav'
        with wave.open(str(wav), 'wb') as target:
            voice.synthesize_wav(text, target, SynthesisConfig(length_scale=speed, noise_scale=0, noise_w_scale=0))
        samples, rate = sf.read(wav, dtype='float32')
        samples = np.interp(np.arange(0, len(samples), rate / 16000), np.arange(len(samples)), samples).astype(np.float32)
        np.concatenate([np.zeros(8000, dtype=np.float32), samples, np.zeros(16000, dtype=np.float32)]).tofile(out / f'{index}.f32')
    (out / 'cases.json').write_text(json.dumps([
        {'index': i, 'expected': e, 'label': f'{text} (length_scale={speed})',
         'chunk': 341 if speed < 0.8 else 1600}
        for i, (text, speed, e) in enumerate(cases)]))
    subprocess.run(['node', str(ROOT / 'kyrex-voice/validate_web_wake.cjs'), directory], check=True)
