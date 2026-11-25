import assert from 'node:assert/strict';
import test from 'node:test';
import { streamChat } from '../src/lib/api.js';
import { consumeStream } from '../src/lib/streaming.js';

const cid = '1'.repeat(32), rid = '2'.repeat(32);
const encode = new TextEncoder();
const packet = (number, event) => encode.encode('id: ' + number + '\ndata: ' + JSON.stringify(event) + '\n\n');
const reply = (...frames) => new Response(new ReadableStream({
  start(controller) {
    frames.forEach(frame => controller.enqueue(frame));
    controller.close();
  },
}), { headers: { 'X-Kyrex-Resumable': '1' } });

test('locking a phone and returning resumes the same stream at the last delivered frame', async () => {
  const fetchBefore = globalThis.fetch;
  const visibilityBefore = Object.getOwnPropertyDescriptor(document, 'visibilityState');
  const calls = [];
  let visibility = 'visible';
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
  globalThis.fetch = async (url, options) => {
    calls.push({ url, method: options.method || 'GET' });
    if (calls.length === 1) {
      assert.equal(JSON.parse(options.body).request_id, rid);
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(packet(1, { type: 'delta', content: 'Original ' }));
          options.signal.addEventListener('abort', () => controller.error(options.signal.reason), { once: true });
        },
      }), { headers: { 'X-Kyrex-Resumable': '1' } });
    }
    return reply(packet(1, { type: 'delta', content: 'Original ' }),
      packet(2, { type: 'delta', content: 'reply' }), packet(3, { type: 'done', content: 'Original reply' }));
  };
  try {
    const turn = streamChat(cid, 'Original request', rid);
    const result = await consumeStream(turn.stream, {
      onDelta(text) {
        if (text !== 'Original ') return;
        visibility = 'hidden';
        document.dispatchEvent(new Event('visibilitychange'));
        visibility = 'visible';
        document.dispatchEvent(new Event('visibilitychange'));
      },
    });
    assert.equal(result.full, 'Original reply');
    assert.equal(result.terminal.kind, 'done');
    assert.deepEqual(calls.map(call => call.method), ['POST', 'GET']);
    assert.ok(calls[1].url.endsWith('/' + cid + '/' + rid + '?after=1'));
  } finally {
    globalThis.fetch = fetchBefore;
    if (visibilityBefore) Object.defineProperty(document, 'visibilityState', visibilityBefore);
    else delete document.visibilityState;
  }
});

test('reconnecting an expired reply offers a status check and does not submit the request', async () => {
  const fetchBefore = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    assert.equal(options.method, undefined);
    assert.ok(url.endsWith('/' + cid + '/' + rid + '?after=0'));
    return new Response(JSON.stringify({ detail: 'Check the saved conversation.' }), { status: 404 });
  };
  try {
    const turn = streamChat(cid, 'Original request', rid, undefined, undefined, undefined, true);
    assert.deepEqual((await consumeStream(turn.stream)).terminal,
      { kind: 'error', message: 'Check the saved conversation.', checkStatus: true });
  } finally { globalThis.fetch = fetchBefore; }
});

test('an error after a turn is accepted offers status recovery', async () => {
  const fetchBefore = globalThis.fetch;
  globalThis.fetch = async () => reply(packet(1, { type: 'error', message: 'The model did not return a reply.' }));
  try {
    const turn = streamChat(cid, 'Original request', rid);
    assert.deepEqual((await consumeStream(turn.stream)).terminal,
      { kind: 'error', message: 'The model did not return a reply.', checkStatus: true });
  } finally { globalThis.fetch = fetchBefore; }
});
