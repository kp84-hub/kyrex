import assert from 'node:assert/strict';
import React, { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import Onboarding from '../src/components/Onboarding.jsx';
import App from '../src/App.jsx';

const providers = [{ id: 'opencode', label: 'OpenCode Go' }, { id: 'openrouter', label: 'OpenRouter' }];
const initial = { required: true, step: 'provider', providers, profiles: [], model: '' };
let savedState = initial, failProbe = true, completed = null;
const calls = [];
globalThis.fetch = async (url, options = {}) => {
  calls.push([url, options]);
  let data = savedState;
  if (url.endsWith('/provider')) {
    const body = JSON.parse(options.body);
    assert.equal(body.api_key, 'test-only-onboarding-provider-key');
    savedState = { ...initial, step: 'model', profile_id: 'setup-test', profiles: [{ id: 'setup-test', name: 'OpenCode Go', has_api_key: true }] };
    data = savedState;
  } else if (url.endsWith('/models')) data = { models: ['mimo-v2.6-flash', 'custom/model:free'] };
  else if (url.endsWith('/test')) {
    if (failProbe) return { ok: false, status: 400, json: async () => ({ detail: 'The provider rejected your credentials or model access.' }) };
    savedState = { ...savedState, step: 'finish', model: JSON.parse(options.body).model, verified: true }; data = savedState;
  } else if (url.endsWith('/complete')) { savedState = { ...savedState, required: false, step: 'ready', bot_id: 'my-chief', conversation_id: 'my-welcome' }; data = savedState; }
  return { ok: true, json: async () => data };
};
const div = document.body.appendChild(document.createElement('div'));
let root = createRoot(div);
function Harness() {
  const [state, setState] = useState(initial);
  return state.required ? React.createElement(Onboarding, { state, onState: setState,
    onComplete: async value => { completed = value; }, onAdvanced: () => {} }) : React.createElement('p', null, 'Chat ready');
}
const button = text => [...div.querySelectorAll('button')].find(item => item.textContent === text);
const input = async (label, value) => act(async () => {
  const element = [...div.querySelectorAll('label')].find(item => item.firstChild.textContent === label).querySelector('input');
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(element, value);
  element.dispatchEvent(new Event('input', { bubbles: true }));
});
await act(async () => root.render(React.createElement(Harness)));
assert.equal(calls.length, 0, 'first screen makes no model/provider request');
assert.equal(button('Save and choose model').disabled, true);
await input('Provider API key', 'test-only-onboarding-provider-key');
await act(async () => button('Save and choose model').click());
assert.equal(div.querySelector('input[type=password]'), null, 'key entry is cleared after saving');
assert.deepEqual(calls.map(([url]) => url), ['/api/chat/onboarding/provider', '/api/chat/onboarding/models']);
assert.match(div.textContent, /may use your provider/);
await input('Model ID', 'custom/model:free');
await act(async () => button('Test connection').click());
assert.match(div.querySelector('[role=alert]').textContent, /rejected/);
assert.equal(button('Create Overwatcher and open Chat'), undefined, 'failure cannot unlock assistant creation');
assert.equal(calls.some(([url]) => url.endsWith('/complete')), false);
failProbe = false;
await act(async () => button('Test connection').click());
assert.match(div.textContent, /Connection verified/);
assert.equal(completed, null, 'successful model test still waits for explicit creation');
await act(async () => button('Create Overwatcher and open Chat').click());
assert.equal(completed.conversation_id, 'my-welcome');
assert.match(div.textContent, /Chat ready/);
assert.equal(calls.some(([url]) => url === '/api/chat'), false, 'setup never submits a chat turn');
await act(async () => root.unmount());

// A refreshed tab resumes from server state without storing credentials locally.
root = createRoot(div);
await act(async () => root.render(React.createElement(Onboarding, {
  state: { ...savedState, required: true, step: 'model', verified: false }, onState: () => {}, onComplete: () => {}, onAdvanced: () => {},
})));
assert.match(div.textContent, /Choose and test/);
assert.equal(div.querySelector('input[type=password]'), null);
assert.equal(div.querySelector('input[list]').value, 'custom/model:free');
await act(async () => root.unmount());

// App-level gate must use owner setup state, never globally advertised models.
const appCalls = [];
globalThis.fetch = async (url, options = {}) => {
  appCalls.push([url, options]);
  const data = url === '/api/chat/onboarding' ? initial : url === '/api/chat/providers'
    ? { providers: [{ id: 'host-global', label: 'Host model', models: ['host-model'] }] }
    : { conversations: [], bots: [], workspaces: [], profiles: [] };
  return { ok: true, json: async () => data };
};
root = createRoot(div);
await act(async () => root.render(React.createElement(App)));
assert.match(div.textContent, /Connect your AI provider/);
assert.equal(div.querySelector('.composer'), null);
assert.equal(button('Talk'), undefined);
assert.equal(appCalls.some(([, init]) => init.method === 'POST'), false);
assert.equal(appCalls.some(([url]) => url === '/api/chat'), false);
await act(async () => root.unmount());
console.log('first-run gate, explicit connection test, failure handling, resume and Chat handoff: passed');
