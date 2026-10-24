import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Message from '../src/components/Message.jsx';
import { useChat } from '../src/hooks/useChat.js';
import { consumeStream } from '../src/lib/streaming.js';

const request = 'How far away am I from 120 Example Ridge Dr Testville NC';
const metadata = { request, status: 'needs_origin' };
const event = { type: 'done', content: 'I need your current location. [Open in Google Maps](https://www.google.com/maps/dir/?api=1&destination=Example)', maps_route: metadata };
async function* frames() { yield event; }
assert.deepEqual((await consumeStream(frames())).terminal.maps_route, metadata);

let success, failure;
Object.defineProperty(navigator, 'geolocation', { configurable: true, value: {
  getCurrentPosition(ok, error) { success = ok; failure = error; },
} });
const position = () => ({ coords: { latitude: 35.12345, longitude: -78.12345, accuracy: 12 }, timestamp: Date.now() });
const node = document.body.appendChild(document.createElement('div')), root = createRoot(node), routed = [];
const props = { conversationId: 'first', isLastAssistant: true,
  message: { role: 'assistant', content: event.content, maps_route: metadata },
  onRouteFromHere: (...args) => routed.push(args) };
const button = () => [...node.querySelectorAll('button')].find(b => /current location|Finding your location/.test(b.textContent));
await act(async () => root.render(React.createElement(Message, props)));
assert.equal(node.querySelector('a').target, '_blank');
assert.ok(button(), 'A sent request still has a location control, even after a transcript reload');
await act(async () => button().click());
assert.ok(button().disabled);
await act(async () => success(position()));
assert.equal(routed.length, 1);
assert.equal(routed[0][0], request); assert.equal(routed[0][1].status, 'available'); assert.equal(routed[0][2], 'first');

await act(async () => button().click());
await act(async () => failure({ code: 1 }));
assert.match(node.textContent, /Location is blocked/);
assert.equal(routed.length, 1, 'Denied permission cannot resubmit the route');
await act(async () => button().click());
await act(async () => root.render(React.createElement(Message, { ...props, conversationId: 'second' })));
await act(async () => success(position()));
assert.equal(routed.length, 1, 'A late location fix cannot cross a chat switch');
await act(async () => root.render(React.createElement(Message, { ...props, isLastAssistant: false })));
assert.equal(button(), undefined, 'An older route cannot replay after a newer turn');

// Full hook/SSE path: route metadata renders immediately and a scoped follow-up
// sends the original question with a fresh fix only to its originating chat.
let latest;
const cid = 'a'.repeat(32), second = 'b'.repeat(32), posts = [];
const convs = new Map([cid, second].map(id => [id, { conversation_id: id, bot_id: 'chief', messages: [] }]));
const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
globalThis.fetch = async (url, options = {}) => {
  if (url === '/api/bots') return json({ bots: [{ id: 'chief', name: 'The Overwatcher', status: 'running' }] });
  if (url === '/api/conversations') return json({ conversations: [...convs.values()] });
  if (url.startsWith('/api/conversations/')) return json(convs.get(url.split('/').at(-1)));
  if (url === '/api/chat') {
    posts.push(JSON.parse(options.body));
    return new Response('data: ' + JSON.stringify({ ...event, conversation_id: cid }) + '\n\n',
      { headers: { 'Content-Type': 'text/event-stream' } });
  }
  return json({ providers: [], workspaces: [], delegations: [], relayed: [] });
};
globalThis.localStorage = window.localStorage; localStorage.clear();
function Probe() { latest = useChat(); return null; }
await act(async () => root.render(React.createElement(Probe)));
await act(async () => latest.loadConversation(cid));
await act(async () => latest.send(request));
assert.deepEqual(latest.messages.at(-1).maps_route, metadata);
await act(async () => latest.send(request, [], [], { status: 'available', latitude: 35.12345,
  longitude: -78.12345, accuracy_meters: 12, captured_at: Date.now() }, cid));
assert.equal(posts[1].device_location.latitude, 35.12345);
await act(async () => latest.loadConversation(second));
await act(async () => latest.send(request, [], [], { status: 'available' }, cid));
assert.equal(posts.length, 2, 'The hook rejects a stale originating conversation before any POST');
await act(async () => root.unmount());
console.log('Maps reply action, permission failures, stream metadata, scoped resubmission and chat-switch isolation passed');
