import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Composer from '../src/components/Composer.jsx';
import Message from '../src/components/Message.jsx';
import { useChat } from '../src/hooks/useChat.js';

const node = document.body.appendChild(document.createElement('div'));
const root = createRoot(node);
let sequence = 0;
URL.createObjectURL = () => `blob:fixture-${++sequence}`;
const revoked = [];
URL.revokeObjectURL = url => revoked.push(url);
const file = new window.File(['pixels'], 'Kbot.png', { type: 'image/png' });
const sent = [];
const props = { compact: true, conversationId: 'a'.repeat(32), onSend: (text, files) => sent.push({ text, files }) };
const choose = async files => act(async () => {
  const input = node.querySelector('input[type=file]');
  Object.defineProperty(input, 'files', { value: files, configurable: true });
  input.dispatchEvent(new Event('change', { bubbles: true }));
});
await act(async () => root.render(React.createElement(Composer, props)));
await choose([file]);
assert.equal(node.querySelectorAll('.composer-image').length, 1);
assert.equal(node.querySelector('[aria-label="Send message"]').disabled, false, 'An image-only message can be sent');
await act(async () => node.querySelector('[aria-label="Remove image 1"]').click());
assert.equal(node.querySelectorAll('.composer-image').length, 0);
await choose([file, file, file, file]);
assert.match(node.textContent, /up to three/);
assert.equal(node.querySelectorAll('.composer-image').length, 0);
await choose([file]);
await act(async () => node.querySelector('[aria-label="Send message"]').click());
assert.equal(sent.length, 1); assert.equal(sent[0].files[0], file);
assert.equal(node.querySelectorAll('.composer-image').length, 0);

await choose([file]);
await act(async () => root.render(React.createElement(Composer, { ...props, conversationId: 'b'.repeat(32) })));
assert.equal(node.querySelectorAll('.composer-image').length, 0, 'An unsent image cannot follow a chat switch');
await act(async () => root.render(React.createElement(Composer, { ...props, onSend: () => false })));
await choose([file]);
await act(async () => node.querySelector('[aria-label="Send message"]').click());
assert.equal(node.querySelectorAll('.composer-image').length, 1, 'Failed upload retains the selected file');
assert.ok(revoked.length >= 4, 'Preview object URLs are released');

const cid = 'a'.repeat(32), iid = 'c'.repeat(32), image = { id: iid, url: `/api/conversations/${cid}/images/${iid}` };
await act(async () => root.render(React.createElement(Message, { message: { role: 'user', content: 'Look at Kbot',
  images: [image, { id: 'bad', url: 'https://untrusted.example/tracker' }] } })));
assert.equal(node.querySelectorAll('img').length, 1, 'Only private attachment routes render images');

// Exercise the real hook + API: uploaded opaque IDs, never image bytes, go to Chat.
let latest, delayUpload, holdUpload = false;
const uploads = [], posts = [];
const convs = new Map([[cid, { conversation_id: cid, bot_id: 'chief', messages: [] }],
  ['b'.repeat(32), { conversation_id: 'b'.repeat(32), bot_id: 'chief', messages: [] }]]);
const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
globalThis.fetch = async (url, options = {}) => {
  if (url === '/api/bots') return json({ bots: [{ id: 'chief', name: 'The Overwatcher', status: 'running' }] });
  if (url === '/api/conversations') return json({ conversations: [...convs.values()] });
  if (url.endsWith('/images')) {
    uploads.push({ url, body: options.body });
    if (holdUpload) return new Promise(resolve => { delayUpload = () => resolve(json(image)); });
    return json(image);
  }
  if (url.startsWith('/api/conversations/')) return json(convs.get(url.split('/').at(-1)));
  if (url === '/api/chat') {
    posts.push(JSON.parse(options.body));
    return new Response('data: ' + JSON.stringify({ type: 'done', conversation_id: cid, content: 'Kbot has blue eyes.' }) + '\n\n',
      { headers: { 'Content-Type': 'text/event-stream' } });
  }
  return json({ providers: [], workspaces: [], delegations: [], relayed: [] });
};
globalThis.localStorage = window.localStorage; localStorage.clear();
function Probe() { latest = useChat(); return null; }
await act(async () => root.render(React.createElement(Probe)));
await act(async () => latest.loadConversation(cid));
await act(async () => latest.send('', [file]));
assert.equal(uploads[0].body, file);
assert.deepEqual(posts[0].image_ids, [iid]);
assert.equal(posts[0].conversation_id, cid);
assert.equal(Object.hasOwn(posts[0], 'workspace_id'), false);
assert.equal(latest.messages.find(m => m.role === 'user').images[0].url, image.url);

holdUpload = true;
let sending;
await act(async () => { sending = latest.send('Read this image', [file]); });
await act(async () => latest.loadConversation('b'.repeat(32)));
await act(async () => { delayUpload(); await sending; });
assert.equal(posts.length, 1, 'A delayed upload cannot send into a different conversation');
assert.equal(latest.messages.length, 0);
await act(async () => root.unmount());
console.log('Image previews, removal, limits, upload recovery, private rendering and conversation binding passed');
