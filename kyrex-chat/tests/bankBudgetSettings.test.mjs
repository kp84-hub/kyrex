import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import BankBudgetSettings from '../src/components/BankBudgetSettings.jsx';
import BudgetSettings from '../src/components/BudgetSettings.jsx';

const node = document.body.appendChild(document.createElement('div'));
let root = createRoot(node), writes = [], opened = [], modes = [], revokeFails = false;
const ref = 'a'.repeat(64), flow = 'b'.repeat(32);
let view = { available: true, environment: 'sandbox', state: 'connected', revision: 'c'.repeat(32),
  enabled: false, private_mode: false, bot_id: '', accounts: [{ ref, label: 'Checking 1', kind: 'depository' }],
  selected: [], start_date: '', starting_balance: '', currency: 'USD', time_zone: 'UTC', interval_seconds: 21600,
  last_scan: null, detail: '', flow, configured: false, conversation_id: 'budget-chat',
  bots: [{ id: 'budget', name: 'Budget Bot', model: 'owner-model', status: 'running' }] };
const response = (data, ok = true) => ({ ok, status: ok ? 200 : 409, json: async () => data });
globalThis.fetch = async (url, init = {}) => {
  const data = init.body ? JSON.parse(init.body) : undefined;
  if (init.method && init.method !== 'GET') writes.push({ url, method: init.method, data });
  if (url.includes('/finish')) { view = { ...view, state: 'connected' }; return response(view); }
  if (url.includes('/disconnect')) {
    if (revokeFails) {
      view = { ...view, state: 'disconnect_pending', enabled: false, revision: 'd'.repeat(32) };
      return response({ detail: 'Provider disconnection is pending.' }, false);
    }
    view = { ...view, state: 'disconnected', enabled: false, accounts: [], selected: [], configured: false };
    return response(view);
  }
  if (url.includes('/totals')) return response({ privacy: 'category_totals_only', categories: [
    { category: 'groceries', currency: 'USD', amount_minor: 14500 }], pending_count: 1,
    enabled: true, history_loaded: false, latest_attempt_ok: false,
    cash_estimate: { currency: 'USD', estimated_minor: 85500 }, limitation: 'Estimate from selected cash accounts only.' });
  if (init.method === 'PUT') {
    view = { ...view, ...data, private_mode: true, configured: true };
    return response(view);
  }
  if (url.includes('/budget/bank')) return response(view);
  // Private mode must hide the legacy receipt settings once bank status loads.
  return response({});
};
const render = Component => act(async () => root.render(React.createElement(Component, {
  profiles: [], onOpenConversation: id => opened.push(id), onMode: value => modes.push(value),
})));
const button = text => [...node.querySelectorAll('button')].find(b => b.textContent === text);
const control = text => [...node.querySelectorAll('label')].find(l => l.textContent.trim().startsWith(text))?.querySelector('input,select');
const select = (element, value) => act(async () => {
  element.value = value; element.dispatchEvent(new Event('change', { bubbles: true }));
});
const input = (element, value) => act(async () => {
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(element, value);
  element.dispatchEvent(new Event('input', { bubbles: true }));
});
await render(BankBudgetSettings);
assert.equal(writes.length, 0, 'opening settings never connects, enables or submits a bank action');
assert.match(node.textContent, /Sandbox mode uses test banks/);
assert.match(node.textContent, /excluded from its model input/);
await select(control('Private Budget Bot'), 'budget');
await act(async () => node.querySelector('fieldset input[type=checkbox]').click());
await input(control('Starting cash balance'), '1000.00');
await act(async () => control('Enable private bank tracking').click());
await act(async () => button('Save private bank tracking').click());
assert.equal(writes.at(-1).method, 'PUT');
assert.equal(writes.at(-1).data.enabled, true);
assert.equal(writes.at(-1).data.starting_balance, '1000.00');
assert.deepEqual(writes.at(-1).data.selected, [ref]);
assert.equal(modes.at(-1), true);
assert.ok(!('access_token' in writes.at(-1).data));
await act(async () => button('Open private Budget Bot').click());
assert.deepEqual(opened, ['budget-chat']);
await act(async () => button('Refresh private totals').click());
assert.match(node.textContent, /\$145\.00/);
assert.match(node.textContent, /\$855\.00/);
assert.match(node.textContent, /totals may be incomplete/);
assert.match(node.textContent, /totals may be stale/);
await act(async () => button('Disconnect bank').click());
assert.equal(writes.filter(w => w.url.includes('/disconnect')).length, 0, 'disconnect needs an explicit owner confirmation');
revokeFails = true;
await act(async () => button('Disconnect and clear bank records').click());
assert.match(node.textContent, /Provider disconnection is pending/);
assert.match(node.textContent, /disconnect pending/);
revokeFails = false;
await act(async () => button('Disconnect and clear bank records').click());
assert.equal(writes.at(-1).data.revision, 'd'.repeat(32), 'a failed revoke refreshes the saved revision before retry');
assert.equal(button('Open private Budget Bot'), undefined);
await act(async () => root.unmount());

// Returning from Hosted Link verifies only this owner-bound opaque flow.
root = createRoot(node); writes = [];
view = { ...view, state: 'linking', flow, private_mode: false };
window.history.replaceState(null, '', `/?settings=budget&bank_link=${flow}`);
await render(BankBudgetSettings);
assert.equal(writes[0].url, '/api/automations/budget/bank/finish');
assert.deepEqual(writes[0].data, { flow });
assert.equal(writes.filter(w => w.method === 'PUT').length, 0, 'returning from bank sign-in never enables tracking');
assert.ok(!window.location.search.includes('bank_link'));
await act(async () => root.unmount());

root = createRoot(node);
view = { ...view, private_mode: true };
await render(BudgetSettings);
assert.equal(node.querySelector('[aria-label="Budget Bot"]'), null, 'private bank mode hides the old receipt/model-text flow');
assert.ok(node.querySelector('[aria-label="Private bank budget"]'));
await act(async () => root.unmount());
node.remove();
console.log('Private bank settings: opt-in, starting balance, safe totals, return flow, revocation retry and receipt exclusion passed.');
