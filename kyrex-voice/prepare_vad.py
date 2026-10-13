"""One checksum-pinned Silero v5 speech model for all local wake listeners."""
import hashlib
import shutil
import urllib.request
from pathlib import Path

VAD_URL = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad_v5.onnx'
VAD_SHA = '6b99cbfd39246b6706f98ec13c7c50c6b299181f2474fa05cbc8046acc274396'


def prepare_vad(cache, destination):
    path = Path(cache) / 'silero-vad-v5.onnx'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with urllib.request.urlopen(VAD_URL, timeout=60) as source, path.open('wb') as target:
            shutil.copyfileobj(source, target)
    if hashlib.sha256(path.read_bytes()).hexdigest() != VAD_SHA:
        path.unlink()
        raise ValueError('Silero speech model checksum mismatch')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, destination)
    return destination
