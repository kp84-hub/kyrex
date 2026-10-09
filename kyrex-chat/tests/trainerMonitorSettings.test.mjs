import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import TrainerMonitorSettings from '../src/components/TrainerMonitorSettings.jsx';

let current = { enabled: false, delivery: 'group', bot_id: 'calendar', interval_seconds: 3600, horizon_days: 14 };
const requests = [];
let failSave = false;
const alerts = [{ id: 'unknown', day: '2099-10-12', starts_at: Date.now() / 1000 + 3600, version: 3,
  message: 'Your Monday class has a new trainer: Austin.', state: 'unknown', detail: 'Delivery could not be verified' },
{ id: 'past', day: '2000-01-03', starts_at: 0, version: 1,
  message: 'Your Monday class has a new trainer: Donna.', state: 'sent' }];
globalThis.fetch = async (url, options = {}) => {
  requests.push({ url, options });
  if (url === '/api/automations/level6-trainers' && options.method === 'PUT') {
    if (failSave) return { ok: false, status: 409, json: async () => ({ detail: 'Calendar Bot unavailable' }) };
    current = JSON.parse(options.body);
  }
  const data = url.endsWith('/history') ? { alerts } : url.endsWith('/resend') ? { queued: true }
    : url.endsWith('/reset') ? { reset: true }
    : { settings: current, bots: [{ id: 'calendar', name: 'Calendar Bot' }], service_ready: false,
      group_note: 'Connect the Calendar Bot Browser Host' };
  return { ok: true, status: 200, json: async () => data };
};
const node = document.body.appendChild(document.createElement('div'));
const root = createRoot(node);
await act(async () => root.render(React.createElement(TrainerMonitorSettings)));
assert.match(node.textContent, /Monitor not configured/);
assert.match(node.textContent, /Check the group before resending/);
assert.match(node.textContent, /Connect the Calendar Bot Browser Host/);
assert.equal(node.querySelector('input[type="number"]').value, '1');
assert.equal(node.querySelector('input[type="checkbox"]').checked, false);
assert.equal([...node.querySelectorAll('button')].filter(b => /Resend/.test(b.textContent)).length, 1);
await act(async () => node.querySelector('input[type="checkbox"]').click());
await act(async () => node.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
assert.deepEqual(current, { enabled: true, delivery: 'group', bot_id: 'calendar', interval_seconds: 3600, horizon_days: 14 });
assert.match(node.textContent, /Preferences saved/);
const resend = [...node.querySelectorAll('button')].find(b => /Resend/.test(b.textContent));
await act(async () => resend.click());
const call = requests.find(r => r.url.endsWith('/unknown/resend'));
assert.equal(call.options.method, 'POST');
const body = JSON.parse(call.options.body);
assert.equal(body.confirm, true);
assert.ok(body.request_id.length >= 16);
assert.match(node.textContent, /Resend queued for a fresh schedule check/);
await act(async () => [...node.querySelectorAll('button')].find(b => /Reset baseline/.test(b.textContent)).click());
assert.ok(requests.some(r => r.url.endsWith('/reset') && r.options.method === 'POST'));
assert.match(node.textContent, /seed the baseline silently/);
failSave = true;
await act(async () => node.querySelector('form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
assert.match(node.querySelector('[role="alert"]').textContent, /Calendar Bot unavailable/);
assert.equal(requests.some(r => /send_level6|messages.*send/.test(r.url)), false);
await act(async () => root.unmount());
console.log('Trainer settings, disabled defaults, visible unknown outcomes, explicit resend and baseline reset: passed');
