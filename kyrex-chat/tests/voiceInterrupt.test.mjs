import assert from 'node:assert/strict';
import test from 'node:test';
import { VoiceClient } from '../src/lib/voiceClient.js';

globalThis.window = { speechSynthesis: { cancel() {}, speak() {} } };
const tick = () => new Promise(resolve => setTimeout(resolve, 5));
async function until(condition) {
  for (let i = 0; i < 200 && !condition(); i++) await tick();
  assert.ok(condition(), 'Expected voice state was not reached');
}
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function fixture({ working = false, delayedHeaders = false, ready = true, holdInterrupt = false, engine = 'openai', holdSpeech = false } = {}) {
  const calls = [], phases = [], errors = [], messages = [];
  const headers = deferred(), acknowledged = deferred(), speech = deferred();
  let stream, player, tones = 0, stops = 0, micStops = 0, results = 0;
  const json = value => ({ ok: true, json: async () => value });
  const worker = { postMessage(message) { messages.push(message); }, terminate() {} };
  const client = new VoiceClient({ clientId: 'a'.repeat(32),
    workerFactory: () => { queueMicrotask(() => worker.onmessage({ data: { type: 'ready' } })); return worker; },
    onResult: () => results++,
    onState: state => { phases.push(state.phase); if (state.error) errors.push(state.error); },
    microphone: async () => ({ tone() { tones++; }, stop() { micStops++; }, context: {
      destination: {}, decodeAudioData: async value => value,
      createBufferSource() { player = { connect() {}, start() {}, stop() { stops++; } }; return player; },
    } }),
    fetcher: async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith('/session')) return json({ conversation_id: 'voice-test', settings: { configured: true, engine, follow_up_seconds: 0 } });
      if (url.endsWith('/listen')) return json({ lease: 'test-voice-lease' });
      if (url.endsWith('/transcribe')) return json({ text: 'Show my calendar' });
      if (url.endsWith('/chat')) {
        const response = { ok: true, body: new ReadableStream({ start(controller) {
          stream = controller;
          if (!working) {
            controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify({ type: 'done', content: 'A long reply. '.repeat(100) }) + '\n\n'));
            controller.close();
          }
        } }) };
        if (delayedHeaders) await headers.promise;
        return response;
      }
      if (url.endsWith('/speech')) {
        if (holdSpeech) await speech.promise;
        return { ok: true, arrayBuffer: async () => new ArrayBuffer(20) };
      }
      if (url.endsWith('/interrupt')) {
        if (holdInterrupt) await acknowledged.promise;
        if (working && ready) {
          stream.enqueue(new TextEncoder().encode('data: {"type":"cancelled"}\n\n')); stream.close();
        }
        return json({ cancelled: working, ready });
      }
      return json({});
    },
  });
  const count = suffix => calls.filter(call => call.url.endsWith('/' + suffix)).length;
  async function run(wake = false) {
    await client.start(wake);
    if (wake) worker.onmessage({ data: { type: 'wake' } });
    client.samples(new Float32Array(3200).fill(.1));
    return client.finishCapture();
  }
  return { client, calls, phases, errors, messages, run, count, worker,
    get tones() { return tones; }, get stops() { return stops; }, get micStops() { return micStops; }, get results() { return results; },
    get player() { return player; }, headers, acknowledged, speech };
}

test('interrupt stops the current audio immediately, retains the lease, and records only a fresh command', async () => {
  const f = fixture(); const run = f.run();
  try {
    await until(() => f.client.phase === 'speaking');
    const pending = f.client.interrupt();
    assert.equal(f.stops, 1); assert.equal(f.client.phase, 'interrupting');
    assert.equal(f.client.interrupt(), pending, 'Repeated interrupt shares one operation');
    f.client.samples(new Float32Array(3200).fill(.2));
    await pending; await run;
    assert.equal(f.client.phase, 'recording'); assert.equal(f.tones, 2);
    assert.equal(f.client.recording.chunks.length, 0, 'Speaker tail is discarded');
    assert.equal(f.count('chat'), 1); assert.equal(f.count('speech'), 1);
    assert.equal(f.count('listen'), 1); assert.equal(f.count('interrupt'), 1);
    assert.equal(f.micStops, 0); assert.ok(f.client.heartbeat);
    const chat = f.calls.find(c => c.url.endsWith('/chat'));
    const interrupt = f.calls.find(c => c.url.endsWith('/interrupt'));
    assert.equal(JSON.parse(interrupt.options.body).request_id, JSON.parse(chat.options.body).request_id);
    assert.equal(interrupt.options.headers['X-Voice-Lease'], 'test-voice-lease');
    assert.deepEqual(f.errors, []);
  } finally { await f.client.stop(); }
});

