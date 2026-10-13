"""Fetch fixed, checksum-verified native libraries and offline wake model.

Large generated assets stay out of git; both local and CI APK builds use the
same archive hashes and cherry-pick only named files, never tar paths.
"""
import hashlib
import shutil
import tarfile
import urllib.request
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'kyrex-voice'))
from prepare_vad import prepare_vad
MODEL = 'sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01'
MODEL_SHA = 'f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a'
NATIVE_SHA = '2ff63469a71cb6009aa2e3ed5f4a670f8abdcbe4bb9ffd23776afc792a6b4f44'


def archive(name, url, expected):
    path = ROOT / 'downloads' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with urllib.request.urlopen(url, timeout=90) as src, path.open('wb') as dst:
            shutil.copyfileobj(src, dst)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        path.unlink()
        raise ValueError(f'Archive checksum mismatch: {name}')
    return path


def copy_member(tar, name, destination):
    member = tar.getmember(name)
    if not member.isfile():
        raise ValueError('Expected an archive file')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tar.extractfile(member) as source, destination.open('wb') as target:
        shutil.copyfileobj(source, target)


def main():
    model = archive('wake-english.tar.bz2', f'https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL}.tar.bz2', MODEL_SHA)
    native = archive('native.tar.bz2', 'https://github.com/k2-fsa/sherpa-onnx/releases/download/v1.13.8/sherpa-onnx-v1.13.8-android.tar.bz2', NATIVE_SHA)
    wake = ROOT / 'app/src/main/assets/wake'
    prepare_vad(ROOT / 'downloads', wake / 'silero_vad.onnx')
    with tarfile.open(model) as tar:
        for output, member in [('encoder.onnx', 'encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx'),
                               ('decoder.onnx', 'decoder-epoch-12-avg-2-chunk-16-left-64.onnx'),
                               ('joiner.onnx', 'joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx'), ('tokens.txt', 'tokens.txt')]:
            copy_member(tar, f'{MODEL}/{member}', wake / output)
    with tarfile.open(native) as tar:
        for name in ['libsherpa-onnx-jni.so', 'libonnxruntime.so']:
            copy_member(tar, './jniLibs/arm64-v8a/' + name, ROOT / 'app/src/main/jniLibs/arm64-v8a' / name)
    print('Verified wake model and ARM64 native libraries prepared.')


if __name__ == '__main__':
    main()
