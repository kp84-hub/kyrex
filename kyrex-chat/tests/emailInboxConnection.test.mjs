import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import EmailInboxConnection from '../src/components/EmailInboxConnection.jsx';

const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div);
const calls = [];
let view = { source: 'gmail', personal_gmail_enabled: true, connected: false, active: false };
let fail = false, changes = 0;
globalThis.fetch = async (url, init) => {
  const body = init.body ? JSON.parse(init.body) : null;
  calls.push({ url, method: init.method, body });
  if (fail) return { ok: false, json: async () => ({ detail: 'AgentMail unavailable. Personal Gmail was not used.' }) };
  if (url.endsWith('/source')) view = { ...view, source: body.source, personal_gmail_enabled: body.source === 'gmail', active: false };
  if (url.endsWith('/connect')) {
    assert.deepEqual(Object.keys(body).sort(), ['api_key', 'inbox_id']);
    view = { source: 'agentmail', personal_gmail_enabled: false, connected: true, active: true,
      inbox_id: body.inbox_id, address: 'school@agentmail.to' };
  }
  if (url.endsWith('/disconnect')) view = { ...view, source: 'none', active: false, connected: false };
  return { ok: true, json: async () => view };
};
const button = text => [...div.querySelectorAll('button')].find(item => item.textContent === text);
const edit = async (input, value) => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};
await act(async () => root.render(React.createElement(EmailInboxConnection, { onChange: () => changes++ })));
assert.equal(calls.length, 1, 'Opening settings only reads status');
await act(async () => button('Stop personal Gmail reads').click());
assert.equal(view.source, 'none');
assert.ok(!calls.some(c => c.url.includes('/google/disconnect')), 'Calendar OAuth grant preserved');
await edit(div.querySelector('input'), 'inb_school');
await edit(div.querySelector('input[type=password]'), 'test-inbox-key-' + 'x'.repeat(30));
await act(async () => button('Connect dedicated inbox').click());
assert.equal(view.source, 'agentmail');
assert.equal(div.querySelector('input[type=password]').value, '', 'Saved secret never repopulated');
assert.match(div.textContent, /Keep forwarding for all incoming mail turned off/);
assert.match(div.textContent, /Personal Gmail reads are blocked/);
let copied;
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { copied = text; } } });
await act(async () => button('Copy inbox address').click());
assert.equal(copied, 'school@agentmail.to');
fail = true;
await act(async () => button('Connect dedicated inbox').click());
assert.equal(view.source, 'agentmail');
assert.match(div.querySelector('[role=alert]').textContent, /Personal Gmail was not used/);
assert.ok(!calls.some(c => c.url.includes('upgrade-gmail')), 'No consent or Gmail fallback');
fail = false;
await act(async () => button('Disconnect AgentMail').click());
assert.equal(view.source, 'none');
assert.equal(button('Copy inbox address'), undefined);
await act(async () => button('Use personal Gmail').click());
assert.equal(view.source, 'gmail', 'Only explicit personal-Gmail choice reenables source');
assert.equal(changes, 4);
await act(async () => root.unmount());
console.log('Dedicated inbox setup, explicit source changes, failed reads and secret clearing: passed');
