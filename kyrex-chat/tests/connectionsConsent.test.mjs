import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import ConnectionsSettings from '../src/components/ConnectionsSettings.jsx';

const authorization = 'https://accounts.google.com/o/oauth2/v2/auth?state=TEST-ONLY';
let connected = false;
let start;
let resolveStart;
let popup;
let blocked = false;
let navigated;
let closed = false;
window.open = (url) => {
  assert.equal(url, 'about:blank');
  assert.equal(start, undefined, 'window is reserved before the async start request');
  if (blocked) return null;
  popup = { opener: window, closed: false, close() { closed = true; }, location: { replace(url) { navigated = url; } } };
  return popup;
};
globalThis.fetch = async (url) => {
  if (url.endsWith('/google/connect')) {
    start = true;
    return new Promise(resolve => { resolveStart = resolve; });
  }
  return { ok: true, json: async () => ({ connectors: [{ provider: 'google', configured: true,
    connected, usable: connected, status: connected ? 'connected' : 'disconnected' }] }) };
};
const make = async () => {
  start = undefined; navigated = undefined; closed = false;
  const div = document.createElement('div'); document.body.append(div);
  const root = createRoot(div);
  await act(async () => root.render(React.createElement(ConnectionsSettings)));
  const connect = div.querySelector('[aria-label="Connect Google Calendar"]');
  act(() => connect.click());
  return { div, root, connect };
};
const finishStart = async (url = authorization) => {
  await act(async () => resolveStart({ ok: true, json: async () => ({ authorization_url: url }) }));
};

let { div, root } = await make();
assert.equal(popup.opener, null, 'the consent page cannot control the Chat tab');
assert.equal(navigated, undefined);
await finishStart();
assert.equal(navigated, authorization);
assert.ok(div.querySelector('a[href="' + authorization + '"]'));
assert.equal(div.querySelector('[aria-label="Connected"]'), null, 'opening consent does not claim connection');
connected = true;
await act(async () => window.dispatchEvent(new Event('focus')));
assert.ok(div.querySelector('[aria-label="Connected"]'));
assert.equal(div.querySelector('[role="status"]'), null, 'actual connection ends setup automatically');
await act(async () => root.unmount());

blocked = true; connected = false;
({ div, root } = await make());
await finishStart();
assert.match(div.querySelector('[role="status"]').textContent, /Open the sign-in page/);
assert.ok(div.querySelector('a[href="' + authorization + '"]'), 'blocked popup still has an actionable link');
await act(async () => root.unmount());

blocked = false;
({ div, root } = await make());
await finishStart('javascript:alert(1)');
assert.equal(navigated, undefined);
assert.equal(closed, true);
assert.equal(div.querySelector('[role="status"]'), null);
assert.match(div.querySelector('.message-error').textContent, /unsupported sign-in page/);
await act(async () => root.unmount());
console.log('Mobile tap-time consent, fallback, verified status and unsafe URL rejection: passed');
