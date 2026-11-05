import assert from 'node:assert/strict';
import { VoiceClient, voiceReply } from '../src/lib/voiceClient.js';
import { Resampler, CommandCapture, wavBlob } from '../src/lib/voiceAudio.js';
const session = { conversation_id: 'voice-alice', settings: { configured: true, engine: 'device', voice: 'cedar' } };
const response = data => ({ ok: true, json: async () => data });
const wait = async condition => { for (let i = 0; i < 100 && !condition(); i++) await new Promise(r => setTimeout(r, 1)); assert.ok(condition()); };
let sends = 0, stopped = 0, samples;
const calls = [];
const fetcher = async (url, options) => {
  calls.push([url, options]);
  if (url.endsWith('/session')) return response(session);
  if (url.endsWith('/listen')) return response({ lease: 'test-only-lease' });
  if (url.endsWith('/stop')) return response({ stopped: true });
  if (url.endsWith('/transcribe')) return response({ text: 'Show my calendar' });
  if (url.endsWith('/chat')) { sends++; throw new Error('Connection interrupted. Check voice chat.'); }
  return response({});
};
const microphone = async callback => { samples = callback; return { stop: () => stopped++, tone: () => {}, stream: null }; };
const client = new VoiceClient({ fetcher, microphone, clientId: 'a'.repeat(32) });
assert.equal(calls.length, 0);
await client.start(false); assert.equal(client.phase, 'recording');
assert.equal(calls[0][0], '/api/voice/session'); assert.equal(calls[1][0], '/api/voice/listen');
samples(new Float32Array(3200).fill(.1)); await client.finishCapture();
assert.equal(sends, 1, 'Failed commands are not retried'); assert.equal(client.phase, 'stopped'); assert.equal(stopped, 1);
assert.ok(calls.find(([url]) => url.endsWith('/chat'))[1].headers['X-Voice-Lease']);
let resolveMic, lateStopped = 0;
const late = new VoiceClient({ fetcher, microphone: () => new Promise(resolve => { resolveMic = resolve; }), clientId: 'b'.repeat(32) });
const starting = late.start(false); await wait(() => resolveMic); await late.stop();
resolveMic({ stop: () => lateStopped++, tone: () => {} }); await starting;
assert.equal(lateStopped, 1, 'Late permission results release the microphone'); assert.equal(late.phase, 'stopped');
let wakeWorker;
const listening = new VoiceClient({ fetcher, microphone, clientId: 'c'.repeat(32), workerFactory: () => (wakeWorker = { postMessage() {}, terminate() {} }) });
const wakeStart = listening.start(true); await wait(() => wakeWorker);
wakeWorker.onmessage({ data: { type: 'ready' } }); await wakeStart; assert.equal(listening.phase, 'listening');
await listening.stop(); wakeWorker.onmessage({ data: { type: 'wake' } }); assert.equal(listening.phase, 'stopped');
const values = Float32Array.from({ length: 600 }, (_, i) => Math.sin(i));
const resampler = new Resampler(48000), whole = new Resampler(48000).push(values);
assert.deepEqual([...resampler.push(values.slice(0, 256)), ...resampler.push(values.slice(256))], [...whole]);
const wav = new DataView(await wavBlob([new Float32Array(3200).fill(.1)]).arrayBuffer());
assert.equal(wav.getUint32(24, true), 16000); assert.equal(wav.getUint16(22, true), 1);
const capture = new CommandCapture(); assert.equal(capture.push(new Float32Array(3200).fill(.1)), null); assert.equal(capture.push(new Float32Array(19200)), 'done');
assert.equal(new CommandCapture().push(new Float32Array(128000)), 'empty');
const stream = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('data: {"type":"done","content":"Hello Keith."}\n\n')); controller.close(); } });
assert.equal(await voiceReply({ body: stream }), 'Hello Keith.');
// The default transport must call native Window.fetch with the global receiver.
// Injected arrow-function transports cannot expose an Illegal invocation bug.
const originalFetch = globalThis.fetch;
const nativeCalls = [];
globalThis.fetch = function(url, options) {
  assert.equal(this, globalThis, 'Native fetch retains its browser receiver');
  nativeCalls.push(url);
  return fetcher(url, options);
};
try {
  const nativeClient = new VoiceClient({ microphone, clientId: 'd'.repeat(32) });
  await nativeClient.start(false);
  assert.equal(nativeClient.phase, 'recording', 'Default browser fetch can start voice');
  await nativeClient.request('heartbeat');
  await nativeClient.stop();
  assert.deepEqual(nativeCalls, ['/api/voice/session', '/api/voice/listen', '/api/voice/heartbeat', '/api/voice/stop']);
} finally { globalThis.fetch = originalFetch; }
console.log('Voice lifecycle, stale callbacks, no retries, resampling and SSE: passed');


