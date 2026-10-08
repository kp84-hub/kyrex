import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import App from '../src/App.jsx';

globalThis.localStorage = window.localStorage;
localStorage.setItem('kyrex-chat.activeConversationId', 'live');
HTMLElement.prototype.scrollIntoView = () => {};
const response = body => ({ ok: true, status: 200, async json() { return body; } });
const savedTimers = new Map();
const originalTimeout = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
globalThis.setTimeout = (fn, ms, ...args) => {
  if (ms !== 2500) return originalTimeout(fn, ms, ...args);
  const id = {}; savedTimers.set(id, fn); return id;
};
globalThis.clearTimeout = id => { savedTimers.delete(id); originalClear(id); };
let finishStream, emitFrame, terminal = false, relayedOnce = false, reads = 0;
const row = { delegation_id: 'd-live', task_id: 't-live', target_bot_id: 'dev',
  executor_prefix: 'developer', text: 'Fix the parser', status: 'running',
  progress: [{ stage: 'Inspecting the relevant files…' }] };
globalThis.fetch = async (url, options) => {
  if (url === '/api/chat') return { ok: true, body: new ReadableStream({ start(controller) {
    emitFrame = frame => controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(frame) + '\n\n'));
    finishStream = () => { controller.enqueue(new TextEncoder().encode('data: {"type":"done","content":"Developer work is still running."}\n\n')); controller.close(); };
  } }) };
  if (url.startsWith('/api/delegations')) {
    const relayed = terminal && !relayedOnce ? [{ message: 'Developer finished.' }] : [];
    if (terminal) relayedOnce = true;
    return response({ delegations: [row], relayed });
  }
  if (url === '/api/conversations/live') { reads++; return response({
    conversation_id: 'live', bot_id: 'chief', messages: terminal ? [
      { id: 'durable-final', role: 'assistant', content: 'Developer finished.' },
    ] : [] }); }
  if (url === '/api/conversations') return response({ conversations: [{ conversation_id: 'live', bot_id: 'chief' }] });
  if (url === '/api/bots') return response({ bots: [{ id: 'chief', name: 'The Overwatcher' }] });
  return response({});
};
const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div);
try {
  await act(async () => root.render(React.createElement(App)));
  assert.match(div.querySelector('.delegated-work').textContent, /Inspecting the relevant files/);
  assert.match(div.querySelector('.sidebar').textContent, /Inspecting the relevant files/);
  const input = div.querySelector('textarea');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(input, 'Fix the parser');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  assert.ok(finishStream, 'coordinator stream stays open during target work');
  row.progress.push({ stage: 'Running checks…' });
  assert.ok(savedTimers.size, 'live cards keep polling during the coordinator stream');
  await act(async () => { const [id, fn] = savedTimers.entries().next().value; savedTimers.delete(id); await fn(); });
  assert.match(div.querySelector('.delegated-work').textContent, /Running checks/);
  assert.match(div.querySelector('.sidebar').textContent, /Running checks/);
  const priorReads = reads;
  terminal = true; row.status = 'done'; row.result_summary = 'Fixed the parser. Checks passed.';
  await act(async () => { const [id, fn] = savedTimers.entries().next().value; savedTimers.delete(id); await fn(); });
  assert.equal(reads, priorReads, 'saved replies cannot replace the streaming coordinator turn');
  assert.equal(savedTimers.size, 0, 'polling stops when the target finishes');
  await act(async () => finishStream());
  assert.ok(reads > priorReads, 'deferred durable reply is checked after the turn completes');
  // A direct Bot turn takes its current stage from the chat stream, rather
  // than being masked by the active conversation's immediate fallback.
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(input, 'Inspect the parser');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  await act(async () => {
    emitFrame({ type: 'task', task_id: 'direct-task', status: 'running' });
    emitFrame({ type: 'progress', payload: { stage: 'Reading the parser…', args: 'PRIVATE' } });
  });
  assert.equal(div.querySelector('.conversation-subtitle').textContent, 'Reading the parser…');
  await act(async () => emitFrame({ type: 'approval_request', task_id: 'direct-task', tier: 1, summary: 'Approve changes' }));
  assert.equal(div.querySelector('.conversation-subtitle').textContent, 'Waiting for your approval');
  await act(async () => finishStream());
} finally {
  await act(async () => root.unmount());
  globalThis.setTimeout = originalTimeout; globalThis.clearTimeout = originalClear;
}
console.log('Overwatcher cards update while streaming; transcript refresh waits and polling stops: passed');
