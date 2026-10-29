import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import BudgetSettings from '../src/components/BudgetSettings.jsx';

const node = document.body.appendChild(document.createElement('div'));
let root = createRoot(node), writes = [], opened = [];
let settings = { enabled: false, bot_id: '', interval_seconds: 3600, dollar_currency: 'USD', time_zone: 'UTC', since: null, last_scan: null, detail: '' };
let bots = [], entry = { id: 'one', revision: 'a'.repeat(32), merchant: 'Acme', kind: 'purchase',
  category: 'software', date: '2026-10-29', amount_minor: 1999, currency: 'USD', cadence: '', renewal_date: '',
  state: 'review', needs: ['possible duplicate receipt'], source: { label: 'shop@example.test', received: '2026-10-29T12:00:00Z' },
  date_basis: 'email date (UTC)', reference: '', evidence: '<script>Never execute email text</script>' };
const response = data => ({ ok: true, json: async () => data });
globalThis.fetch = async (url, init = {}) => {
  const data = init.body ? JSON.parse(init.body) : undefined;
  if (init.method && init.method !== 'GET') writes.push({ url, method: init.method, data });
  if (url === '/api/bots') return response({ id: 'budget' });
  if (url === '/api/bots/budget') {
    bots = [{ id: 'budget', name: 'Budget Bot', model: 'chosen-model' }];
    return response(bots[0]);
  }
  if (url.includes('/entries/')) {
    assert.equal(data.revision, entry.revision);
    entry = { ...entry, ...data.fields, needs: [], revision: 'b'.repeat(32) };
    return response({ saved: true });
  }
  if (url.includes('/ledger?')) return response({ totals_minor: {}, review_count: entry.state === 'review' ? 1 : 0,
    categories: [], subscriptions: [], entries: [entry], entries_truncated: false, limitation: 'Email-derived records only.' });
  if (init.method === 'PUT') settings = { ...settings, ...data };
  return response({ settings, bots, configured: !!settings.bot_id, conversation_id: 'budget-chat',
    bot_template: { name: 'Budget Bot', systemPrompt: 'Use budget_read', policy: { 'budget:read': 0, '*': 'deny' } } });
};
const render = () => act(async () => root.render(React.createElement(BudgetSettings, {
  profiles: [{ id: 'mine', name: 'My provider', models: ['chosen-model'] }], onOpenConversation: id => opened.push(id),
})));
const button = text => [...node.querySelectorAll('button')].find(b => b.textContent === text);
const select = async (element, value) => act(async () => {
  element.value = value;
  element.dispatchEvent(new Event('change', { bubbles: true }));
});
await render();
assert.equal(writes.length, 0, 'opening settings never starts a Bot or enables collection');
assert.match(node.textContent, /no backlog is imported/);
assert.match(node.textContent, /email text is sent to/);
let selects = node.querySelectorAll('select');
await select(selects[0], 'mine');
await select(node.querySelectorAll('select')[1], 'chosen-model');
await act(async () => button('Create Budget Bot').click());
assert.equal(writes[0].url, '/api/bots');
assert.equal(writes[0].data.provider_profile_id, 'mine');
assert.equal(writes[0].data.model, 'chosen-model');
assert.equal(writes[1].data.status, 'running');
assert.equal(writes.filter(w => w.method === 'PUT').length, 0, 'creating a Bot does not start email scans');
await act(async () => node.querySelector('input[type=checkbox]').click());
await act(async () => button('Save budget tracking').click());
assert.equal(writes.at(-1).data.enabled, true);
assert.equal(writes.at(-1).data.bot_id, 'budget');
assert.equal(node.querySelector('.email-automation-status').textContent, 'Enabled');
await act(async () => button('Open Budget Bot').click());
assert.deepEqual(opened, ['budget-chat']);
assert.equal(node.querySelector('script'), null, 'email evidence remains inert text');
await act(async () => button('Review entry').click());
const status = [...node.querySelectorAll('label')].find(l => l.textContent.startsWith('Ledger status')).querySelector('select');
await select(status, 'excluded');
await act(async () => button('Save entry').click());
assert.equal(writes.at(-1).data.fields.state, 'excluded');
assert.match(node.textContent, /excluded/);
await act(async () => root.unmount());
root = createRoot(node);
const count = writes.length;
await render();
assert.equal(node.querySelector('input[type=checkbox]').checked, true);
assert.equal(writes.length, count, 'reopening cannot enable, replay, or create anything');
await act(async () => node.querySelector('input[type=checkbox]').click());
await act(async () => button('Save budget tracking').click());
assert.equal(writes.at(-1).data.enabled, false);
assert.equal(node.querySelector('.email-automation-status').textContent, 'Paused');
await act(async () => root.unmount());
root = createRoot(node);
globalThis.fetch = async () => response({});
await render();
assert.match(node.textContent, /Budget (?:ledger|settings) could not be loaded/);
assert.ok(node.querySelector('[aria-label="Budget Bot"]'), 'malformed responses cannot crash the surrounding settings screen');
await act(async () => root.unmount());
node.remove();
console.log('Budget settings: explicit model, opt-in, review, persistence, pause and inert evidence passed.');
