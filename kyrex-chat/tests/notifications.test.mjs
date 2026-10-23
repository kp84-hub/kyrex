import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import NotificationCenter from '../src/components/NotificationCenter.jsx';
import { disablePush, enablePush } from '../src/lib/notifications.js';

const cid = 'a'.repeat(32), id = 'b'.repeat(32);
globalThis.localStorage = window.localStorage;
Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
window.PushManager = class {};
const publicKey = 'B' + 'A'.repeat(86);

function fixture({ denied = false, failEnrollment = false, unauthorized = false } = {}) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  const calls = [], opened = [];
  let requests = 0, subscriptions = 0, unsubscribed = 0, registered = 0, enabled = false;
  const items = [{ id, kind: 'awaiting_approval', title: 'Bot · Task review needed', conversation: cid, created: 1000, read: null }];
  const subscription = { toJSON: () => ({ endpoint: 'https://fcm.googleapis.com/fcm/send/fixture', keys: {} }),
    unsubscribe: async () => { unsubscribed++; return true; } };
  const worker = { pushManager: { getSubscription: async () => null,
    subscribe: async options => { assert.equal(options.userVisibleOnly, true); subscriptions++; return subscription; } } };
  window.Notification = { permission: 'default', requestPermission: async () => {
    requests++; window.Notification.permission = denied ? 'denied' : 'granted'; return window.Notification.permission;
  } };
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: {
    register: async () => { registered++; return worker; }, ready: Promise.resolve(worker),
    getRegistration: async () => ({ pushManager: { getSubscription: async () => subscription } }),
    addEventListener() {}, removeEventListener() {},
  } });
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    const json = (value, status = 200) => ({ ok: status < 400, status, json: async () => value });
    if (unauthorized) return json({ detail: 'Not authenticated' }, 401);
    if (url.endsWith('/settings')) return json({ available: true, public_key: publicKey });
    if (url.endsWith('/test')) return json({ queued: true });
    if (url.includes('/devices/')) {
      if (options.method === 'POST' && failEnrollment) return json({ detail: 'Device could not be saved' }, 503);
      if (options.method === 'POST') enabled = true;
      if (options.method === 'DELETE') enabled = false;
      return json({ enabled });
    }
    if (url.endsWith('/read')) {
      const ids = JSON.parse(options.body).ids;
      items.forEach(item => { if (ids.includes(item.id)) item.read = 2000; });
      return json({ updated: true });
    }
    if (url.endsWith('/' + id)) return json(items[0]);
    return json({ notifications: items, unread: items.filter(item => !item.read).length });
  };
  const div = document.body.appendChild(document.createElement('div')), root = createRoot(div);
  const button = text => [...div.querySelectorAll('button')].find(b => b.textContent === text);
  return { calls, items, opened, div, root, button,
    get requests() { return requests; }, get subscriptions() { return subscriptions; },
    get unsubscribed() { return unsubscribed; }, get registered() { return registered; },
    async mount(props = {}) { await act(async () => root.render(React.createElement(NotificationCenter, { onOpenConversation: value => opened.push(value), ...props }))); },
    async open() { await act(async () => div.querySelector('.notification-trigger').click()); },
    async close() { await act(async () => root.unmount()); div.remove(); },
  };
}

test('bell shows unread results without asking for permission; click resolves the current owner and opens its conversation', async () => {
  const f = fixture();
  try {
    await f.mount(); assert.equal(f.requests, 0); assert.equal(f.registered, 0);
    assert.match(f.div.querySelector('.notification-trigger').getAttribute('aria-label'), /1 unread/);
    await f.open(); assert.equal(f.requests, 0);
    assert.match(f.div.textContent, /Task review needed/);
    await act(async () => f.div.querySelector('.notification-item').click());
    assert.deepEqual(f.opened, [cid]);
    assert.equal(f.div.querySelector('[role=dialog]'), null);
    assert.match(f.calls.find(c => c.url.endsWith('/' + id)).url, /api\/notifications/);
    assert.ok(f.calls.every(c => !/approve|respond|api\/chat$|api\/task/.test(c.url)));
    assert.equal(f.div.querySelector('.notification-count'), null);
  } finally { await f.close(); }
});

