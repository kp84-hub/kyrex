import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { isLocationRequest, currentLocation, locationForMessage } from '../src/lib/location.js';
import { streamChat } from '../src/lib/api.js';
import Composer from '../src/components/Composer.jsx';

let permission = 'prompt', reads = 0, success, failure;
Object.defineProperty(navigator, 'permissions', { configurable: true, value: { query: async () => ({ state: permission }) } });
Object.defineProperty(navigator, 'geolocation', { configurable: true, value: {
  getCurrentPosition(ok, error, options) { reads++; success = ok; failure = error; assert.equal(options.maximumAge, 60000); },
} });
const position = () => ({ coords: { latitude: 35.12345, longitude: -78.12345, accuracy: 12 }, timestamp: Date.now() });
assert.ok(isLocationRequest('How far away is the Fuquay Varina Target from me'));
assert.ok(isLocationRequest('Directions to 120 Example Street'));
assert.ok(!isLocationRequest('Read my calendar'));
assert.ok(!isLocationRequest('How far along is the pull request?'));
assert.ok(!isLocationRequest('Where am I in the setup process?'));
assert.equal(await locationForMessage('Read my calendar'), undefined);
assert.deepEqual(await locationForMessage('How far is Example store from me?'), { status: 'not_shared' });
assert.equal(reads, 0, 'Only the explicit control can prompt for new location permission');
const pending = currentLocation(); success(position());
const fix = await pending;
assert.equal(fix.status, 'available');
assert.equal(await locationForMessage('Directions to Example store', fix), fix);
permission = 'granted';
const refresh = locationForMessage('How far is Example store from me?');
await Promise.resolve(); success(position());
assert.equal((await refresh).status, 'available');
const prior = reads;
assert.deepEqual(await locationForMessage('Directions to Example store', { status: 'not_shared' }), { status: 'not_shared' });
assert.equal(reads, prior, 'Removing a fix suppresses automatic location for this message');
const controller = new AbortController(), stopped = currentLocation({ signal: controller.signal });
controller.abort(); success(position());
assert.deepEqual(await stopped, { status: 'unavailable' }, 'Late location cannot restore cancelled context');
const blocked = currentLocation(); failure({ code: 1 });
assert.deepEqual(await blocked, { status: 'denied' });

const node = document.body.appendChild(document.createElement('div')), root = createRoot(node), sent = [];
const props = { compact: true, conversationId: 'one', activeBotId: 'chief', onSend: (...args) => sent.push(args) };
const input = async value => act(async () => {
  const field = node.querySelector('textarea');
  Object.getOwnPropertyDescriptor(field.constructor.prototype, 'value').set.call(field, value);
  field.dispatchEvent(new Event('input', { bubbles: true }));
});
const share = () => [...node.querySelectorAll('button')].find(b => b.textContent === 'Use current location');
await act(async () => root.render(React.createElement(Composer, props)));
assert.equal(share(), undefined, 'Ordinary chat has no extra location control');
await input('How far is Example store from me?');
await act(async () => share().click());
assert.match(node.textContent, /Finding your location/);
assert.ok(node.querySelector('[aria-label="Send message"]').disabled);
await act(async () => success(position()));
assert.match(node.textContent, /Current location attached/);
await act(async () => node.querySelector('[aria-label="Send message"]').click());
assert.equal(sent[0][3].status, 'available');
await input('Directions to Example store');
await act(async () => share().click());
await act(async () => root.render(React.createElement(Composer, { ...props, conversationId: 'two' })));
await act(async () => success(position()));
assert.equal(node.querySelector('[aria-label="Remove current location"]'), null, 'A delayed fix cannot follow a conversation switch');
await act(async () => share().click());
await act(async () => success(position()));
await act(async () => node.querySelector('[aria-label="Remove current location"]').click());
await act(async () => node.querySelector('[aria-label="Send message"]').click());
assert.deepEqual(sent.at(-1)[3], { status: 'not_shared' });
await act(async () => root.unmount());

let posted;
globalThis.fetch = async (url, options) => {
  posted = JSON.parse(options.body);
  return new Response('data: {"type":"done","content":"Open Maps"}\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
};
for await (const chunk of streamChat('one', 'Directions to Example store', 'turn', undefined, [], fix).stream) { /* drain */ }
assert.deepEqual(posted.device_location, fix);
assert.equal(posted.message, 'Directions to Example store');
assert.equal(posted.conversation_id, 'one');
console.log('Location permission, one-shot fixes, removal, chat isolation, cancellation and request transport passed');
