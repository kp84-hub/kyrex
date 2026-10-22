import assert from 'node:assert/strict';
import test from 'node:test';
import { VoiceClient, spokenText, speechChunks } from '../src/lib/voiceClient.js';

globalThis.window = { speechSynthesis: { cancel() {} } };
const longReply = 'This is a longer spoken reply with several complete sentences. '.repeat(24).trim();
const until = async condition => {
  for (let n = 0; n < 100 && !condition(); n++) await new Promise(resolve => setTimeout(resolve, 2));
  assert.ok(condition(), 'Voice did not reach the expected state');
};

function fixture({ reply = longReply, failAt = 0, holdAudio = false, holdDecode = false, holdPlayback = false, oversizedAudio = false, followUpSeconds = 0 } = {}) {
  const calls = [], parts = [], played = [], phases = [], errors = [];
  let audioResolve, decodeResolve, player;
  const json = value => ({ ok: true, json: async () => value });
  const context = {
    destination: {},
    decodeAudioData: async data => {
      if (holdDecode) await new Promise(resolve => { decodeResolve = resolve; });
      return data;
    },
    createBufferSource() {
      player = { connect() {}, start() {
        played.push(this.buffer);
        if (!holdPlayback) queueMicrotask(() => this.onended());
      }, stop() { this.onended?.(); } };
      return player;
    },
  };
  const client = new VoiceClient({ clientId: 'a'.repeat(32),
    onState: state => { phases.push(state.phase); if (state.error) errors.push(state.error); },
    microphone: async () => ({ tone() {}, stop() {}, context }),
    fetcher: async (url, options) => {
      calls.push(url);
      if (url.endsWith('/session')) return json({ conversation_id: 'voice-fixture',
        settings: { configured: true, engine: 'openai', follow_up_seconds: followUpSeconds } });
      if (url.endsWith('/listen')) return json({ lease: 'test-only-speech-lease' });
      if (url.endsWith('/transcribe')) return json({ text: 'Explain the result' });
      if (url.endsWith('/chat')) return { ok: true, body: new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify({ type: 'done', content: reply }) + '\n\n'));
        controller.close();
      } }) };
      if (url.endsWith('/speech')) {
        const part = JSON.parse(options.body).text; parts.push(part);
        // Model the production mismatch: accepted text can produce too much
        // uncompressed audio when synthesized in one request.
        if (part.length * 3000 > 4 * 1024 * 1024 || parts.length === failAt) return { ok: false,
          headers: new Headers(), json: async () => ({ detail: 'The speech response exceeded its limit.' }) };
        return { ok: true, arrayBuffer: async () => {
          if (holdAudio) await new Promise(resolve => { audioResolve = resolve; });
          if (oversizedAudio) return new ArrayBuffer(4 * 1024 * 1024 + 1);
          return new TextEncoder().encode(part).buffer;
        } };
      }
      return json({});
    },
  });
  async function run() {
    await client.start(false);
    client.samples(new Float32Array(3200).fill(.1));
    return client.finishCapture();
  }
  return { client, calls, parts, played, phases, errors, run,
    releaseAudio: () => audioResolve(), releaseDecode: () => decodeResolve(),
    hasAudio: () => !!audioResolve, hasDecode: () => !!decodeResolve, hasPlayer: () => !!player };
}

test('long shared replies play in order without resubmitting the Chat command', async () => {
  const f = fixture();
  await f.run();
  assert.deepEqual(f.errors, []);
  assert.ok(f.parts.length > 1);
  assert.equal(f.parts.join(' '), spokenText(longReply));
  assert.deepEqual(f.played.map(buffer => new TextDecoder().decode(buffer)), f.parts);
  assert.equal(f.calls.filter(url => url.endsWith('/chat')).length, 1);
  assert.equal(f.calls.filter(url => url.endsWith('/transcribe')).length, 1);
  assert.equal(f.phases.filter(phase => phase === 'followup_wait').length, 0);
  assert.equal(f.client.phase, 'stopped');
});

test('short replies still need one speech request', async () => {
  const f = fixture({ reply: 'Tomorrow is clear.' }); await f.run();
  assert.deepEqual(f.parts, ['Tomorrow is clear.']);
  assert.equal(f.played.length, 1); assert.deepEqual(f.errors, []);
});

test('follow-up listening starts only after every speech piece finishes', async () => {
  const f = fixture({ followUpSeconds: 8 });
  try {
    await f.run();
    assert.ok(f.parts.length > 1);
    assert.equal(f.played.length, f.parts.length);
    assert.equal(f.phases.filter(phase => phase === 'followup_wait').length, 1);
    assert.equal(f.client.phase, 'followup_wait');
    assert.equal(f.phases.lastIndexOf('speaking'), f.phases.length - 2);
  } finally { await f.client.stop(); }
});

test('word and sentence boundaries keep reply text and Unicode intact', () => {
  const sentence = 'Please review this calendar event before approving it.';
  const reply = Array(30).fill(sentence).join(' ');
  const parts = speechChunks(reply);
  assert.equal(parts.join(' '), reply);
  assert.ok(parts.every(part => part.endsWith('.')));
  const unbroken = '😀'.repeat(801);
  const unicode = speechChunks(unbroken);
  assert.equal(unicode.join(''), unbroken);
  assert.ok(unicode.every(part => part.length <= 400 && part.isWellFormed()));
  const malformed = '\ud800 ' + 'x'.repeat(500);
  assert.equal(speechChunks(malformed).join('').replaceAll(' ', ''), malformed.replaceAll(' ', ''));
});

for (const pending of ['Audio', 'Decode', 'Playback']) {
  test(`Stop during ${pending.toLowerCase()} prevents later pieces and stale playback`, async () => {
    const f = fixture({ ['hold' + pending]: true });
    const run = f.run();
    await until(pending === 'Playback' ? f.hasPlayer : f['has' + pending]);
    await f.client.stop();
    if (pending !== 'Playback') f['release' + pending]();
    await run;
    assert.equal(f.parts.length, 1);
    assert.equal(f.played.length, pending === 'Playback' ? 1 : 0);
    assert.equal(f.client.phase, 'stopped'); assert.deepEqual(f.errors, []);
  });
}

test('a later synthesis failure stops playback without retrying speech or Chat', async () => {
  const f = fixture({ failAt: 2 }); await f.run();
  assert.equal(f.parts.length, 2); assert.equal(f.played.length, 1);
  assert.deepEqual(f.errors, ['The speech response exceeded its limit.']);
  assert.equal(f.calls.filter(url => url.endsWith('/chat')).length, 1);
  assert.equal(f.client.phase, 'stopped');
});

test('oversized audio still stops before decode or playback', async () => {
  const f = fixture({ oversizedAudio: true }); await f.run();
  assert.equal(f.parts.length, 1); assert.equal(f.played.length, 0);
  assert.deepEqual(f.errors, ['Reply audio was too large. Read it in voice chat.']);
  assert.equal(f.calls.filter(url => url.endsWith('/chat')).length, 1);
});