// Continuous wake listening uses the SAME explicitly claimed session.
async function continuousFixture({ rejectHeartbeat = false, holdHeartbeat = false } = {}) {
  const calls = [], messages = [];
  let worker, micStops = 0, micStarts = 0, resumes = 0, releaseHeartbeat;
  const context = { state: 'suspended', async resume() { resumes++; this.state = 'running'; } };
  const client = new VoiceClient({ clientId: 'e'.repeat(32),
    fetcher: async (url, options) => {
      calls.push([url, options]);
      if (url.endsWith('/session')) return response(session);
      if (url.endsWith('/listen')) return response({ lease: 'continuous-test-lease' });
      if (url.endsWith('/heartbeat')) {
        if (holdHeartbeat) await new Promise(resolve => { releaseHeartbeat = resolve; });
        if (rejectHeartbeat) return { ok: false, headers: new Headers({ 'X-Voice-Reason': 'stale' }), json: async () => ({ detail: 'Listening switched to another device.' }) };
      }
      return response({});
    },
    microphone: async () => { micStarts++; return { context, tone() {}, stop() { micStops++; } }; },
    workerFactory: () => (worker = { postMessage(message) { messages.push(message.type); }, terminate() {} }),
  });
  const starting = client.start(true); await wait(() => worker);
  worker.onmessage({ data: { type: 'ready' } }); await starting;
  return { client, calls, messages, context, wake: () => worker.onmessage({ data: { type: 'wake' } }),
    release: () => releaseHeartbeat(), held: () => !!releaseHeartbeat,
    get micStops() { return micStops; }, get micStarts() { return micStarts; }, get resumes() { return resumes; } };
}

const continuous = await continuousFixture();
try {
  await continuous.client.visibilityChanged(true);
  assert.equal(continuous.client.phase, 'listening'); assert.equal(continuous.micStops, 0);
  await continuous.client.visibilityChanged(false);
  assert.equal(continuous.resumes, 1); assert.equal(continuous.client.phase, 'listening');
  continuous.wake(); assert.equal(continuous.client.phase, 'recording');
  continuous.client.samples(new Float32Array(128000)); // No command after a wake.
  assert.equal(continuous.client.phase, 'listening', 'Silence rearms wake mode instead of stopping');
  assert.equal(continuous.client.recording, null);
  assert.equal(continuous.calls.filter(([url]) => url.endsWith('/listen')).length, 1);
  assert.equal(continuous.calls.filter(([url]) => /\/(chat|transcribe)$/.test(url)).length, 0);
  assert.equal(continuous.micStarts, 1);
  await continuous.client.stop();
  await continuous.client.visibilityChanged(false);
  assert.equal(continuous.micStarts, 1, 'Stop cannot be undone by tab visibility');
} finally { await continuous.client.stop(); }

const switched = await continuousFixture({ rejectHeartbeat: true });
await switched.client.visibilityChanged(false);
assert.equal(switched.client.phase, 'stopped'); assert.equal(switched.resumes, 0);
assert.equal(switched.calls.filter(([url]) => url.endsWith('/listen')).length, 1, 'A rejected lease is never reclaimed');

const lateResume = await continuousFixture({ holdHeartbeat: true });
const resuming = lateResume.client.visibilityChanged(false);
await wait(lateResume.held);
await lateResume.client.stop(); lateResume.release(); await resuming;
assert.equal(lateResume.client.phase, 'stopped'); assert.equal(lateResume.resumes, 0);
assert.equal(lateResume.micStarts, 1, 'A late renewal cannot reopen audio after Stop');
console.log('Continuous wake, silence, same-lease audio resume, switched device and Stop fencing: passed');