test('Kbot opens the existing inbox without a header bell, permission prompts or automatic reads', async () => {
  const f = fixture();
  const app = document.body.appendChild(document.createElement('div')); app.className = 'kbot-app';
  const composer = app.appendChild(document.createElement('textarea')); composer.className = 'composer-input';
  try {
    await f.mount({ compact: true });
    assert.equal(f.div.querySelector('.notification-trigger'), null);
    assert.equal(f.div.querySelector('[role=dialog]'), null);
    window.__kyrexKbotNotificationsPending = Date.now();
    await act(async () => window.dispatchEvent(new Event('kyrex-kbot-notifications')));
    assert.equal(window.__kyrexKbotNotificationsPending, false);
    assert.ok(f.div.querySelector('[role=dialog]'));
    assert.match(f.div.textContent, /Task review needed/);
    assert.equal(f.div.querySelector('.notification-device'), null);
    assert.equal(f.requests, 0); assert.equal(f.registered, 0);
    assert.ok(f.calls.every(c => !c.options.method || c.options.method === 'GET'));
    const buttons = [...f.div.querySelectorAll('button:not(:disabled)')];
    buttons.at(-1).focus();
    await act(async () => document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })));
    assert.equal(document.activeElement, buttons[0]);
    await act(async () => document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true })));
    assert.equal(document.activeElement, buttons.at(-1));
    const escape = new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    await act(async () => document.dispatchEvent(escape));
    assert.equal(f.div.querySelector('[role=dialog]'), null);
    assert.equal(document.activeElement, composer);
    assert.equal(escape.defaultPrevented, true);
    window.__kyrexKbotNotificationsPending = Date.now();
    await act(async () => window.dispatchEvent(new Event('kyrex-kbot-notifications')));
    await act(async () => f.div.querySelector('.notification-item').click());
    assert.deepEqual(f.opened, [cid]);
    assert.ok(f.calls.every(c => !/approve|respond|api\/chat$|api\/task/.test(c.url)));
    assert.deepEqual(JSON.parse(f.calls.find(c => c.url.endsWith('/read')).options.body).ids, [id]);
  } finally { await f.close(); app.remove(); delete window.__kyrexKbotNotificationsPending; }
});

test('Kbot consumes a fresh startup/visibility request but ignores stale inbox requests', async () => {
  const f = fixture();
  try {
    window.__kyrexKbotNotificationsPending = Date.now() - 20000;
    await f.mount({ compact: true });
    assert.equal(f.div.querySelector('[role=dialog]'), null);
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    window.__kyrexKbotNotificationsPending = Date.now();
    await act(async () => window.dispatchEvent(new Event('kyrex-kbot-notifications')));
    assert.equal(f.div.querySelector('[role=dialog]'), null);
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    await act(async () => document.dispatchEvent(new Event('visibilitychange')));
    assert.ok(f.div.querySelector('[role=dialog]'));
    assert.equal(window.__kyrexKbotNotificationsPending, false);
  } finally { await f.close(); delete window.__kyrexKbotNotificationsPending; }
  const startup = fixture();
  try {
    window.__kyrexKbotNotificationsPending = Date.now();
    await startup.mount({ compact: true });
    assert.ok(startup.div.querySelector('[role=dialog]'));
  } finally { await startup.close(); delete window.__kyrexKbotNotificationsPending; }
});

test('enabling is an explicit gesture, saves the real subscription, and test delivery is labelled queued', async () => {
  const f = fixture();
  try {
    await f.mount(); await f.open();
    await act(async () => f.button('Enable device notifications').click());
    assert.equal(f.requests, 1); assert.equal(f.subscriptions, 1); assert.equal(f.registered, 1);
    const enrollment = f.calls.find(c => c.url.includes('/devices/') && c.options.method === 'POST');
    assert.equal(JSON.parse(enrollment.options.body).subscription.endpoint, 'https://fcm.googleapis.com/fcm/send/fixture');
    await act(async () => f.button('Send test notification').click());
    assert.match(f.div.textContent, /Test queued/);
    await act(async () => f.button('Disable on this device').click());
    assert.ok(f.calls.some(c => c.options.method === 'DELETE')); assert.equal(f.unsubscribed, 1);
    assert.ok(f.button('Enable device notifications'));
  } finally { await f.close(); }
});

