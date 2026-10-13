import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Message from '../src/components/Message.jsx';
import { consumeStream } from '../src/lib/streaming.js';
import { useChat } from '../src/hooks/useChat.js';

const div = document.body.appendChild(document.createElement('div'));
let root = createRoot(div);
const message = {
  id: 'task-week-result', role: 'assistant',
  content: 'Preview only — nothing sent.\n\n#L6Workout\nVerified week',
  message_draft: { recipient: 'L6 Besties', text: '#L6Workout\nVerified week' },
};
let preparations = 0, decisions = [], jobId = 's1', state = 'ready', prepareError = false;
const response = body => ({ ok: true, status: 200, json: async () => body });
globalThis.fetch = async (url, options = {}) => {
  if (url.endsWith('/prepare-send')) {
    preparations++;
    assert.equal(url, '/api/conversations/c1/messages/task-week-result/prepare-send');
    assert.equal(options.method, 'POST');
    assert.equal(options.body, undefined, 'the preview text and recipient are loaded from server storage');
    if (prepareError) return { ok: false, status: 400, json: async () => ({ detail: 'L6 Besties is not in the phone snapshot' }) };
    return response({ id: jobId });
  }
  if (url.endsWith('/decision')) {
    decisions.push(JSON.parse(options.body).decision);
    state = 'accepted';
  }
  return response({ id: jobId, state, name: 'L6 Besties', text: message.message_draft.text,
    recipients: ['Friend A · +15555550111', 'Friend B · +15555550222'] });
};
const button = text => [...div.querySelectorAll('button')].find(item => item.textContent === text);
const render = saved => act(async () => root.render(React.createElement(Message, { conversationId: 'c1', message: saved })));
await render(message);
assert.equal(preparations, 0, 'opening an automated preview makes no phone request');
assert.ok(button('Prepare for L6 Besties'));
assert.equal(button('Send'), undefined);
await act(async () => button('Prepare for L6 Besties').click());
assert.equal(preparations, 1);
assert.ok(div.textContent.includes('Friend A') && div.textContent.includes('Friend B'));
assert.deepEqual(decisions, [], 'preparing and showing recipients never confirms a send');
await act(async () => button('Send').click());
assert.deepEqual(decisions, ['send']);
assert.equal(button('Prepare again'), undefined, 'an accepted send cannot be replayed with Prepare again');

await act(async () => root.unmount()); root = createRoot(div);
state = 'expired';
await render({ ...message, message_send: { id: 's1' } });
assert.equal(preparations, 1, 'reopening a saved card restores its send identity');
assert.ok(button('Prepare again'));
jobId = 's2'; state = 'ready';
await act(async () => button('Prepare again').click());
assert.equal(preparations, 2);
assert.deepEqual(decisions, ['send'], 'renewing an expired preview still requires a new confirmation');

await act(async () => root.unmount()); root = createRoot(div);
prepareError = true;
await render(message);
await act(async () => button('Prepare for L6 Besties').click());
assert.match(div.querySelector('[role="alert"]').textContent, /phone snapshot/);
assert.equal(button('Send'), undefined);
await act(async () => root.unmount());
root = createRoot(div); prepareError = false; state = 'ready'; jobId = 's3';
async function* livePreview() {
  yield { type: 'done', content: message.content, conversation_id: 'c1',
    message_draft: message.message_draft, message_id: message.id };
}
const { terminal } = await consumeStream(livePreview());
assert.deepEqual(terminal.message_draft, message.message_draft);
assert.equal(terminal.persisted_id, message.id);
const beforeLive = preparations;
await render({ ...message, id: 'temporary-assistant-id',
  message_draft: terminal.message_draft, persisted_id: terminal.persisted_id });
assert.equal(preparations, beforeLive, 'a live manual preview does not prepare automatically');
await act(async () => button('Prepare for L6 Besties').click());
assert.equal(preparations, beforeLive + 1, 'preparation uses the durable message id from SSE');
assert.deepEqual(decisions, ['send'], 'the live preview also requires an explicit Send');
await act(async () => root.unmount());
let latest;
function Probe() { latest = useChat(); return null; }
const originalFetch = globalThis.fetch;
globalThis.fetch = async (url, options = {}) => {
  if (url === '/api/conversations') return response({ conversations: [{ conversation_id: 'c1' }] });
  if (url === '/api/conversations/c1') return response({ conversation_id: 'c1', messages: [] });
  if (url === '/api/bots') return response({ bots: [] });
  if (url === '/api/chat') return { ok: true, body: new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify({
      type: 'done', content: message.content, conversation_id: 'c1',
      message_draft: message.message_draft, message_id: message.id,
    }) + '\n\n'));
    controller.close();
  } }) };
  return originalFetch(url, options);
};
const probeRoot = createRoot(document.body.appendChild(document.createElement('div')));
await act(async () => probeRoot.render(React.createElement(Probe)));
await act(async () => latest.loadConversation('c1'));
await act(async () => latest.send('#L6Workout preview'));
const actualLive = latest.messages.find(item => item.role === 'assistant');
assert.deepEqual(actualLive.message_draft, message.message_draft);
assert.equal(actualLive.persisted_id, message.id);
root = createRoot(div);
const beforeHook = preparations;
await render(actualLive);
assert.equal(preparations, beforeHook);
await act(async () => button('Prepare for L6 Besties').click());
assert.equal(preparations, beforeHook + 1);
assert.deepEqual(decisions, ['send'], 'the actual Chat hook does not send a manual preview');
await act(async () => root.unmount());
await act(async () => probeRoot.unmount());
console.log('saved workout preview, recipient review, explicit Send, expiry renewal and visible preparation errors: passed');
