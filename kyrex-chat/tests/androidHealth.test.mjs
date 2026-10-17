import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AndroidHealthSetup from '../src/components/AndroidHealthSetup.jsx';
import ConnectionsSettings from '../src/components/ConnectionsSettings.jsx';
import { readAndroidHealthSetup, clearAndroidHealthSetup, androidHealthIntent } from '../src/lib/androidHealth.js';

const request = { challenge: 'a'.repeat(43), state: 'b'.repeat(43) };
const storage = window.sessionStorage;
assert.equal(readAndroidHealthSetup({ search: '' }, storage), null);
assert.equal(readAndroidHealthSetup({ search: '?android_health=1&challenge=bad' }, storage).invalid, true);
let pending = readAndroidHealthSetup({ search: '?android_health=1&' + new URLSearchParams(request) }, storage);
assert.equal(pending.challenge, request.challenge);
assert.equal(readAndroidHealthSetup({ search: '' }, storage).state, request.state, 'survives sign-in redirect');
clearAndroidHealthSetup(storage);
assert.equal(readAndroidHealthSetup({ search: '' }, storage), null);
assert.throws(() => androidHealthIntent({ code: 'evil#Intent', state: request.state }));

let signedIn = false;
let responseState = request.state;
const calls = [];
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url, options });
  const value = url === '/api/me' ? { authenticated: signedIn, username: 'alice' }
    : url === '/api/android/config' ? { health: true, origin: window.location.origin }
    : url === '/api/android/health/authorize' ? { code: 'c'.repeat(43), state: responseState, expires_at: Date.now()/1000 + 120 }
    : url === '/api/connections' ? { connectors: [{ provider: 'samsung_health', configured: true, connected: false, status: 'disconnected', read_only: true }] }
    : {};
  return { ok: true, json: async () => value };
};
async function render(component) {
  const div = document.createElement('div'); document.body.appendChild(div);
  const root = createRoot(div);
  await act(async () => root.render(component));
  return { div, async close() { await act(async () => root.unmount()); div.remove(); } };
}
let screen = await render(React.createElement(AndroidHealthSetup, { request }));
assert.ok(screen.div.querySelector('a[href="/auth/login"]'));
assert.equal(calls.filter(c => c.options.method === 'POST').length, 0, 'opening screen cannot grant health');
await screen.close();
signedIn = true;
screen = await render(React.createElement(AndroidHealthSetup, { request }));
assert.match(screen.div.textContent, /Connect this phone to alice/);
await act(async () => screen.div.querySelector('button').click());
const authorization = calls.find(c => c.url.endsWith('/authorize'));
assert.deepEqual(JSON.parse(authorization.options.body), { ...request, expected_owner: 'alice' });
assert.ok(!Object.hasOwn(JSON.parse(authorization.options.body), 'owner'), 'owner is resolved by the server');
const callback = screen.div.querySelector('a[href^="intent:"]');
assert.ok(callback && callback.href.includes('package=com.kyrex.chat'));
assert.ok(!screen.div.textContent.includes('c'.repeat(43)), 'callback code is not displayed');
assert.ok(!screen.div.innerHTML.includes('device_token'));
await screen.close();
responseState = 'wrong-state';
screen = await render(React.createElement(AndroidHealthSetup, { request }));
await act(async () => screen.div.querySelector('button').click());
assert.match(screen.div.querySelector('[role="alert"]').textContent, /expired/);
assert.equal(screen.div.querySelector('a[href^="intent:"]'), null);
await screen.close();

Object.defineProperty(navigator, 'userAgent', { configurable: true, value: 'Android' });
screen = await render(React.createElement(ConnectionsSettings, {}));
assert.ok(screen.div.querySelector(`a[href="${androidHealthIntent()}"]`));
assert.ok(!screen.div.textContent.includes('open the Kyrex Health companion app'));
assert.ok(!calls.some(c => c.url.endsWith('samsung_health/pair')), 'integrated settings do not mint legacy codes');
await screen.close();
console.log('Android health setup, consent, return, and Connections checks passed.');