test('denied permission leaves inbox working and never subscribes or registers the worker', async () => {
  const f = fixture({ denied: true });
  try {
    await f.mount(); await f.open();
    await act(async () => f.button('Enable device notifications').click());
    assert.match(f.div.querySelector('[role=alert]').textContent, /not allowed/);
    assert.equal(f.registered, 0); assert.equal(f.subscriptions, 0);
    assert.ok(f.div.querySelector('.notification-item'));
  } finally { await f.close(); }
});

test('a failed enrollment rolls back a newly-created browser subscription', async () => {
  const f = fixture({ failEnrollment: true });
  await assert.rejects(enablePush({ available: true, public_key: publicKey }), /could not be saved/);
  assert.equal(f.unsubscribed, 1); assert.equal(f.subscriptions, 1);
  await f.close();
});

test('disable revokes server delivery before unsubscribing locally', async () => {
  const f = fixture();
  await disablePush(); assert.equal(f.calls[0].options.method, 'DELETE'); assert.equal(f.unsubscribed, 1);
  await f.close();
});

test('mark shown as read sends only displayed identities and Escape restores focus', async () => {
  const f = fixture();
  try {
    await f.mount(); await f.open();
    await act(async () => f.button('Mark shown as read').click());
    assert.deepEqual(JSON.parse(f.calls.find(c => c.url.endsWith('/read')).options.body).ids, [id]);
    await act(async () => document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    assert.equal(f.div.querySelector('[role=dialog]'), null);
    assert.equal(document.activeElement, f.div.querySelector('.notification-trigger'));
  } finally { await f.close(); }
});

test('a push deep link resolves through the authenticated inbox and cannot trigger an action', async () => {
  const f = fixture(); window.history.replaceState({}, '', '/?notification=' + id);
  try {
    await f.mount(); assert.deepEqual(f.opened, [cid]);
    assert.equal(window.location.search, ''); assert.equal(f.requests, 0);
    assert.ok(f.calls.every(c => c.url.startsWith('/api/notifications')));
  } finally { await f.close(); }
});

test('signed-out inbox never displays a previous owner’s rows or badge', async () => {
  const f = fixture({ unauthorized: true });
  try {
    await f.mount(); await f.open();
    assert.equal(f.div.querySelector('.notification-count'), null);
    assert.equal(f.div.querySelector('.notification-item'), null);
    assert.match(f.div.textContent, /Not authenticated/);
  } finally { await f.close(); }
});

test('service worker shows generic content, coalesces duplicate identities, and only opens same-origin inbox links', async () => {
  const handlers = {}, shown = [], opened = [], messages = [];
  const self = { location: { origin: 'https://chat.example.test' },
    addEventListener: (name, handler) => { handlers[name] = handler; },
    registration: { showNotification: async (title, options) => shown.push({ title, options }) },
    clients: { matchAll: async () => [{ url: 'https://foreign.example/', postMessage: m => messages.push(m) }],
      openWindow: async url => opened.push(url) } };
  vm.runInNewContext(fs.readFileSync(new URL('../public/sw.js', import.meta.url), 'utf8'), { self, URL });
  const push = async payload => { let work; handlers.push({ data: { json: () => payload }, waitUntil: promise => { work = promise; } }); await work; };
  await push({ id, title: 'Private email subject', body: 'Secret content', url: 'https://evil.example/approve' });
  await push({ id });
  assert.equal(shown.length, 2); assert.equal(shown[0].options.tag, shown[1].options.tag);
  assert.ok(!JSON.stringify(shown).includes('Secret')); assert.equal(shown[0].title, 'Kyrex Chat');
  let work;
  handlers.notificationclick({ notification: { close() {}, data: { id, url: 'https://evil.example' } }, waitUntil: p => { work = p; } }); await work;
  assert.deepEqual(opened, ['https://chat.example.test/?notification=' + id]);
  await push({ id: '../../private' }); assert.equal(shown.length, 2);
});