test('an early interrupt waits for server registration and cooperative cancellation before recording', async () => {
  const f = fixture({ working: true, delayedHeaders: true, holdInterrupt: true }); const run = f.run();
  try {
    await until(() => f.client.phase === 'working');
    const interrupted = f.client.interrupt(); await tick();
    assert.equal(f.count('interrupt'), 0); assert.equal(f.tones, 1);
    f.headers.resolve(); await until(() => f.count('interrupt') === 1);
    assert.equal(f.client.phase, 'interrupting'); assert.equal(f.tones, 1);
    f.acknowledged.resolve(); await interrupted; await run;
    assert.equal(f.client.phase, 'recording'); assert.equal(f.count('speech'), 0);
    assert.equal(f.count('chat'), 1); assert.deepEqual(f.errors, []); assert.equal(f.results, 1);
  } finally { await f.client.stop(); }
});

test('wake detection runs during work and playback; the keyword interrupts without submitting another command', async () => {
  const f = fixture(); const run = f.run(true);
  try {
    await until(() => f.client.phase === 'speaking');
    assert.ok(f.messages.some(m => m.type === 'resume'));
    f.client.samples(new Float32Array(1600).fill(.1));
    assert.ok(f.messages.some(m => m.type === 'samples'));
    f.worker.onmessage({ data: { type: 'wake' } });
    await until(() => f.client.phase === 'recording'); await run;
    assert.equal(f.stops, 1); assert.equal(f.count('interrupt'), 1);
    assert.equal(f.count('transcribe'), 1); assert.equal(f.count('chat'), 1);
    assert.equal(f.messages.at(-1).type, 'pause'); assert.equal(f.tones, 2);
  } finally { await f.client.stop(); }
});

test('a late speech response cannot play after interruption', async () => {
  const f = fixture({ holdSpeech: true }); const run = f.run();
  try {
    await until(() => f.client.phase === 'synthesizing');
    await f.client.interrupt(); f.speech.resolve(); await run;
    assert.equal(f.player, undefined); assert.equal(f.count('speech'), 1);
    assert.equal(f.client.phase, 'recording'); assert.equal(f.count('chat'), 1);
  } finally { f.speech.resolve(); await f.client.stop(); }
});

test('an unsettled server request stops voice without replay or a new readiness tone', async () => {
  const f = fixture({ ready: false }); const run = f.run();
  await until(() => f.client.phase === 'speaking');
  await f.client.interrupt(); await run;
  assert.equal(f.client.phase, 'stopped'); assert.equal(f.tones, 1);
  assert.match(f.errors[0], /still stopping/); assert.equal(f.count('chat'), 1);
});

test('Stop during interruption suppresses late recording and playback', async () => {
  const f = fixture({ holdInterrupt: true }); const run = f.run();
  await until(() => f.client.phase === 'speaking');
  const interrupted = f.client.interrupt(); await until(() => f.count('interrupt') === 1);
  await f.client.stop(); f.acknowledged.resolve(); await interrupted; await run;
  assert.equal(f.client.phase, 'stopped'); assert.equal(f.tones, 1);
  assert.equal(f.count('chat'), 1); assert.deepEqual(f.errors, []);
});

test('device speech cancellation does not depend on an onend callback', async () => {
  const previous = globalThis.SpeechSynthesisUtterance;
  globalThis.SpeechSynthesisUtterance = class {};
  const f = fixture({ engine: 'device' }); const run = f.run();
  try {
    await until(() => f.client.phase === 'speaking');
    await f.client.interrupt(); await run;
    assert.equal(f.client.phase, 'recording'); assert.equal(f.count('speech'), 0);
    assert.equal(f.count('chat'), 1); assert.deepEqual(f.errors, []);
  } finally { await f.client.stop(); globalThis.SpeechSynthesisUtterance = previous; }
});
