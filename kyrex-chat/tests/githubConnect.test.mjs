import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import ConnectionsSettings from '../src/components/ConnectionsSettings.jsx';
import { consentUrl } from '../src/lib/consentWindow.js';
let connected = false, blocked = false, opened = 0, navigated = '', requestBody;
const startUrl = window.location.origin + '/api/connections/github/setup?state=TEST';
window.open = () => { opened++; return blocked ? null : { closed: false, location: { replace(url) { navigated = url; } } }; };
globalThis.fetch = async (url, opts = {}) => {
  if (url.endsWith('/github/connect') || url.endsWith('/github/manage')) {
    requestBody = opts.body;
    return { ok: true, json: async () => ({ authorization_url: startUrl }) };
  }
  return { ok: true, json: async () => ({ connectors: [{ provider: 'github', connected,
    usable: connected, configured: true, status: connected ? 'connected' : 'disconnected',
    capabilities: { bots: { github_reader: { capabilities: ['github.read'] } } } }] }) };
};
const make = async () => {
  const container = document.createElement('div'); document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(React.createElement(ConnectionsSettings)));
  return { container, root };
};
let { container, root } = await make();
assert.equal(container.querySelector('input[type="password"]'), null);
assert.ok(!container.textContent.includes('Fine-grained token'));
await act(async () => container.querySelector('[aria-label="Connect GitHub"]').click());
assert.equal(opened, 1);
assert.equal(navigated, startUrl);
assert.equal(requestBody, undefined, 'No token or manual repositories are submitted');
assert.ok(container.querySelector('a[href="' + startUrl + '"]'));
assert.equal(container.querySelector('[aria-label="Connected"]'), null);
connected = true;
await act(async () => window.dispatchEvent(new Event('focus')));
assert.ok(container.querySelector('[aria-label="Connected"]'));
const manage = [...container.querySelectorAll('button')].find(b => b.textContent === 'Manage repositories');
assert.ok(manage);
await act(async () => manage.click());
assert.equal(opened, 2);
await act(async () => root.unmount());
connected = false; blocked = true;
({ container, root } = await make());
await act(async () => container.querySelector('[aria-label="Connect GitHub"]').click());
assert.match(container.querySelector('[role="status"]').textContent, /Open the sign-in page/);
assert.ok(container.querySelector('a[href="' + startUrl + '"]'));
await act(async () => root.unmount());
for (const url of ['https://github.com/login/oauth/authorize?state=TEST', 'https://github.com/apps/kyrex-reader/installations/new?state=TEST']) {
  assert.equal(consentUrl(url), url);
}
for (const url of ['https://github.com.evil.test/login/oauth/authorize', 'https://evil.test/api/connections/github/setup', 'https://github.com/user/repo', 'https://github.com:444/login/oauth/authorize', 'javascript:alert(1)']) {
  assert.throws(() => consentUrl(url));
}
console.log('GitHub Connect: no token form, mobile consent/fallback, actual completion, repository management and URL validation passed');
