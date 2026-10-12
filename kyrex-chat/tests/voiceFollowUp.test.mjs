import assert from 'node:assert/strict';
import { VoiceClient } from '../src/lib/voiceClient.js';
const until = async condition => {
  for (let n = 0; n < 100 && !condition(); n++) await new Promise(r => setTimeout(r, 5));
  assert.ok(condition(), 'Voice did not reach the expected state');
};
const oldWindow = globalThis.window, oldUtterance = globalThis.SpeechSynthesisUtterance;
globalThis.window = { speechSynthesis: { speak: utterance => queueMicrotask(() => utterance.onend()), cancel() {} } };
globalThis.SpeechSynthesisUtterance = class {};
try {
  function fixture({ wake = false, seconds = 8 } = {}) {
    const calls = [], tones = [], phases = []; let samples, worker, stopped = 0;
    const json = value => ({ ok: true, json: async () => value });
    const client = new VoiceClient({ clientId: 'e'.repeat(32), onState: state => phases.push(state.phase),
      microphone: async callback => { samples = callback; return { stop: () => stopped++, tone: () => tones.push(true) }; },
      workerFactory: () => (worker = { messages: [], postMessage(data) { this.messages.push(data); }, terminate() {} }),
      fetcher: async (url, options) => {
        calls.push([url, options]);
        if (url.endsWith('/session')) return json({ conversation_id: 'voice-same-thread', settings: { configured: true, engine: 'device', follow_up_seconds: seconds } });
        if (url.endsWith('/listen')) return json({ lease: 'test-only-followup-lease' });
        if (url.endsWith('/transcribe')) return json({ text: 'And tomorrow?' });
        if (url.endsWith('/chat')) return { ok: true, body: new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode('data: {"type":"done","content":"Tomorrow is clear."}\n\n')); c.close(); } }) };
        return json({});
      } });
    return { client, calls, tones, phases, samples: data => samples(data), worker: () => worker, stopped: () => stopped, wake };
  }
  async function start(f) {
    const promise = f.client.start(f.wake);
    if (f.wake) { await until(() => f.worker()); f.worker().onmessage({ data: { type: 'ready' } }); }
    await promise;
    if (f.wake) f.worker().onmessage({ data: { type: 'wake' } });
  }
  const voiced = new Float32Array(3200).fill(.1);
  const f = fixture(); await start(f);
  f.samples(voiced); await f.client.finishCapture();
  assert.equal(f.client.phase, 'followup_wait', 'Does not listen during playback or its speaker tail');
  const before = f.calls.length; f.samples(voiced); assert.equal(f.calls.length, before);
  await until(() => f.client.phase === 'followup');
  assert.equal(f.tones.length, 2, 'Initial request and follow-up both have a readiness beep');
  f.samples(voiced); assert.equal(f.client.phase, 'recording');
  await f.client.finishCapture(); await until(() => f.client.phase === 'followup');
  assert.equal(f.calls.filter(([url]) => url.endsWith('/chat')).length, 2);
  assert.equal(f.calls.filter(([url]) => url.endsWith('/listen')).length, 1, 'Follow-ups reuse the same lease');
  const requests = f.calls.filter(([url]) => url.endsWith('/chat')).map(([, opts]) => JSON.parse(opts.body).request_id);
  assert.notEqual(requests[0], requests[1], 'Each distinct follow-up gets a distinct request identity');
  f.samples(new Float32Array(8 * 16000)); await until(() => f.client.phase === 'stopped');
  assert.equal(f.calls.filter(([url]) => url.endsWith('/transcribe')).length, 2, 'Idle silence is never uploaded');
  assert.equal(f.stopped(), 1);

  const w = fixture({ wake: true, seconds: 15 }); await start(w);
  w.samples(voiced); await w.client.finishCapture(); await until(() => w.client.phase === 'followup');
  w.samples(new Float32Array(8 * 16000)); assert.equal(w.client.phase, 'followup', '15-second option permits a longer pause');
  w.samples(new Float32Array(7 * 16000)); assert.equal(w.client.phase, 'listening');
  assert.equal(w.worker().messages.at(-1).type, 'resume');
  assert.equal(w.calls.filter(([url]) => url.endsWith('/chat')).length, 1);
  await w.client.stop();

  const off = fixture({ seconds: 0 }); await start(off); off.samples(voiced); await off.client.finishCapture();
  await until(() => off.client.phase === 'stopped'); assert.equal(off.tones.length, 1);
  const cancelled = fixture(); await start(cancelled); cancelled.samples(voiced); await cancelled.client.finishCapture();
  await cancelled.client.stop(); await new Promise(r => setTimeout(r, 300));
  assert.equal(cancelled.client.phase, 'stopped'); assert.equal(cancelled.tones.length, 1, 'Stop cancels a pending follow-up');
  console.log('Voice follow-ups, same session, repeated turns, silence, Off, and Stop: passed');
} finally { globalThis.window = oldWindow; globalThis.SpeechSynthesisUtterance = oldUtterance; }
