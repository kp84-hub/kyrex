// Local WASM keyword spotting. No microphone samples leave this worker.
let kws, stream, vad, enabled = false, speechFilter = 'silero', samplesSeen = 0, lastSpeech = -16000;
const keywords = '▁K A I RE X :1.0 @KYREX\n';
function configureFilter(value) {
  speechFilter = value === 'off' ? 'off' : 'silero';
  samplesSeen = 0; lastSpeech = -16000;
  vad?.reset();
  if (speechFilter === 'silero' && kws && !vad) {
    vad = createWakeVad(Module, { sileroVad: { model: './silero_vad.onnx', threshold: 0.5,
      minSilenceDuration: 0.1, minSpeechDuration: 0.096, maxSpeechDuration: 5, windowSize: 512 },
      sampleRate: 16000, numThreads: 1, provider: 'cpu', debug: 0, bufferSizeInSeconds: 8 });
    if (!vad.handle) throw new Error('Speech filter could not initialize.');
  }
}
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
        maxActivePaths: 32, numTrailingBlanks: 1, keywordsScore: 1.0, keywordsThreshold: 0.19,
        keywords: '', keywordsBuf: keywords, keywordsBufSize: new TextEncoder().encode(keywords).length,
      });
      if (!kws.handle) throw new Error();
      configureFilter(speechFilter);
      stream = kws.createStream(); enabled = true; self.postMessage({ type: 'ready' });
    } catch { self.postMessage({ type: 'error', error: 'Local wake detection is unavailable. Try Talk now.' }); }
  },
};
self.onmessage = event => {
  if (event.data.type === 'configure') {
    try { configureFilter(event.data.speechFilter); }
    catch { self.postMessage({ type: 'error', error: 'Local speech filtering could not load. Choose Off in Voice settings or try Talk.' }); }
    return;
  }
  if (event.data.type === 'pause') { enabled = false; return; }
  if (event.data.type === 'resume') { if (stream) { stream.free(); stream = kws.createStream(); configureFilter(speechFilter); enabled = true; } return; }
  if (event.data.type === 'samples' && enabled && kws) {
    try {
      samplesSeen += event.data.samples.length;
      if (speechFilter === 'silero') {
        vad.acceptWaveform(event.data.samples);
        if (vad.isDetected()) lastSpeech = samplesSeen;
        vad.clear(); // Keep completed speech segments from accumulating.
      }
      // Preserve every keyword sample, including quiet word-initial consonants.
      // Only acceptance is gated, with a 500 ms tail for decoder latency.
      stream.acceptWaveform(16000, event.data.samples);
      while (kws.isReady(stream)) {
        kws.decode(stream);
        if (kws.getResult(stream).keyword) {
          if (speechFilter === 'off' || samplesSeen - lastSpeech < 8000) {
            enabled = false; self.postMessage({ type: 'wake' }); break;
          }
          kws.reset(stream); self.postMessage({ type: 'filtered' });
        }
      }
      self.postMessage({ type: 'accepted' });
    } catch { self.postMessage({ type: 'error', error: 'Wake detection stopped. Try Talk now.' }); enabled = false; }
  } else if (event.data.type === 'samples') self.postMessage({ type: 'accepted' });
};
try { importScripts('/voice-wake/sherpa-onnx-kws.js', '/voice-wake/sherpa-onnx-vad.js', '/voice-wake/sherpa-onnx-wasm-kws-main.js'); }
catch { self.postMessage({ type: 'error', error: 'Wake files could not load. Try Talk now or reopen Kyrex Chat.' }); }
