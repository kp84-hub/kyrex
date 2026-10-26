import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import DeveloperRepository from '../src/components/DeveloperRepository.jsx';
import BotSettings from '../src/components/BotSettings.jsx';
import { consentUrl } from '../src/lib/consentWindow.js';
let info = { repositories: ['alice/private'], repository: null, base: null, publisher_connected: false, can_publish: false };
let failPublish = false;
const requests = [];
let changed = 0, popupUrl = '';
window.open = () => ({ location: { replace(url) { popupUrl = url; } }, close() {} });
globalThis.fetch = async (url, options = {}) => {
  requests.push({ url, options });
  let payload = info;
  if (url.endsWith('/github-write/connect')) payload = { authorization_url: '/api/connections/github-write/setup?state=TEST' };
  if (url.endsWith('/repository') && options.method === 'POST') {
    info = { ...info, repository: JSON.parse(options.body).repository, base: 'main', can_publish: true, publisher_connected: true };
    payload = { repository: info.repository };
  }
  if (url.endsWith('/review')) payload = { repository: 'alice/private', base: 'main', snapshot: 'a'.repeat(64), files: ['app.py'], diff: '-old\n+new' };
  if (url.endsWith('/repository/refresh')) {
    info = { ...info, workspace: { status: 'current', behind: 0, ahead: 0 } };
    payload = { workspace: info.workspace };
  }
  if (url.endsWith('/publish')) {
    if (failPublish) return { ok: false, status: 409, json: async () => ({ detail: 'The workspace changed after review' }) };
    payload = { url: 'https://github.com/alice/private/pull/7' };
  }
  return { ok: true, json: async () => payload };
};
const node = document.body.appendChild(document.createElement('div'));
const root = createRoot(node);
const render = async (status) => act(async () => root.render(React.createElement(DeveloperRepository, { bot: { id: 'dev', status }, onChanged: () => changed++ })));
const button = (name) => [...node.querySelectorAll('button')].find(b => b.textContent === name);
await render('running');
await act(async () => button('Repository').click());
assert.match(node.textContent, /Pause this Bot/);
assert.equal(node.querySelector('select').disabled, true);
assert.equal(button('Use repository').disabled, true);
assert.equal(button('Refresh checkout').disabled, true);
await render('paused');
assert.equal(node.querySelector('select').disabled, false);
await act(async () => button('Enable PR publishing').click());
assert.equal(popupUrl, 'http://localhost/api/connections/github-write/setup?state=TEST');
assert.ok(node.querySelector('a[href="' + popupUrl + '"]'));
await act(async () => {
  node.querySelector('select').value = 'alice/private';
  node.querySelector('select').dispatchEvent(new Event('change', { bubbles: true }));
});
await act(async () => button('Use repository').click());
assert.equal(changed, 1);
assert.match(node.textContent, /Current: alice\/private/);
await act(async () => button('Review changes').click());
assert.match(node.querySelector('pre').textContent, /\+new/);
await act(async () => button('Refresh checkout').click());
assert.equal(node.querySelector('pre'), null, 'Refreshing invalidates the old PR review');
assert.match(node.textContent, /0 commits behind/);
assert.ok(requests.some(r => r.url.endsWith('/repository/refresh') && r.options.method === 'POST'));
await act(async () => button('Review changes').click());
assert.equal(button('Create pull request').disabled, true);
await act(async () => {
  const input = node.querySelector('input');
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'Fix source');
  input.dispatchEvent(new Event('input', { bubbles: true }));
});
assert.equal(button('Create pull request').disabled, false);
failPublish = true;
await act(async () => button('Create pull request').click());
assert.match(node.querySelector('[role="alert"]').textContent, /changed after review/);
assert.equal(node.querySelector('a[href*="/pull/"]'), null);
failPublish = false;
await act(async () => button('Create pull request').click());
assert.equal(node.querySelector('a[href*="/pull/"]').href, 'https://github.com/alice/private/pull/7');
const publish = requests.filter(r => r.url.endsWith('/publish')).at(-1);
assert.deepEqual(JSON.parse(publish.options.body), { snapshot: 'a'.repeat(64), title: 'Fix source', body: '' });
assert.equal(button('Create pull request').disabled, true);
await render('running');
assert.equal(node.querySelector('pre'), null);
assert.equal(consentUrl('/api/connections/github-write/setup?state=x'), 'http://localhost/api/connections/github-write/setup?state=x');
assert.throws(() => consentUrl('https://evil.test/api/connections/github-write/setup'));
await act(async () => root.unmount());

// Render the actual settings integration, including legacy/custom coding
// roles. A component-only test misses a selector hidden by its parent.
const settingsNode = document.body.appendChild(document.createElement('div'));
const settingsRoot = createRoot(settingsNode);
const codingBot = { id: 'dev', name: 'Renamed coding bot', manageable: true,
  status: 'stopped', role: { id: 'custom' }, repository_available: true };
const renderSettings = async (bot) => act(async () => settingsRoot.render(
  React.createElement(BotSettings, { bots: [bot] })));
const repositoryButton = () => [...settingsNode.querySelectorAll('button')]
  .find(b => b.textContent === 'Repository');
await renderSettings(codingBot);
assert.ok(repositoryButton(), 'legacy coding grants expose repository controls');
await act(async () => repositoryButton().click());
assert.ok(settingsNode.querySelector('[aria-label="Developer repository"]'));
await renderSettings({ ...codingBot, repository_available: false });
assert.equal(repositoryButton(), undefined, 'server denial takes precedence');
await renderSettings({ ...codingBot, name: 'Developer Bot', repository_available: false });
assert.equal(repositoryButton(), undefined, 'display names do not grant repository access');
await renderSettings({ ...codingBot, repository_available: false, role: { id: 'developer' } });
assert.equal(repositoryButton(), undefined, 'server denial overrides legacy role');
await renderSettings({ ...codingBot, repository_available: undefined, role: { id: 'developer' } });
assert.ok(repositoryButton(), 'older backend developer role remains supported');
await act(async () => settingsRoot.unmount());
console.log('Developer repository selection, publisher consent, paused review, stale diff errors and confirmed PR links: passed');
