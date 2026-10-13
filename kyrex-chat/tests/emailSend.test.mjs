import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Message from '../src/components/Message.jsx';
import { consumeStream } from '../src/lib/streaming.js';
import { useChat } from '../src/hooks/useChat.js';

const node = document.body.appendChild(document.createElement('div'));
let root = createRoot(node);
let job = { id: 'email-one', conversation_id: 'c1', revision: 'review-one', state: 'draft',
  detail: 'Review the exact address and message. Nothing has been sent.', from: 'assistant@example.com',
  to: 'office@example.com', subject: 'Appointment', body: 'Tuesday afternoon availability?', reply: false };
let decisions = [], edits = [], reads = 0;
const response = value => ({ ok: true, json: async () => value });
globalThis.fetch = async (url, init = {}) => {
  if (url.endsWith('/edit')) {
    const data = JSON.parse(init.body); edits.push(data);
    assert.equal(data.revision, job.revision);
    job = { ...job, ...data, revision: 'review-two' };
  } else if (url.endsWith('/decision')) {
    const data = JSON.parse(init.body); decisions.push(data);
    assert.equal(data.revision, job.revision, 'only the currently displayed exact review can be confirmed');
    job = { ...job, state: data.decision === 'send' ? 'accepted' : 'cancelled',
      detail: data.decision === 'send' ? 'Zoho accepted this email. Appointment confirmation is not yet verified.' : 'Cancelled. Nothing sent.' };
  } else if (url.endsWith('/replies')) { reads++; return response({ content: 'Office reply: Tuesday at 10 is available.' }); }
  return response(job);
};
const button = text => [...node.querySelectorAll('button')].find(b => b.textContent === text);
const render = () => act(async () => root.render(React.createElement(Message, { conversationId: 'c1',
  message: { id: 'm1', role: 'assistant', content: 'Email review ready.', email_draft: { id: job.id } } })));
await render();
assert.deepEqual(decisions, [], 'mounting or reloading a review never approves sending');
assert.match(node.textContent, /assistant@example.com/);
assert.equal(node.querySelector('input[type=email]').value, 'office@example.com');
const area = node.querySelector('textarea');
await act(async () => {
  Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(area, 'Could you offer Wednesday instead?');
  area.dispatchEvent(new Event('input', { bubbles: true }));
});
assert.equal(button('Send email').disabled, true, 'unsaved edits cannot be sent');
assert.equal(button('Cancel email').disabled, false, 'unsaved edits can still be discarded by cancelling');
await act(async () => button('Save changes').click());
assert.equal(edits.length, 1); assert.equal(decisions.length, 0, 'saving edits does not send');
assert.equal(button('Send email').disabled, false);
await act(async () => button('Send email').click());
assert.deepEqual(decisions, [{ revision: 'review-two', decision: 'send' }]);
assert.equal(button('Send email'), undefined, 'accepted email cannot be retried');
assert.match(node.textContent, /confirmation is not yet verified/);
await act(async () => button('Check email replies').click());
assert.equal(reads, 1); assert.match(node.querySelector('[aria-label="Email reply"]').textContent, /Tuesday at 10/);
await act(async () => root.unmount()); root = createRoot(node);
await render();
assert.equal(decisions.length, 1, 'reopening a sent card cannot send again');
await act(async () => root.unmount()); root = createRoot(node);
job = { ...job, state: 'draft', revision: 'reply-review', reply: true, to: 'bookings@example.com', original_sender: 'Office <office@example.com>' };
await render();
assert.equal(node.querySelector('input[type=email]').disabled, true, 'a threaded reply keeps the verified recipient');
await act(async () => button('Cancel email').click());
assert.equal(decisions.at(-1).decision, 'cancel');
assert.equal(button('Send email'), undefined);
await act(async () => root.unmount());

let incoming;
async function* stream() { yield { type: 'email_draft', draft_id: 'native-email' }; yield { type: 'done', content: '', email_draft: { id: 'native-email' } }; }
const { terminal } = await consumeStream(stream(), { onEmailDraft: event => incoming = event.draft_id });
assert.equal(incoming, 'native-email'); assert.equal(terminal.kind, 'done'); assert.equal(terminal.email_draft.id, 'native-email');

let hook;
function Harness() { hook = useChat('alice'); return null; }
root = createRoot(node);
const chunk = new TextEncoder().encode('event: message\ndata: {"type":"conversation","conversation_id":"c1"}\n\nevent: message\ndata: {"type":"email_draft","draft_id":"hook-email"}\n\nevent: message\ndata: {"type":"done","content":"Review ready","email_draft":{"id":"hook-email"}}\n\n');
globalThis.fetch = async url => url === '/api/chat' ? { ok: true, body: new ReadableStream({ start(c) { c.enqueue(chunk); c.close(); } }) }
  : response(url === '/api/conversations/c1' ? { conversation_id: 'c1', messages: [] }
    : url === '/api/conversations' ? { conversations: [] } : { bots: [] });
await act(async () => root.render(React.createElement(Harness)));
await act(async () => hook.loadConversation('c1'));
await act(async () => hook.send('Email office@example.com about Tuesday'));
assert.ok(hook.messages.some(m => m.role === 'assistant' && m.email_draft?.id === 'hook-email'));
await act(async () => root.unmount());
console.log('Email exact review, edits, explicit send/cancel, threaded reply, reload and native hook: passed');
