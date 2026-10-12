import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import VoiceConnection from '../src/components/VoiceConnection.jsx';

const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div);
const calls = [];
let copied = '';
let paired = false;
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { copied = value; } } });
globalThis.fetch = async (url, options) => {
  calls.push([url, options.method]);
  assert.equal(options.body, undefined, 'phone pairing cannot select an owner or bot');
  return { ok: true, json: async () => url.endsWith('/connect')
    ? { pairing_code: 'voice-test-code', expires_at: Date.now() / 1000 + 600 } : { paired } };
};
const button = text => [...div.querySelectorAll('button')].find(item => item.textContent === text);
await act(async () => root.render(React.createElement(VoiceConnection)));
assert.deepEqual(calls, [['/api/connections/voice', 'GET']], 'opening setup never requests microphone, pairs or sends');
await act(async () => button('Pair voice phone').click());
assert.equal(calls.at(-1)[0], '/api/connections/voice/connect');
assert.match(div.textContent, /10 minutes/);
await act(async () => button('Copy voice pairing code').click());
assert.equal(copied, 'voice-test-code');
paired = true;
await act(async () => window.dispatchEvent(new Event('focus')));
assert.match(div.querySelector('summary').textContent, /paired/, 'returning from the phone refreshes pairing status');
await act(async () => button('Disconnect voice phone').click());
assert.equal(calls.at(-1)[0], '/api/connections/voice/disconnect');
assert.equal(button('Copy voice pairing code'), undefined);
await act(async () => root.unmount());
console.log('voice pairing is explicit, copyable, owner-scoped and revocable: passed');
