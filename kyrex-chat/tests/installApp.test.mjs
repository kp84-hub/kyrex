import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import InstallApp from '../src/components/InstallApp.jsx';
import ProviderSettings from '../src/components/ProviderSettings.jsx';
import Sidebar from '../src/components/Sidebar.jsx';
import { useAppInstall } from '../src/hooks/useAppInstall.js';

function InstallHarness({ open = true }) {
  const state = useAppInstall();
  return open ? React.createElement(InstallApp, { state }) : null;
}

window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
const host = document.createElement('div');
document.body.append(host);
let root = createRoot(host);
await act(async () => root.render(React.createElement(InstallHarness, { open: false })));
assert.equal(host.textContent, '');
let earlyCalls = 0;
const earlyPrompt = new Event('beforeinstallprompt', { cancelable: true });
earlyPrompt.prompt = async () => { earlyCalls++; };
earlyPrompt.userChoice = Promise.resolve({ outcome: 'dismissed' });
await act(async () => window.dispatchEvent(earlyPrompt));
await act(async () => root.render(React.createElement(InstallHarness)));
assert.match(host.textContent, /Install Kyrex Chat/);
await act(async () => host.querySelector('button').click());
assert.equal(earlyCalls, 1);
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
await act(async () => root.render(React.createElement(InstallHarness)));
assert.equal(host.textContent, '');
await act(async () => root.unmount());
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
const originalFetch = globalThis.fetch;
globalThis.fetch = async () => ({ ok: true, json: async () => ({ profiles: [] }) });
function SettingsHarness({ open }) {
  const state = useAppInstall();
  return React.createElement(React.Fragment, null,
    React.createElement(Sidebar, { conversations: [], open: true }),
    open ? React.createElement(ProviderSettings, { installState: state }) : null);
}
root = createRoot(host);
await act(async () => root.render(React.createElement(SettingsHarness, { open: false })));
assert.doesNotMatch(host.querySelector('aside').textContent, /Install Kyrex Chat/);
let settingsCalls = 0;
const settingsPrompt = new Event('beforeinstallprompt', { cancelable: true });
settingsPrompt.prompt = async () => { settingsCalls++; };
settingsPrompt.userChoice = Promise.resolve({ outcome: 'dismissed' });
await act(async () => window.dispatchEvent(settingsPrompt));
await act(async () => root.render(React.createElement(SettingsHarness, { open: true })));
assert.equal(host.querySelectorAll('.install-app-button').length, 1);
assert.ok(host.querySelector('.provider-settings .install-app-button'));
await act(async () => root.render(React.createElement(SettingsHarness, { open: false })));
await act(async () => root.render(React.createElement(SettingsHarness, { open: true })));
await act(async () => host.querySelector('.install-app-button').click());
assert.equal(settingsCalls, 1);
await act(async () => root.unmount());
globalThis.fetch = originalFetch;
console.log('Install UI: Settings-only placement, prompt retention, manual fallback, native prompt, dismissal, and standalone passed.');
