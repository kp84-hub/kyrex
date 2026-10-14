import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { useChat } from '../src/hooks/useChat.js';

let latest, stored = [], mode = 'sleep', posts = 0, stops = 0, aborted = 0;
let finishViewer;
const response = body => ({ ok: true, status: 200, json: async () => body });
const frame = value => new TextEncoder().encode('data: ' + JSON.stringify(value) + '\n\n');
function Probe() { latest = useChat(); return null; }
globalThis.fetch = async (url, options = {}) => {
  if (url === '/api/chat/cancel') { stops++; return response({ cancelled: true }); }
  if (url === '/api/chat') {
    posts++;
    const request = JSON.parse(options.body);
    const id = `turn-${request.request_id}-assistant`;
    stored.push({ id: `turn-${request.request_id}-user`, role: 'user', content: request.message });
    if (mode !== 'ordinary') stored.push({ id, role: 'assistant', content: 'Checking your existing workout booking.',
      workout_pending: { id: 'booking-one', deadline: 9999999999 } });
    return { ok: true, body: new ReadableStream({ start(controller) {
      options.signal.addEventListener('abort', () => {
        aborted++;
        controller.error(new DOMException('Viewer detached', 'AbortError'));
      }, { once: true });
      if (mode !== 'before-progress' && mode !== 'ordinary') {
        controller.enqueue(frame({ type: 'progress', payload: { workout_booking: true, stage: 'Checking Level 6' } }));
      }
      finishViewer = () => { controller.enqueue(frame({ type: 'done', content: 'Ordinary answer' })); controller.close(); };
      if (mode === 'network') queueMicrotask(() => controller.error(new TypeError('network issue')));
      if (mode === 'quiet') queueMicrotask(() => controller.close());
    } }) };
  }
  if (url === '/api/conversations/c1') {
    assert.equal(options.cache, 'no-store');
    return response({ conversation_id: 'c1', messages: structuredClone(stored) });
  }
  if (url === '/api/conversations') return response({ conversations: [{ conversation_id: 'c1' }] });
  if (url === '/api/bots') return response({ bots: [] });
  assert.ok(!url.startsWith('/api/task/'), 'Workout recovery must use its saved conversation, not an invented Bot task.');
  return response({});
};
const visible = value => Object.defineProperty(document, 'visibilityState', { configurable: true, value });
visible('visible');
const root = createRoot(document.body.appendChild(document.createElement('div')));
await act(async () => root.render(React.createElement(Probe)));
await act(async () => latest.loadConversation('c1'));

for (const scenario of ['sleep', 'before-progress', 'network', 'quiet']) {
  mode = scenario;
  const before = posts;
  let sending;
  await act(async () => { sending = latest.send(scenario === 'network' ? 'yes' : 'Book my workout tomorrow at 8:30 AM'); });
  if (scenario === 'sleep' || scenario === 'before-progress') {
    assert.equal(latest.isGenerating, true);
    await act(async () => { visible('hidden'); document.dispatchEvent(new Event('visibilitychange')); });
    assert.equal(aborted, scenario === 'sleep' ? 0 : 1, 'Sleeping alone must not stop the server operation.');
    await act(async () => { visible('visible'); document.dispatchEvent(new Event('visibilitychange')); });
  }
  await act(async () => { await sending; });
  assert.equal(latest.isGenerating, false, 'A sleeping or disconnected viewer cannot leave the composer stuck.');
  assert.equal(latest.error, null);
  assert.match(latest.messages.at(-1).content, /existing workout booking/);
  assert.equal(latest.messages.at(-1).error, undefined);
  await act(async () => latest.retry());
  assert.equal(posts, before + 1, 'Checking status cannot POST another preparation or yes.');
  const saved = stored.at(-1);
  delete saved.workout_pending;
  saved.workout_booking = { id: 'booking-one', revision: 'saved-revision', state: scenario === 'network' ? 'confirmed' : 'proposed' };
  saved.content = scenario === 'network' ? 'Glofox confirmed. Booking reference: recorded-receipt.' : 'Level 6 tomorrow at 8:30 AM. Reply **yes** to book this exact class once.';
  await act(async () => window.dispatchEvent(new Event('online')));
  assert.equal(latest.messages.at(-1).content, saved.content);
  assert.equal(latest.messages.at(-1).id, saved.id, 'The saved reply replaces the same turn rather than adding a duplicate.');
  assert.equal(latest.messages.filter(m => m.id === saved.id).length, 1);
  assert.equal(posts, before + 1);
  assert.equal(stops, 0, 'Transport recovery cannot send the user Stop command.');
}

mode = 'ordinary';
let sending;
const beforeAbort = aborted;
await act(async () => { sending = latest.send('Research exercise routines'); });
await act(async () => { visible('hidden'); document.dispatchEvent(new Event('visibilitychange')); });
await act(async () => { visible('visible'); document.dispatchEvent(new Event('visibilitychange')); });
assert.equal(latest.isGenerating, true);
assert.equal(aborted, beforeAbort, 'An ordinary research stream keeps its existing lifecycle.');
await act(async () => { finishViewer(); await sending; });
assert.equal(latest.messages.at(-1).content, 'Ordinary answer');
await act(async () => root.unmount());
console.log('workout viewers recover after sleep or network loss without repeating bookings: passed');
