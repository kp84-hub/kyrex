import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import ProviderSettings from '../src/components/ProviderSettings.jsx';

let profiles = [{ id: 'opencode', name: 'Opencode', provider: 'opencode',
  base_url: 'https://opencode.ai/zen/go/v1', models: ['deepseek-v4.1-flash'],
  has_api_key: true, api_key_last4: 'fake', header_names: ['Key', 'Value'] },
  { id: 'opencode_gpt', name: 'Opencode_GPT', provider: 'opencode',
    base_url: 'https://opencode.ai/zen/go/v1/responses', models: ['gpt-6-luna'],
    has_api_key: true, api_key_last4: 'fake', header_names: [] }];
const calls = [];
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null });
  let value = {};
  if (url === '/api/chat/provider-profiles') {
    if (options.method === 'POST') {
      const body = JSON.parse(options.body);
      profiles = profiles.map(p => p.id === body.id ? { ...p, ...body,
        header_names: Object.hasOwn(body, 'headers') ? Object.keys(body.headers) : p.header_names } : p);
      value = profiles.find(p => p.id === body.id);
    } else value = { profiles };
  }
  if (url === '/api/chat/provider-profiles/opencode_gpt/consolidate') {
    profiles = profiles.filter(p => p.id !== 'opencode_gpt');
    value = profiles.find(p => p.id === 'opencode');
  }
  return { ok: true, json: async () => value };
};
const div = document.createElement('div'); document.body.append(div);
const root = createRoot(div);
let changed = 0;
await act(async () => root.render(React.createElement(ProviderSettings, {
  onSaved: () => changed++, installState: { installed: false, help: '', busy: false, install: () => {} },
})));
const form = () => div.querySelector('.provider-form');
const row = name => [...div.querySelectorAll('.provider-row')].find(el => el.querySelector('strong')?.textContent === name);
const edit = async name => act(async () => [...row(name).querySelectorAll('button')].find(b => b.textContent === 'Edit').click());
const submit = async () => act(async () => form().dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })));
const change = async (el, value) => act(async () => {
  const proto = el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
  el.dispatchEvent(new window.Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
});
const posts = () => calls.filter(c => c.url === '/api/chat/provider-profiles' && c.method === 'POST');

await edit('Opencode');
assert.equal(form().querySelector('input[type=password]').value, '');
assert.equal(form().querySelector('input[type=password]').required, false);
assert.equal(form().querySelector('input[placeholder^="Profile ID"]').disabled, true);
assert.match(form().textContent, /Key, Value/);
assert.equal(form().querySelector('textarea'), null, 'Saved secret header values are never reconstructed');
await submit();
assert.equal(Object.hasOwn(posts()[0].body, 'headers'), false, 'Ordinary edits preserve saved headers');
assert.equal(posts()[0].body.api_key, '', 'A stored key is preserved on the server');
assert.equal(form().querySelector('input[type=password]').required, true, 'New profiles still require a key');

await edit('Opencode');
await change(form().querySelector('[aria-label="Extra headers action"]'), 'clear');
await submit();
assert.deepEqual(posts()[1].body.headers, {});
assert.deepEqual(profiles[0].header_names, []);
assert.equal(profiles[0].id, 'opencode', 'Editing never changes Bot profile references');

await edit('Opencode_GPT');
await change(form().querySelector('input[type=url]'), 'https://opencode.ai/zen/go/v1');
await submit();
assert.equal(posts()[2].body.base_url, 'https://opencode.ai/zen/go/v1');
assert.equal(posts()[2].body.api_key, '');
assert.equal(Object.hasOwn(posts()[2].body, 'headers'), false);
assert.equal(changed, 3);
await act(async () => [...row('Opencode_GPT').querySelectorAll('button')].find(b => b.textContent === 'Use Opencode instead').click());
const consolidation = calls.find(c => c.url.endsWith('/consolidate'));
assert.equal(consolidation.method, 'POST');
assert.deepEqual(consolidation.body, { target_id: 'opencode' });
assert.equal(row('Opencode_GPT'), undefined);
assert.equal(changed, 4);
assert.equal(calls.some(c => /\/llm\/check|\/api\/chat$/.test(c.url)), false, 'Editing never runs a model or task');
await act(async () => root.unmount());
console.log('Provider editing, explicit header clearing, and saved-key preservation passed');
