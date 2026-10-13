import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../public/voice-wake-worker.js', import.meta.url), 'utf8');
const events = [], keywordInput = [];
let candidate = false, speech = false, resets = 0, cleared = 0;
const stream = () => ({ acceptWaveform: (rate, samples) => { assert.equal(rate, 16000); keywordInput.push(samples); }, free() {} });
const kws = { handle: 1, createStream: stream, isReady: () => candidate,
  decode() {}, getResult: () => ({ keyword: 'KYREX' }), reset() { candidate = false; resets++; } };
const vad = { handle: 1, reset() { speech = false; }, acceptWaveform() {},
  isDetected: () => speech, clear() { cleared++; } };
const context = { TextEncoder, Float32Array, self: { postMessage: e => events.push(e) },
  createKws: () => kws, createWakeVad: () => vad, importScripts() {} };
vm.createContext(context); vm.runInContext(source, context);
context.Module.onRuntimeInitialized(); assert.equal(events[0].type, 'ready');
const send = data => context.self.onmessage({ data });
const pcm = new Float32Array(1600).fill(0.01);
candidate = true; send({ type: 'samples', samples: pcm });
assert.ok(events.some(e => e.type === 'filtered')); assert.ok(!events.some(e => e.type === 'wake'));
assert.equal(keywordInput[0], pcm, 'Quiet consonants and complete input reach keyword spotting unchanged');
assert.equal(resets, 1); assert.equal(cleared, 1);
speech = true; candidate = true; send({ type: 'samples', samples: pcm });
assert.ok(events.some(e => e.type === 'wake'), 'Speech-confirmed keyword wakes');
send({ type: 'resume' }); events.length = 0; candidate = true;
send({ type: 'samples', samples: pcm });
assert.ok(!events.some(e => e.type === 'wake'), 'Old speech cannot authorize a match after reply/resume');
speech = true; candidate = false; send({ type: 'samples', samples: pcm });
speech = false; send({ type: 'samples', samples: new Float32Array(8000) });
candidate = true; events.length = 0; send({ type: 'samples', samples: pcm });
assert.ok(!events.some(e => e.type === 'wake'), 'Speech confirmation expires after its bounded tail');
send({ type: 'configure', speechFilter: 'off' }); candidate = true;
events.length = 0; send({ type: 'samples', samples: pcm });
assert.ok(events.some(e => e.type === 'wake'), 'Off preserves keyword-only detection');
send({ type: 'pause' }); events.length = 0;
send({ type: 'samples', samples: pcm }); assert.deepEqual(events.map(e => e.type), ['accepted']);
console.log('Speech-confirmed wake, full keyword input, rejected-match reset, bounded tail, resume, Off and pause: passed');
