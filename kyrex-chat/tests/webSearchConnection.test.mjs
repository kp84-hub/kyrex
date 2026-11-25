import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import WebSearchConnection from '../src/components/WebSearchConnection.jsx';

const KEY = 'tvly-offline-ui-search-credential';
async function mount({ configured = false, fail = false, requiresReconnect = false } = {}) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET'; calls.push({ url, ...options, method });
    if (fail && method === 'PUT') return { ok: false, status: 422, json: async () => ({ detail: KEY }) };
    if (method === 'PUT') { configured = true; requiresReconnect = false; }
    if (method === 'DELETE') { configured = false; requiresReconnect = false; }
    return { ok: true, status: 200, json: async () => ({ provider: 'tavily', configured, requires_reconnect: requiresReconnect }) };
  };
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(React.createElement(WebSearchConnection)));
  return { container, calls, async close() { await act(async () => root.unmount()); container.remove(); } };
}
const button = (container, label) => [...container.querySelectorAll('button')].find(b => b.textContent === label);
async function enter(container) {
  const element = container.querySelector('[aria-label="Tavily Search API key"]');
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(element, KEY);
    element.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
}
async function submit(container) {
  await act(async () => container.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })));
}

test('key uses only the authenticated body, is cleared, and is never called verified', async () => {
  const view = await mount();
  try {
    await enter(view.container); await submit(view.container);
    const call = view.calls.find(c => c.method === 'PUT');
    assert.equal(call.url, '/api/connections/web-search');
    assert.deepEqual(JSON.parse(call.body), { key: KEY });
    assert.equal(call.cache, 'no-store');
    assert.equal(view.container.querySelector('input').value, '');
    assert.ok(!view.container.textContent.includes(KEY));
    assert.match(view.container.textContent, /API access will be checked when you search/);
    assert.equal(window.localStorage.getItem('webSearchKey'), null);
    assert.ok(button(view.container, 'Replace key'));
  } finally { await view.close(); }
});

test('rejected saves clear secrets and do not echo untrusted validation bodies', async () => {
  const view = await mount({ fail: true });
  try {
    await enter(view.container); await submit(view.container);
    assert.match(view.container.querySelector('[role="alert"]').textContent, /could not be saved/);
    assert.equal(view.container.querySelector('input').value, '');
    assert.ok(!view.container.textContent.includes(KEY));
  } finally { await view.close(); }
});

test('disconnect requires confirmation and removes the configured state', async () => {
  const view = await mount({ configured: true });
  try {
    await act(async () => button(view.container, 'Disconnect web search').click());
    assert.equal(view.calls.filter(c => c.method === 'DELETE').length, 0);
    await act(async () => button(view.container, 'Confirm disconnect').click());
    assert.equal(view.calls.filter(c => c.method === 'DELETE').length, 1);
    assert.ok(button(view.container, 'Save key'));
  } finally { await view.close(); }
});


test('old provider connection explains reconnection and can be replaced or removed', async () => {
  const view = await mount({ requiresReconnect: true });
  try {
    assert.match(view.container.textContent, /Replace your saved Brave key with a Tavily key/);
    assert.ok(button(view.container, 'Disconnect web search'));
    assert.ok(button(view.container, 'Save key'));
    await enter(view.container); await submit(view.container);
    assert.ok(!view.container.textContent.includes('Replace your saved Brave key'));
    assert.ok(button(view.container, 'Replace key'));
  } finally { await view.close(); }
});
