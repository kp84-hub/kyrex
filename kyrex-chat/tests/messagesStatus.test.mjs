import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import MessagesStatus from '../src/components/MessagesStatus.jsx';
import ConnectionsSettings from '../src/components/ConnectionsSettings.jsx';

let clock = 1000000, state = 'ready', failed = false, calls = [];
const originalNow = Date.now;
Date.now = () => clock;
Object.defineProperty(document, 'hidden', { configurable: true, value: false });
const originalInterval = globalThis.setInterval, originalClear = globalThis.clearInterval;
const intervals = new Map(); let next = 0;
globalThis.setInterval = (callback, delay) => { const id = ++next; intervals.set(id, { callback, delay }); return id; };
globalThis.clearInterval = id => intervals.delete(id);
const phone = () => ({ paired: true, connected: true, synced_at: 900,
  phone: { status: state, last_seen: 1000, expires_in: 45, send_ready: state === 'ready' } });
globalThis.fetch = async (url, options) => {
  calls.push(url);
  assert.equal(options?.method, undefined, 'Settings must never mutate or send');
  if (url === '/api/connections') return { ok: true, json: async () => ({ connectors: [{
    provider: 'device_messages', mode: 'android_companion', paired: true, connected: true,
    status: 'connected', configured: true, synced_at: 900,
    capabilities: { bots: { messages_reader: { capabilities: ['messages.read'] } } },
  }] }) };
  assert.equal(url, '/api/connections/messages/status');
  assert.equal(options.cache, 'no-store');
  if (failed) throw Error('Network unavailable');
  return { ok: true, json: async () => phone() };
};
const node = document.createElement('div'); document.body.append(node);
const root = createRoot(node);
const tick = async delay => { await act(async () => {
  for (const entry of [...intervals.values()]) if (entry.delay === delay) await entry.callback();
}); };
await act(async () => root.render(React.createElement(MessagesStatus)));
assert.match(node.textContent, /Messages connected/);
assert.match(node.textContent, /Ready for sends you confirm/);
assert.match(node.textContent, /Saved texts are available/);
clock += 45000; await tick(1000);
assert.match(node.textContent, /Phone not reachable/);
assert.doesNotMatch(node.textContent, /Ready for sends you confirm/);
state = 'reconnecting'; await tick(5000);
assert.match(node.textContent, /Messages reconnecting/);
state = 'needs_attention'; await tick(5000);
assert.match(node.textContent, /open the phone companion to reconnect/);
failed = true; await tick(5000);
assert.match(node.textContent, /Could not check live phone status/);
assert.match(node.textContent, /Saved texts are available/);
failed = false;
Object.defineProperty(document, 'hidden', { configurable: true, value: true });
const before = calls.length; await tick(5000); assert.equal(calls.length, before);
Object.defineProperty(document, 'hidden', { configurable: true, value: false });
state = 'ready';
await act(async () => document.dispatchEvent(new Event('visibilitychange')));
assert.match(node.textContent, /Messages connected/);
await act(async () => root.render(React.createElement(ConnectionsSettings)));
assert.equal(intervals.size, 0, 'Closing the live panel stops its poll and clock');
const card = () => node.querySelector('[aria-label="Messages connector"]');
assert.equal(node.querySelector('[aria-label="Live Messages connection"]'), null, 'Collapsed settings should not poll');
assert.match(card().textContent, /Saved texts available/);
await act(async () => { card().open = true; card().dispatchEvent(new Event('toggle')); });
assert.match(node.textContent, /Live phone status: Messages connected/);
assert.ok(intervals.size > 0);
await act(async () => { card().open = false; card().dispatchEvent(new Event('toggle')); });
assert.equal(intervals.size, 0);
assert.equal(node.querySelector('[aria-label="Live Messages connection"]'), null);
await act(async () => root.unmount());
globalThis.setInterval = originalInterval; globalThis.clearInterval = originalClear; Date.now = originalNow;
console.log('Settings-only live presence, expiry, visibility, read-only requests and cleanup: passed');
