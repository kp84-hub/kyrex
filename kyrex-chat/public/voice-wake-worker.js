// Local WASM keyword spotting. No microphone samples leave this worker.
let kws, stream, enabled = false, awaiting = false;
const keywords = '▁HE Y ▁K Y RE X :2.0 @HEY_KYREX\n▁HE Y ▁K A I ▁RE X :2.0 @HEY_KYREX\n';
var Module = {
  locateFile: file => '/voice-wake/' + file,
  print: () => {}, printErr: () => {},
  onAbort: () => self.postMessage({ type: 'error', error: 'The local wake engine could not load. Try Talk now.' }),
  onRuntimeInitialized: () => {
    try {
      kws = createKws(Module, {
        featConfig: { samplingRate: 16000, featureDim: 80 },
        modelConfig: { transducer: {
          encoder: './encoder-epoch-12-avg-2-chunk-16-left-64.onnx',
          decoder: './decoder-epoch-12-avg-2-chunk-16-left-64.onnx',
          joiner: './joiner-epoch-12-avg-2-chunk-16-left-64.onnx',
        }, tokens: './tokens.txt', provider: 'cpu', modelType: 'zipformer2', numThreads: 1, debug: 0 },
        maxActivePaths: 4, numTrailingBlanks: 1, keywordsScore: 2.0, keywordsThreshold: 0.15,
        keywords: '', keywordsBuf: keywords, keywordsBufSize: new TextEncoder().encode(keywords).length,
      });
      if (!kws.handle) throw new Error();
      stream = kws.createStream(); enabled = true; self.postMessage({ type: 'ready' });
    } catch { self.postMessage({ type: 'error', error: 'Local wake detection is unavailable. Try Talk now.' }); }
  },
};
self.onmessage = event => {
  if (event.data.type === 'pause') { enabled = false; return; }
  if (event.data.type === 'resume') { if (stream) { stream.free(); stream = kws.createStream(); enabled = true; } return; }
  if (event.data.type === 'samples' && enabled && kws) {
    try {
      stream.acceptWaveform(16000, event.data.samples);
      while (kws.isReady(stream)) {
        kws.decode(stream);
        if (kws.getResult(stream).keyword) { enabled = false; self.postMessage({ type: 'wake' }); break; }
      }
      self.postMessage({ type: 'accepted' });
    } catch { self.postMessage({ type: 'error', error: 'Wake detection stopped. Try Talk now.' }); enabled = false; }
  } else if (event.data.type === 'samples') self.postMessage({ type: 'accepted' });
};
try { importScripts('/voice-wake/sherpa-onnx-kws.js', '/voice-wake/sherpa-onnx-wasm-kws-main.js'); }
catch { self.postMessage({ type: 'error', error: 'Wake files could not load. Try Talk now or reopen Kyrex Chat.' }); }
