import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { useChat } from '../src/hooks/useChat.js';

let latest, requestId, conversationFetchOptions, delayedOtherConversation;
let stored = [];
const answer = 'Recent texts with Ethan The Neighbor:\n\nEthan: See you at six.';
const response = body => ({ ok: true, status: 200, json: async () => body });
function Probe() { latest = useChat(); return null; }
globalThis.fetch = async (url, options) => {
  if (url === '/api/conversations/c2') return new Promise(resolve => {
    delayedOtherConversation = () => resolve(response({ conversation_id: 'c2', messages: [{ id: 'other', role: 'assistant', content: 'Another conversation' }] }));
  });
  if (url === '/api/chat') {
    requestId = JSON.parse(options.body).request_id;
    return { ok: true, body: new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify({ type: 'done', content: answer }) + '\n\n'));
      controller.close();
    } }) };
  }
  if (url === '/api/conversations/c1') {
    conversationFetchOptions = options;
    return response({ conversation_id: 'c1', messages: stored });
  }
  if (url === '/api/conversations') return response({ conversations: [{ conversation_id: 'c1' }] });
  if (url === '/api/bots') return response({ bots: [] });
  return response({});
};
Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
const root = createRoot(document.body.appendChild(document.createElement('div')));
await act(async () => root.render(React.createElement(Probe)));
await act(async () => latest.loadConversation('c1'));
await act(async () => latest.send('Show my recent texts with Ethan The Neighbor.'));
assert.equal(latest.messages.at(-1).content, answer);
await act(async () => window.dispatchEvent(new Event('focus')));
assert.equal(latest.messages.at(-1)?.content, answer, 'clicking back into Chat must not erase a completed result when a snapshot is behind');
assert.equal(conversationFetchOptions?.cache, 'no-store', 'transcript reads bypass cached snapshots');

stored = [
  { id: `turn-${requestId}-user`, role: 'user', content: 'Show my recent texts with Ethan The Neighbor.' },
  { id: `turn-${requestId}-assistant`, role: 'assistant', content: answer },
];
const visibleIds = latest.messages.map(m => m.id);
await act(async () => window.dispatchEvent(new Event('focus')));
assert.equal(latest.messages.length, 2, 'the persisted turn replaces the optimistic turn without duplication');
assert.deepEqual(latest.messages.map(m => m.id), visibleIds, 'refresh preserves message keys rather than remounting the result');
stored = [];
await act(async () => window.dispatchEvent(new Event('focus')));
assert.equal(latest.messages.at(-1)?.content, answer, 'a subsequent stale snapshot cannot remove an acknowledged result');
await act(async () => latest.loadConversation('c1'));
assert.equal(latest.messages.at(-1)?.content, answer, 'selecting the current conversation must not clear a completed result');
stored = [
  { id: `turn-${requestId}-user`, role: 'user', content: 'Show my recent texts with Ethan The Neighbor.' },
  { id: `turn-${requestId}-assistant`, role: 'assistant', content: answer },
  { id: 'research-completion', role: 'assistant', content: 'Research finished.' },
];
await act(async () => window.dispatchEvent(new Event('focus')));
assert.equal(latest.messages.at(-1).content, 'Research finished.', 'background results still arrive on focus');
let loadingOther;
await act(async () => { loadingOther = latest.loadConversation('c2'); });
await act(async () => latest.loadConversation('c1'));
await act(async () => { delayedOtherConversation(); await loadingOther; });
assert.equal(latest.activeId, 'c1');
assert.equal(latest.messages.at(-1).content, 'Research finished.', 'a slower conversation load cannot replace the selected transcript');
await act(async () => root.unmount());
console.log('completed text results survive focus refresh and stale snapshots: passed');
