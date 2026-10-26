import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AccountGate from '../src/components/AccountGate.jsx';
import FamilySettings from '../src/components/FamilySettings.jsx';

const options = { enabled: true, github: true, google: true, email: true, managed: true, signup_mode: 'invite' };
const calls = [];
let mode = 'signed-out';
globalThis.fetch = async (url, init = {}) => {
  calls.push([url, init]);
  let data;
  if (url === '/auth/options') data = mode === 'legacy' ? { enabled: false } : options;
  else if (url === '/api/me') data = { authenticated: mode !== 'signed-out', username: 'test-user' };
  else if (url === '/api/account') data = { account: {
    id: 'test-id', owner: 'account_test', kind: mode === 'managed' ? 'managed' : 'independent',
    complete: mode !== 'profile', name: mode === 'managed' ? 'Avery' : 'Taylor', timezone: 'Europe/Rome',
  }, can_invite: true };
  else if (url === '/auth/email/start') data = { challenge: 'test-only-email-challenge' };
  else if (url === '/auth/email/verify') { mode = 'profile'; data = { signed_in: true }; }
  else if (url === '/api/account/profile') { mode = 'ready'; data = {}; }
  else if (url === '/api/account/family') data = { members: [] };
  else if (url === '/api/conversations') data = { conversations: [] };
  else data = {};
  return { ok: true, status: 200, json: async () => data };
};
const div = document.body.appendChild(document.createElement('div'));
let root;
const button = name => [...div.querySelectorAll('button')].find(e => e.textContent === name);
const input = async (selector, value) => act(async () => {
  const field = div.querySelector(selector);
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(field, value);
  field.dispatchEvent(new Event('input', { bubbles: true }));
});
const render = async () => {
  root = createRoot(div);
  await act(async () => root.render(React.createElement(AccountGate, null, React.createElement('p', { id: 'existing-app' }, 'Existing Chat'))));
};
await render();
assert.match(div.textContent, /invitation-only/);
assert.ok(button('Continue with Google').disabled);
assert.equal(calls.some(([url]) => url === '/api/chat/onboarding' || url === '/api/chat'), false);
await act(async () => div.querySelector('input[type=checkbox]').click());
assert.equal(button('Continue with Google').disabled, false);
await input('input[type=email]', 'taylor@example.test');
await act(async () => button('Continue with email').click());
assert.ok(div.querySelector('input[autocomplete=one-time-code]'));
await input('input[autocomplete=one-time-code]', '12345678');
await act(async () => button('Verify and continue').click());
assert.match(div.textContent, /Make yourself at home/);
assert.equal(div.querySelector('#existing-app'), null);
await input('input[autocomplete=nickname]', 'Taylor');
// The adult acknowledgment carries over this explicit sign-up in this tab.
await act(async () => button('Continue to setup').click());
assert.ok(div.querySelector('#existing-app'));
assert.deepEqual(JSON.parse(calls.find(([url]) => url === '/api/account/profile')[1].body), {
  name: 'Taylor', timezone: 'Europe/Rome', adult: true,
});
await act(async () => root.unmount());

mode = 'legacy'; calls.length = 0;
await render();
assert.ok(div.querySelector('#existing-app'));
assert.deepEqual(calls.map(([url]) => url), ['/auth/options']);
await act(async () => root.unmount());

mode = 'managed'; calls.length = 0;
await render();
assert.match(div.textContent, /Your parent manages this account and can review your chats/);
assert.ok(div.querySelector('textarea'));
assert.equal(div.querySelector('#existing-app'), null);
assert.equal(div.querySelector('input[type=file]'), null);
assert.equal(calls.some(([url]) => /provider|connections|onboarding|bots|voice/.test(url)), false);
await act(async () => root.unmount());

mode = 'ready'; calls.length = 0;
root = createRoot(div);
await act(async () => root.render(React.createElement(FamilySettings)));
assert.match(div.textContent, /Create a parent-managed account/);
assert.ok(button('Create managed account').disabled);
assert.match(div.textContent, /Deletion from Kyrex does not delete copies retained by the model provider/);
assert.equal(calls.some(([, init]) => init.method === 'POST'), false, 'No accounts, emails or model requests are created on load');
await act(async () => root.unmount());
console.log('Account sign-up, profile setup, legacy bypass, managed UI isolation and parent consent controls passed.');
