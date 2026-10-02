import assert from 'node:assert/strict';
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react';
import ConnectionsSettings from '../src/components/ConnectionsSettings.jsx';
import { buildHubModel } from '../src/lib/connectorRegistry.js';

let paired = false;
let synced = false;
const permanent = 'PHONE-CREDENTIAL-MUST-NEVER-REACH-DOM';
const view = () => ({ provider: 'device_messages', configured: true, paired,
  connected: synced, status: synced ? 'connected' : 'disconnected',
  synced_at: synced ? 1790935200 : null, read_only: true,
  upload_token: permanent,
  capabilities: { bots: { messages_reader: { capabilities: ['messages.read'] } } },
});
const calls = [];
globalThis.fetch = async (url) => {
  calls.push(url);
  let data;
  if (url === '/api/connections') data = { connectors: [view()] };
  else if (url.endsWith('/messages/connect')) data = { authorization_url: '/api/connections/messages/setup' };
  else if (url.endsWith('/messages/disconnect')) { paired = false; synced = false; data = {disconnected: true}; }
  else throw Error('Unexpected request ' + url);
  return { ok: true, async json() { return data; } };
};
let opened; window.open = (url) => { opened = url; };
const container = document.createElement('div');
document.body.append(container);
const root = createRoot(container);
await act(async () => root.render(React.createElement(ConnectionsSettings)));
const card = () => container.querySelector('[aria-label="Messages connector"]');
const button = (text) => [...card().querySelectorAll('button')].find(b => b.textContent.trim() === text);
assert.equal(card().open, false);
assert.match(card().textContent, /Google Messages on your phone/);
assert.equal(buildHubModel([]).available.find(c => c.id === 'messages').connectable, false);
await act(async () => button('Connect').click());
assert.equal(card().open, true);
assert.equal(opened, '/api/connections/messages/setup');
assert.match(card().textContent, /matching emoji/);
assert.equal(card().textContent.includes('Termux'), false);
assert.match(card().textContent, /recent conversations/);
assert.equal(container.textContent.includes(permanent), false);
assert.ok(card().querySelector('a[href="/api/connections/messages/setup"]'));
paired = true;
await act(async () => button('Check connection').click());
assert.ok(button('Disconnect'));
assert.equal(card().closest('section').getAttribute('aria-label'), 'Available');
synced = true;
await act(async () => button('Check connection').click());
assert.equal(card().closest('section').getAttribute('aria-label'), 'Connected');
assert.equal(container.textContent.includes(permanent), false);
await act(async () => button('Disconnect').click());
assert.equal(card().closest('section').getAttribute('aria-label'), 'Available');
assert.equal(card().textContent.includes('short-lived-pairing-code'), false);
assert.ok(calls.includes('/api/connections/messages/disconnect'));
await act(async () => root.unmount());
console.log('Messages web pairing, backend gating, status and revocation: passed');
