import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import PrivacySettings from '../src/components/PrivacySettings.jsx';

let saved = true;
let reject = false;
const calls = [];
globalThis.fetch = async (url, opts = {}) => {
  calls.push({ url, ...opts });
  if (opts.method === 'PUT') {
    if (reject) return { ok: false, status: 503, statusText: 'Unavailable', async json() { return { detail: 'Save unavailable' }; } };
    saved = JSON.parse(opts.body).share_saved_memory;
  }
  return { ok: true, status: 200, async json() { return { share_saved_memory: saved, secret_filter_enabled: true }; } };
};
const container = document.createElement('div');
document.body.appendChild(container);
const root = createRoot(container);
await act(async () => { root.render(React.createElement(PrivacySettings)); });
let checkbox = container.querySelector('input');
assert.equal(checkbox.checked, true);
assert.match(container.textContent, /can still reach your selected provider/);
await act(async () => { checkbox.click(); });
assert.equal(container.querySelector('input').checked, false);
assert.deepEqual(JSON.parse(calls.find((call) => call.method === 'PUT').body), { share_saved_memory: false });
assert.equal(calls[0].url, '/api/chat/privacy');
reject = true;
await act(async () => { container.querySelector('input').click(); });
assert.equal(container.querySelector('input').checked, false);
assert.match(container.querySelector('[role="alert"]').textContent, /Save unavailable/);
await act(async () => { root.unmount(); });
await act(async () => { createRoot(container).render(React.createElement(PrivacySettings)); });
assert.equal(container.querySelector('input').checked, false);
console.log('Privacy settings load, save, reload, and failure handling passed.');
