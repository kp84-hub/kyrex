import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { useChat } from '../src/hooks/useChat.js';

let latest;
function Probe() { latest = useChat(); return null; }
const response = body => ({ ok: true, status: 200, async json() { return body; } });
let messages = [{ id: 'u1', role: 'user', content: 'Find events' }];
let deferred;
let hold = false;
let finishStream;
globalThis.fetch = async url => {
  if (url === '/api/chat') return { ok: true, body: new ReadableStream({ start(controller) { finishStream = () => { controller.enqueue(new TextEncoder().encode('event: done\ndata: {"type":"done","content":"New answer"}\n\n')); controller.close(); }; } }) };
  if (url === '/api/conversations/c1') {
    if (hold) return new Promise(resolve => { deferred = body => resolve(response(body)); });
    return response({ conversation_id: 'c1', bot_id: 'chief', messages });
  }
  if (url === '/api/bots') return response({ bots: [{ id: 'chief', name: 'The Overwatcher' }] });
  if (url === '/api/conversations') return response({ conversations: [{ conversation_id: 'c1' }] });
  return response({});
};
Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
const root = createRoot(document.body.appendChild(document.createElement('div')));
await act(async () => { root.render(React.createElement(Probe)); });
await act(async () => { await latest.loadConversation('c1'); });
assert.equal(latest.messages.length, 1);
messages = [...messages, { id: 'research-stable-answer', role: 'assistant', content: '**Festival**' }];
await act(async () => { window.dispatchEvent(new Event('focus')); });
assert.equal(latest.messages.at(-1).id, 'research-stable-answer', 'finished research appears on return to chat');
await act(async () => { window.dispatchEvent(new Event('focus')); });
assert.equal(latest.messages.length, 2, 'refresh does not duplicate the durable answer');
hold = true;
let refresh;
await act(async () => { refresh = latest.refreshMessages('c1'); });
assert.ok(deferred);
await act(async () => { latest.setActiveId('c2'); });
await act(async () => { deferred({ conversation_id: 'c1', messages: [{ id: 'stale', role: 'assistant', content: 'Old chat' }] }); await refresh; });
assert.notEqual(latest.messages.at(-1).id, 'stale', 'a late response cannot replace the newly selected chat');
await act(async () => { latest.setActiveId('c1'); });
await act(async () => { refresh = latest.refreshMessages('c1'); });
let sending;
await act(async () => { sending = latest.send('New request'); });
assert.ok(finishStream);
await act(async () => { deferred({ conversation_id: 'c1', messages: [{ id: 'stale', role: 'assistant', content: 'Old reply' }] }); await refresh; });
assert.ok(latest.messages.some(m => m.content === 'New request'), 'an in-flight refresh cannot erase the new user turn');
await act(async () => { finishStream(); await sending; });
assert.equal(latest.messages.at(-1).content, 'New answer');
await act(async () => { root.unmount(); });
console.log('research completion refresh checks passed');
