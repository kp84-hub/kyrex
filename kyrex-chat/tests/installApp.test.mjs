import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import InstallApp from '../src/components/InstallApp.jsx';

window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
const host = document.createElement('div');
document.body.append(host);
let root = createRoot(host);
await act(async () => root.render(React.createElement(InstallApp)));
assert.match(host.textContent, /Install Kyrex Chat/);
await act(async () => host.querySelector('button').click());
assert.match(host.textContent, /browser menu/);
let calls = 0;
const available = new Event('beforeinstallprompt', { cancelable: true });
available.prompt = async () => { calls++; };
available.userChoice = Promise.resolve({ outcome: 'dismissed' });
await act(async () => window.dispatchEvent(available));
assert.equal(available.defaultPrevented, true);
assert.equal(host.querySelector('#install-help'), null);
await act(async () => host.querySelector('button').click());
assert.equal(calls, 1);
assert.match(host.textContent, /Install Kyrex Chat/); // dismissal is not installation
await act(async () => window.dispatchEvent(new Event('appinstalled')));
assert.equal(host.textContent, '');
await act(async () => root.unmount());

window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} });
root = createRoot(host);
await act(async () => root.render(React.createElement(InstallApp)));
assert.equal(host.textContent, '');
await act(async () => root.unmount());
console.log('Install UI: manual fallback, native prompt, dismissal, and standalone passed.');
