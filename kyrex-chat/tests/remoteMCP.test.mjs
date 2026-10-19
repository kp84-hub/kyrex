import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import RemoteMCPConnection from '../src/components/RemoteMCPConnection.jsx';

const SECRET = 'offline-ui-credential';
const ENDPOINT = 'https://notes.example/private-mcp-session';
const ROW = { id: 'connection-one', label: 'Notes', host: 'notes.example', tested_at: 1790000000,
  has_key: true, status: 'tested', tools: [
    { name: 'read_notes', description: 'Search my notes', read_only: true, selected: false },
    { name: 'send_note', description: 'Send a note', read_only: false, selected: false },
  ] };

async function mount(handler, initial = []) {
  const calls = [];
  let rows = structuredClone(initial);
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const call = { url: String(url), method, body: options.body }; calls.push(call);
    const result = handler?.(call, rows);
    if (result?.rows) rows = result.rows;
    return { ok: !result?.status || result.status < 400, status: result?.status || 200,
      json: async () => result?.body || (method === 'GET' ? { connections: rows } : {}) };
  };
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(React.createElement(RemoteMCPConnection)));
  return { container, calls, async close() { await act(async () => root.unmount()); container.remove(); } };
}

function button(container, label) {
  return [...container.querySelectorAll('button')].find(item => item.textContent === label);
}
async function input(container, label, value) {
  const element = container.querySelector(`[aria-label="${label}"]`);
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new window.Event('input', { bubbles: true }));
  });
}

test('connect sends secrets only in the body and clears key and endpoint after testing', async () => {
  const view = await mount(call => call.method === 'POST' ? { rows: [ROW] } : null);
  try {
    await input(view.container, 'MCP connection name', 'Notes');
    await input(view.container, 'MCP endpoint', ENDPOINT);
    await input(view.container, 'MCP key', SECRET);
    await act(async () => view.container.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })));
    const call = view.calls.find(item => item.method === 'POST');
    assert.deepEqual(JSON.parse(call.body), { label: 'Notes', url: ENDPOINT, token: SECRET, auth_header: 'Authorization' });
    assert.equal(call.url, '/api/connections/mcp');
    assert.equal(view.container.querySelector('[aria-label="MCP key"]').value, '');
    assert.equal(view.container.querySelector('[aria-label="MCP endpoint"]').value, '');
    assert.ok(!view.container.textContent.includes(SECRET) && !view.container.textContent.includes(ENDPOINT));
    assert.equal(window.localStorage.getItem('mcpKey'), null);
    assert.match(view.container.textContent, /Select read-only tools/);
  } finally { await view.close(); }
});

test('rejected connection clears credentials and explains the actual failure', async () => {
  const view = await mount(call => call.method === 'POST' ? { status: 400, body: { detail: 'MCP authentication was rejected.' } } : null);
  try {
    await input(view.container, 'MCP endpoint', ENDPOINT); await input(view.container, 'MCP key', SECRET);
    await act(async () => view.container.querySelector('form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })));
    assert.match(view.container.querySelector('[role="alert"]').textContent, /authentication was rejected/);
    assert.equal(view.container.querySelector('[aria-label="MCP key"]').value, '');
  } finally { await view.close(); }
});

test('tools start unselected, writes are disabled and only selected reads are saved', async () => {
  const view = await mount(call => call.method === 'PUT' ? { rows: [{ ...ROW, status: 'enabled', tools: ROW.tools.map(tool => ({ ...tool, selected: tool.name === 'read_notes' })) }] } : null, [ROW]);
  try {
    const checks = view.container.querySelectorAll('input[type="checkbox"]');
    assert.equal(checks[0].checked, false); assert.equal(checks[1].disabled, true);
    await act(async () => checks[0].click());
    await act(async () => button(view.container, 'Save selected tools').click());
    const call = view.calls.find(item => item.method === 'PUT');
    assert.deepEqual(JSON.parse(call.body), { tools: ['read_notes'] });
    assert.match(view.container.textContent, /Selected tools enabled/);
  } finally { await view.close(); }
});

test('retest refreshes changed grants; disconnect requires confirmation', async () => {
  const view = await mount(call => call.method === 'DELETE' ? { rows: [] } : null, [ROW]);
  try {
    await act(async () => button(view.container, 'Test connection').click());
    assert.ok(view.calls.some(call => call.method === 'POST' && call.url.endsWith('/connection-one/test')));
    await act(async () => button(view.container, 'Disconnect').click());
    assert.equal(view.calls.some(call => call.method === 'DELETE'), false);
    await act(async () => button(view.container, 'Confirm disconnect').click());
    assert.equal(view.calls.filter(call => call.method === 'DELETE').length, 1);
    assert.equal(view.container.querySelector('[aria-label="MCP Notes"]'), null);
  } finally { await view.close(); }
});
