import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import ConnectionsSettings from '../src/components/ConnectionsSettings.jsx';
import MessagesSetup from '../src/components/MessagesSetup.jsx';
import { androidNativeIntent } from '../src/lib/androidHealth.js';

const calls = [];
let synced = false, phoneState = 'not_linked';
Object.defineProperty(document, 'hidden', { configurable: true, value: false });
Object.defineProperty(navigator, 'userAgent', { configurable: true, value: 'Desktop' });
globalThis.fetch = async (url, options) => {
  calls.push({ url, method: options?.method });
  assert.ok(!options?.method || options.method === 'GET', 'Viewing/opening setup must never grant access or send');
  const data = url === '/api/android/config' ? { messages: true, origin: window.location.origin }
    : url === '/api/connections' ? { connectors: [{ provider: 'device_messages', configured: true,
      mode: 'android_companion', paired: synced, connected: synced, synced_at: synced ? 1793660000 : null,
      status: synced ? 'connected' : 'disconnected', read_only: true,
      capabilities: { bots: { messages_reader: { capabilities: ['messages.read'] } } } }] }
    : url === '/api/connections/messages/status' ? { connected: synced, synced_at: synced ? 1793660000 : null,
      phone: { status: phoneState, expires_in: 45, send_ready: false } }
    : {};
  return { ok: true, json: async () => data };
};
const node = document.createElement('div'); document.body.append(node);
const root = createRoot(node);
await act(async () => root.render(React.createElement(ConnectionsSettings)));
const card = () => node.querySelector('[aria-label="Messages connector"]');
assert.equal(card().open, false);
await act(async () => card().querySelector('summary button').click());
assert.equal(card().open, true, 'Desktop Connect opens the guide instead of issuing a pairing code or showing an error');
assert.equal(node.querySelector('.connections-hub > [role="alert"]')?.textContent || '', '');
assert.match(card().textContent, /Open Kyrex on your Android phone/);
assert.equal(card().querySelectorAll('[aria-label="Messages setup guide"] li').length, 4);
assert.equal(card().querySelector('a[href^="intent:"]'), null, 'Desktop cannot launch phone intents');
assert.equal(calls.some(c => c.url.endsWith('/messages/connect')), false);
assert.doesNotMatch(card().textContent, /Live phone status: Messages connected/);

synced = true; phoneState = 'offline';
await act(async () => document.dispatchEvent(new Event('visibilitychange')));
assert.match(card().textContent, /Saved texts available/);
assert.match(card().textContent, /Last synced/);
assert.match(card().textContent, /New sync and confirmed sends need a live phone connection/);
assert.equal(card().querySelectorAll('[aria-label="Messages setup guide"] li').length, 0, 'Existing sync shows management instead of repeating onboarding');
assert.doesNotMatch(card().textContent, /Live phone status: Messages connected/);
assert.equal(calls.some(c => c.method && c.method !== 'GET'), false);
await act(async () => root.unmount());

const second = createRoot(node);
await act(async () => second.render(React.createElement(MessagesSetup, { onAndroid: true, savedTexts: false, sendEnabled: false })));
assert.equal(node.querySelector('a').href, androidNativeIntent('messages'));
assert.match(node.textContent, /Start phone setup/);
assert.match(node.textContent, /Chat sending is off/);
assert.match(node.textContent, /install or update Kyrex for Android/);
await act(async () => second.render(React.createElement(MessagesSetup, { onAndroid: true, savedTexts: true, sendEnabled: true })));
assert.match(node.querySelector('a').textContent, /Manage Messages/);
assert.match(node.textContent, /Confirmed sending from Chat is enabled/);
await act(async () => second.unmount()); node.remove();
console.log('Guided Messages setup, desktop handoff, existing sync, offline phone and consent separation: passed');
