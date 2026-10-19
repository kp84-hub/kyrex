import test from 'node:test';
import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import ComposioApps from '../src/components/ComposioApps.jsx';

const APP = { slug: 'notion', name: 'Notion', description: 'Read selected pages.' };
const PENDING = { id: 'attempt-one', label: 'Notion via Composio', setup_status: 'awaiting_sign_in' };

async function mount(handler, initial = { configured: true, apps: [APP], connections: [] }) {
  const calls = []; let data = structuredClone(initial); let changes = 0;
  globalThis.fetch = async (url, options = {}) => {
    const call = { url: String(url), method: options.method || 'GET', body: options.body };
    calls.push(call); const result = handler?.(call) || {};
    if (result.data) data = result.data;
    return { ok: !result.status || result.status < 400, status: result.status || 200,
      json: async () => result.body || data };
  };
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(React.createElement(ComposioApps, { onChange: () => { changes++; } })));
  return { calls, container, changes: () => changes, async close() { await act(async () => root.unmount()); container.remove(); } };
}
function button(c, text) { return [...c.querySelectorAll('button')].find(b => b.textContent === text); }

test('app catalog disables connect without deployment configuration', async () => {
  const v = await mount(null, { configured: false, apps: [APP], connections: [] });
  try {
    assert.equal(button(v.container, 'Connect Notion').disabled, true);
    assert.match(v.container.textContent, /server setup/);
    assert.equal(v.container.querySelector('input[type="password"]'), null);
  } finally { await v.close(); }
});

test('sign-in uses the owner-authenticated server flow, followed by explicit completion', async () => {
  const v = await mount(call => {
    if (call.url.endsWith('/apps/notion')) return { body: { id: PENDING.id, redirect_url: 'https://connect.composio.dev/link/offline-attempt' },
      data: { configured: true, apps: [APP], connections: [PENDING] } };
    if (call.url.endsWith('/complete')) return { body: {}, data: { configured: true, apps: [APP], connections: [] } };
  });
  try {
    await act(async () => button(v.container, 'Connect Notion').click());
    const start = v.calls.find(c => c.method === 'POST');
    assert.equal(start.url, '/api/connections/composio/apps/notion');
    assert.deepEqual(JSON.parse(start.body), {});
    const link = v.container.querySelector('a');
    assert.equal(link.href, 'https://connect.composio.dev/link/offline-attempt');
    assert.match(link.rel, /noopener/);
    assert.equal(window.localStorage.getItem('composioKey'), null);
    await act(async () => button(v.container, 'I finished signing in').click());
    assert.ok(v.calls.some(c => c.url === '/api/connections/composio/attempt-one/complete'));
    assert.match(v.container.textContent, /Select read-only tools/);
    assert.equal(v.container.querySelector('a'), null);
    assert.equal(v.changes(), 2);
  } finally { await v.close(); }
});

test('pending sign-in is recoverable after reload and can be cancelled', async () => {
  const v = await mount(call => call.method === 'DELETE' ? { data: { configured: true, apps: [APP], connections: [] } } : null,
    { configured: true, apps: [APP], connections: [PENDING] });
  try {
    assert.ok(button(v.container, 'I finished signing in'));
    await act(async () => button(v.container, 'Cancel setup').click());
    assert.ok(v.calls.some(c => c.method === 'DELETE' && c.url === '/api/connections/mcp/attempt-one'));
    assert.equal(button(v.container, 'I finished signing in'), undefined);
  } finally { await v.close(); }
});

test('untrusted sign-in URLs are never rendered', async () => {
  const v = await mount(call => call.method === 'POST' ? { body: { id: 'bad', redirect_url: 'https://evil.example/link/bad' } } : null);
  try {
    await act(async () => button(v.container, 'Connect Notion').click());
    assert.equal(v.container.querySelector('a'), null);
    assert.match(v.container.querySelector('[role="alert"]').textContent, /unsupported sign-in link/);
  } finally { await v.close(); }
});

test('expired sessions give a sign-in message and never claim the app connected', async () => {
  const v = await mount(call => call.method === 'POST' ? { status: 401, body: { detail: 'Sign in' } } : null);
  try {
    await act(async () => button(v.container, 'Connect Notion').click());
    assert.match(v.container.querySelector('[role="alert"]').textContent, /Sign in again/);
    assert.equal(v.container.querySelector('a'), null);
  } finally { await v.close(); }
});
