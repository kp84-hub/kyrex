"""Prepare checksum-pinned offline wake assets for the Windows package."""
import hashlib
import shutil
import tarfile
import urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parent
MODEL = 'sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01'
SHA = 'f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a'


def main():
    root = ROOT / 'build'; root.mkdir(parents=True, exist_ok=True)
    archive = root / 'english.tar.bz2'
    if not archive.exists():
        with urllib.request.urlopen(f'https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL}.tar.bz2', timeout=90) as src, archive.open('wb') as dst:
            shutil.copyfileobj(src, dst)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA:
        archive.unlink(); raise ValueError('Wake archive checksum mismatch')
    assets = ROOT / 'wake'; assets.mkdir(exist_ok=True)
    with tarfile.open(archive) as tar:
        for output, member in [('encoder.onnx','encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx'),
                               ('decoder.onnx','decoder-epoch-12-avg-2-chunk-16-left-64.onnx'),
                               ('joiner.onnx','joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx'),('tokens.txt','tokens.txt')]:
            with tar.extractfile(f'{MODEL}/{member}') as src, (assets/output).open('wb') as dst:
                shutil.copyfileobj(src,dst)
    android = ROOT.parents[1] / 'kyrex-voice-android'
    shutil.copyfile(android / 'app/src/main/assets/wake/keywords.txt', assets / 'keywords.txt')
    shutil.copyfile(android / 'SHERPA-LICENSE', assets / 'SHERPA-LICENSE.txt')
    for name in ('onnxruntime-MIT.txt','onnxruntime-ThirdPartyNotices.txt'):
        shutil.copyfile(android / 'app/src/main/assets/licenses' / name, assets / name)
    print('Verified Windows wake assets prepared.')


if __name__ == '__main__': main()
