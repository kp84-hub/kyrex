import assert from 'node:assert/strict';
import { test } from 'node:test';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import BrowserConnection from '../src/components/BrowserConnection.jsx';

const saved = { reference: 'bd-aaaaaaaaaaaaaaaa', status: 'failed', code: 'dispatch_timeout',
  message: 'The browser dispatch expired before a result was recorded.', bot_id: 'browser-fixture', host_id: 'host-fixture',
  cloud_build: 'abcdef123456', host_build: 'unknown', stages: [{id: 'connection', state: 'passed'}, {id: 'dispatch', state: 'failed'}, {id: 'sign_in', state: 'not_checked'}] };
const state = { bots: [{id: 'browser-fixture', name: 'My Browser', connected: true}], reports: [saved] };
const response = (body, status=200) => ({ok: status < 400, status, statusText: 'Error', json: async () => body});
const click = async node => act(async () => { node.dispatchEvent(new window.MouseEvent('click', {bubbles:true})); });
async function enter(node, value) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(node, value);
    node.dispatchEvent(new window.Event('input', {bubbles:true}));
  });
}
async function mount() {
  const el = document.createElement('div'); document.body.appendChild(el);
  const root = createRoot(el);
  await act(async () => root.render(React.createElement(BrowserConnection)));
  return {el, close: async () => {await act(async () => root.unmount()); el.remove();}};
}

test('Connections reads saved reports, posts only on tap, and reopens without replay', async () => {
  const calls = [];
  globalThis.fetch = async (url, opts={}) => {
    calls.push({url, opts});
    return response(opts.method === 'POST' ? saved : state);
  };
  let ui = await mount();
  assert.equal(calls.filter(c => c.opts.method === 'POST').length, 0);
  assert.match(ui.el.textContent, /bd-aaaaaaaaaaaaaaaa/);
  assert.match(ui.el.textContent, /Member sign-in: Not checked/);
  assert.match(ui.el.textContent, /Cloud version: abcdef123456/);
  await enter(ui.el.querySelector('input'), 'https://example.com');
  const button = [...ui.el.querySelectorAll('button')].find(b => b.textContent === 'Test browser connection');
  assert.equal(button.disabled, false);
  await click(button);
  const posts = calls.filter(c => c.opts.method === 'POST');
  assert.equal(posts.length, 1);
  const payload = JSON.parse(posts[0].opts.body);
  assert.equal(payload.bot_id, 'browser-fixture'); assert.equal(payload.url, 'https://example.com');
  assert.match(payload.request_id, /^[a-f0-9-]{36}$/);
  await ui.close();
  ui = await mount();
  assert.match(ui.el.textContent, /bd-aaaaaaaaaaaaaaaa/);
  assert.equal(calls.filter(c => c.opts.method === 'POST').length, 1);
  await ui.close();
});

test('a lost acknowledgement retries the same identity and exposes an inline error', async () => {
  const ids = [];
  globalThis.fetch = async (url, opts={}) => {
    if (opts.method === 'POST') {
      ids.push(JSON.parse(opts.body).request_id);
      if (ids.length === 1) throw new Error('network disconnected');
      return response(saved);
    }
    return response(state);
  };
  const ui = await mount();
  await enter(ui.el.querySelector('input'), 'https://example.com');
  const button = [...ui.el.querySelectorAll('button')].find(b => b.textContent === 'Test browser connection');
  await click(button);
  assert.match(ui.el.querySelector('[role="alert"]').textContent, /Could not confirm/);
  await click(button);
  assert.equal(ids.length, 2); assert.equal(ids[0], ids[1]);
  await ui.close();
});

test('a failed initial read does not claim the Bot or reconnect service is unconfigured; refresh restores controls', async () => {
  let unavailable = true;
  const calls = [];
  globalThis.fetch = async (url, opts={}) => {
    calls.push({url, opts});
    if (url.includes('/reconnect/')) return response({state: 'idle'});
    return unavailable ? response({}, 503) : response({...state, bots: [{...state.bots[0], reconnect_available: true}]});
  };
  const ui = await mount();
  assert.match(ui.el.querySelector('[role="alert"]').textContent, /could not be loaded/);
  assert.doesNotMatch(ui.el.textContent, /Configure a Browser Bot|reconnect service to be configured/);
  assert.match(ui.el.textContent, /configuration status is unavailable/);
  unavailable = false;
  await click([...ui.el.querySelectorAll('button')].find(b => b.textContent === 'Refresh browser status'));
  assert.ok(ui.el.querySelector('select[aria-label="Browser Bot to test"]'));
  assert.ok([...ui.el.querySelectorAll('button')].find(b => b.textContent === 'Reconnect sign-in'));
  assert.equal(ui.el.querySelector('[role="alert"]'), null);
  assert.ok(calls.every(call => !call.opts.method || call.opts.method === 'GET'), 'refresh never starts a viewer or booking');
  await ui.close();
});

test('a failed later refresh keeps the last verified Browser Bot and private sign-in controls', async () => {
  let unavailable = false;
  globalThis.fetch = async url => {
    if (url.includes('/reconnect/')) return response({state: 'idle'});
    return unavailable ? response({}, 503) : response({...state, bots: [{...state.bots[0], reconnect_available: true}]});
  };
  const ui = await mount();
  unavailable = true;
  await click([...ui.el.querySelectorAll('button')].find(b => b.textContent === 'Refresh browser status'));
  assert.ok(ui.el.querySelector('select'));
  assert.ok([...ui.el.querySelectorAll('button')].find(b => b.textContent === 'Reconnect sign-in'));
  assert.match(ui.el.querySelector('[role="alert"]').textContent, /could not be loaded/);
  assert.doesNotMatch(ui.el.textContent, /Configure a Browser Bot/);
  await ui.close();
});

test('only a successful empty configuration offers Bot setup', async () => {
  globalThis.fetch = async () => response({bots: [], reports: []});
  const ui = await mount();
  assert.match(ui.el.textContent, /Configure a Browser Bot/);
  assert.equal(ui.el.querySelector('[role="alert"]'), null);
  assert.doesNotMatch(ui.el.textContent, /reconnect service to be configured/);
  await ui.close();
});
