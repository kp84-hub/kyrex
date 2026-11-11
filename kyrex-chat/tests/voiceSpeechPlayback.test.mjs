import assert from 'node:assert/strict';
import test from 'node:test';
import { VoiceClient, spokenText, speechChunks } from '../src/lib/voiceClient.js';

globalThis.window = { speechSynthesis: { cancel() {} } };
const longReply = 'This is a longer spoken reply with several complete sentences. '.repeat(24).trim();
const until = async condition => {
  for (let n = 0; n < 100 && !condition(); n++) await new Promise(resolve => setTimeout(resolve, 2));
  assert.ok(condition(), 'Voice did not reach the expected state');
};

function fixture({ reply = longReply, failAt = 0, holdAudio = false, holdNextAudio = false, holdDecode = false, holdPlayback = false, oversizedAudio = false, followUpSeconds = 0, holdChatOpen = false } = {}) {
  const calls = [], parts = [], played = [], phases = [], errors = [], cues = [];
  let audioResolve, audioReject, decodeResolve, player, chatCancelled = false;
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
    microphone: async () => ({ tone(cue) {
      if (cue === 'waiting') {
        cues.push('waiting');
        return () => { cues.push('stopped'); };
      }
    }, stop() {}, context }),
    fetcher: async (url, options) => {
      calls.push(url);
      if (url.endsWith('/session')) return json({ conversation_id: 'voice-fixture',
        settings: { configured: true, engine: 'openai', follow_up_seconds: followUpSeconds } });
      if (url.endsWith('/listen')) return json({ lease: 'test-only-speech-lease' });
      if (url.endsWith('/transcribe')) return json({ text: 'Explain the result' });
      if (url.endsWith('/chat')) return { ok: true, body: new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify({ type: 'done', content: reply }) + '\n\n'));
        if (!holdChatOpen) controller.close();
      }, cancel() { chatCancelled = true; } }) };
      if (url.endsWith('/speech')) {
        const part = JSON.parse(options.body).text; parts.push(part); const index = parts.length;
        // Model the production mismatch: accepted text can produce too much
        // uncompressed audio when synthesized in one request.
        if (part.length * 3000 > 4 * 1024 * 1024 || parts.length === failAt) return { ok: false,
          headers: new Headers(), json: async () => ({ detail: 'The speech response exceeded its limit.' }) };
        return { ok: true, arrayBuffer: async () => {
          if (holdAudio || (holdNextAudio && index === 2)) await new Promise((resolve, reject) => { audioResolve = resolve; audioReject = reject; });
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
  return { client, calls, parts, played, phases, errors, cues, run,
    get chatCancelled() { return chatCancelled; },
    releaseAudio: () => audioResolve(), releaseDecode: () => decodeResolve(),
    rejectAudio: () => audioReject(new Error('The stopped lookahead was aborted')),
    finishPlayback: () => player.onended(),
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

test('the final frame starts speech even while the Chat connection remains open', async () => {
  const f = fixture({ reply: 'Tomorrow is clear.', holdChatOpen: true });
  const run = f.run();
  try {
    await until(() => f.played.length === 1);
    await run;
    assert.equal(f.chatCancelled, true);
    assert.deepEqual(f.errors, []);
  } finally { await f.client.stop(); }
});

test('a slow first audio response gets two bounded local cues, then clears before the reply', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture({ reply: 'Tomorrow is clear.', holdAudio: true });
  const run = f.run();
  try {
    await new Promise(setImmediate);
    assert.ok(f.hasAudio());
    t.mock.timers.tick(2499);
    assert.deepEqual(f.cues, []);
    t.mock.timers.tick(1);
    assert.deepEqual(f.cues, ['waiting']);
    t.mock.timers.tick(12500);
    assert.deepEqual(f.cues, ['waiting', 'stopped', 'waiting']);
    t.mock.timers.tick(45000);
    assert.equal(f.played.length, 0);
    f.releaseAudio();
    await run;
    assert.deepEqual(f.cues, ['waiting', 'stopped', 'waiting', 'stopped']);
    assert.equal(f.played.length, 1);
    assert.deepEqual(f.parts, ['Tomorrow is clear.']);
  } finally { await f.client.stop(); }
});

test('a local spoken wait cue stops immediately with the session', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const f = fixture({ holdAudio: true });
  const events = [], original = window.speechSynthesis;
  window.speechSynthesis = {
    getVoices: () => [{ lang: 'en-US', localService: true }],
    speak: cue => events.push(cue.text),
    cancel: () => events.push('cancelled'),
  };
  window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
  const run = f.run();
  try {
    await new Promise(setImmediate);
    events.length = 0; // Discard startup cleanup.
    t.mock.timers.tick(2500);
    assert.deepEqual(events, ['Give me a moment.']);
    await f.client.stop();
    f.releaseAudio();
    await run;
    t.mock.timers.tick(60000);
    assert.equal(events[1], 'cancelled');
    assert.equal(events.filter(event => event !== 'cancelled').length, 1);
    assert.equal(f.played.length, 0);
  } finally {
    window.speechSynthesis = original;
    delete window.SpeechSynthesisUtterance;
    await f.client.stop();
  }
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
    assert.equal(f.parts.length, pending === 'Playback' ? 2 : 1);
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

test('the next segment is prepared during playback, with only one lookahead', async () => {
  const f = fixture({ holdPlayback: true }); const run = f.run();
  await until(() => f.played.length === 1 && f.parts.length === 2);
  assert.ok(f.parts[0].length <= 180, 'The first audio needs less text than later segments');
  assert.ok(f.parts[0].endsWith('.'), 'The first audio ends at a sentence boundary');
  assert.equal(f.client.phase, 'speaking');
  // Holding playback must not allow the entire reply to be synthesized ahead.
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(f.parts.length, 2);
  f.finishPlayback();
  await until(() => f.played.length === 2 && f.parts.length === 3);
  await f.client.stop(); await run;
  assert.equal(f.played.length, 2); assert.deepEqual(f.errors, []);
  assert.equal(f.calls.filter(url => url.endsWith('/chat')).length, 1);
});

test('a stopped lookahead cannot play or leave an unhandled rejection', async () => {
  const f = fixture({ holdPlayback: true, holdNextAudio: true }); const run = f.run();
  await until(() => f.played.length === 1 && f.hasAudio());
  await f.client.stop(); await run;
  f.rejectAudio(); await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(f.played.length, 1); assert.equal(f.parts.length, 2);
  assert.deepEqual(f.errors, []); assert.equal(f.client.phase, 'stopped');
});

test('speech formatting separates lists and labelled table values without changing facts', () => {
  assert.equal(spokenText('## Tomorrow\n- **Class:** 8:30 AM\n- Cost: $25.00\n- Nothing has been booked.'),
    'Tomorrow. Class: 8:30 AM. Cost: $25.00. Nothing has been booked.');
  assert.equal(spokenText('| Date | Time | Fee |\n| --- | --- | --- |\n| Nov 5 | 8:30 AM | $25.00 |'),
    'Date: Nov 5. Time: 8:30 AM. Fee: $25.00.');
  assert.equal(spokenText('**Review**\n[The schedule](https://example.test/a_(b))\n```text\nnot spoken'),
    'Review. The schedule Code details are in your voice chat.');
  assert.equal(spokenText('Keep file_name, 2 * 3 * 4, -$12.50 and 10.5%.'),
    'Keep file_name, 2 * 3 * 4, -$12.50 and 10.5%.');
  assert.equal(spokenText('Needs approval.\n1. Review the exact class.\n2. Say yes to confirm.'),
    'Needs approval. Review the exact class. Say yes to confirm.');
  assert.equal(spokenText('- [ ] Paid\n- [x] Reviewed'), 'Unchecked: Paid. Checked: Reviewed.');
});

test('chunk boundaries preserve times, decimals and names; truncation keeps whole words', () => {
  const text = 'Dr. Lee will see you at 8:30 a.m. The fee is $25.50. '.repeat(20).trim();
  const parts = speechChunks(text);
  assert.equal(parts.join(' '), text);
  assert.ok(parts.every(part => !part.endsWith('Dr.') && !part.endsWith('$25.')));
  const long = 'A complete sentence about the saved result. '.repeat(100);
  const value = spokenText(long);
  assert.ok(value.endsWith('. The full response is in your voice chat.'));
  assert.ok(value.isWellFormed());
  assert.ok(spokenText('x' + '😀'.repeat(2000)).isWellFormed());
});

test('oversized audio still stops before decode or playback', async () => {
  const f = fixture({ oversizedAudio: true }); await f.run();
  assert.equal(f.parts.length, 1); assert.equal(f.played.length, 0);
  assert.deepEqual(f.errors, ['Reply audio was too large. Read it in voice chat.']);
  assert.equal(f.calls.filter(url => url.endsWith('/chat')).length, 1);
});
