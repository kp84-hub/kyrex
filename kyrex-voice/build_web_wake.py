"""Reproducibly compile the local browser wake engine; run inside emsdk 4.0.23."""
import hashlib
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'kyrex-voice' / 'build' / 'web-wake'
OUT = ROOT / 'kyrex-chat' / 'public' / 'voice-wake'
SOURCE_SHA = 'b0374cc56dbc186d442ae73d5de743bb092470b640c4c50ce7b029044c0c4fa8'
MODEL_SHA = 'f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a'
MODEL = 'sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01'


def download(name, url, expected):
    path = WORK / name
    WORK.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with urllib.request.urlopen(url, timeout=90) as src, path.open('wb') as dst:
            shutil.copyfileobj(src, dst)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        path.unlink()
        raise ValueError('Wake archive checksum mismatch')
    return path


def main():
    source = download('source.tar.gz', 'https://codeload.github.com/k2-fsa/sherpa-onnx/tar.gz/v1.13.8', SOURCE_SHA)
    model = download('english.tar.bz2', f'https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL}.tar.bz2', MODEL_SHA)
    with tarfile.open(source) as archive:
        archive.extractall(WORK, filter='data')
    engine = WORK / 'sherpa-onnx-1.13.8'
    assets = engine / 'wasm/kws/assets'
    assets.mkdir(parents=True, exist_ok=True)
    with tarfile.open(model) as archive:
        for destination, member in [
            ('encoder-epoch-12-avg-2-chunk-16-left-64.onnx', 'encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx'),
            ('decoder-epoch-12-avg-2-chunk-16-left-64.onnx', 'decoder-epoch-12-avg-2-chunk-16-left-64.onnx'),
            ('joiner-epoch-12-avg-2-chunk-16-left-64.onnx', 'joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx'),
            ('tokens.txt', 'tokens.txt')]:
            with archive.extractfile(f'{MODEL}/{member}') as src, (assets / destination).open('wb') as dst:
                shutil.copyfileobj(src, dst)
    script = engine / 'build-wasm-simd-kws.sh'
    script.write_text(script.read_text().replace('make -j8', 'make -j2'))
    subprocess.run(['bash', str(script)], cwd=engine, check=True)
    OUT.mkdir(parents=True, exist_ok=True)
    for name in ('sherpa-onnx-wasm-kws-main.js', 'sherpa-onnx-wasm-kws-main.wasm',
                 'sherpa-onnx-wasm-kws-main.data', 'sherpa-onnx-kws.js'):
        shutil.copyfile(engine / 'build-wasm-simd-kws/install/bin/wasm' / name, OUT / name)
    shutil.copyfile(engine / 'LICENSE', OUT / 'SHERPA-LICENSE.txt')
    # C++/ONNX dependencies carry their notices in the native distribution.
    shutil.copyfile(ROOT / 'kyrex-voice-android/app/src/main/assets/licenses/onnxruntime-MIT.txt', OUT / 'ONNX-LICENSE.txt')
    shutil.copyfile(ROOT / 'kyrex-voice-android/app/src/main/assets/licenses/onnxruntime-ThirdPartyNotices.txt', OUT / 'ONNX-NOTICES.txt')
    print('Browser wake runtime compiled with the pinned English model.')


if __name__ == '__main__':
    main()
