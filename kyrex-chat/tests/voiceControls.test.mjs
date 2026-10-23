import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import VoiceControls from '../src/components/VoiceControls.jsx';
import { VoiceClient } from '../src/lib/voiceClient.js';

const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div), calls = [];
let microphoneRequests = 0;
globalThis.fetch = async (url, init) => {
  calls.push([url, init]);
  return { ok: true, json: async () => ({ conversation_id: 'voice-alice',
    active_device: { name: 'Android phone' }, settings: { configured: true } }) };
};
Object.defineProperty(navigator, 'mediaDevices', { configurable: true,
  value: { getUserMedia: async () => { microphoneRequests++; throw new DOMException('Permission denied', 'NotAllowedError'); } } });
const button = text => [...div.querySelectorAll('button')].find(item => item.textContent === text);
await act(async () => root.render(React.createElement(VoiceControls)));
assert.equal(calls.length, 0, 'Opening Chat never changes a listening session');
assert.equal(div.querySelector('[role=dialog]'), null, 'Voice options are collapsed on arrival');
await act(async () => div.querySelector('[aria-label="Voice controls"]').click());
assert.equal(calls.length, 0, 'Opening voice options never starts the microphone or changes another device');
await act(async () => button('Listen for Kyrex').click());
assert.match(div.querySelector('[role=status]').textContent, /Android phone/);
assert.match(div.textContent, /companion keeps listening when Chat closes/);
assert.equal(microphoneRequests, 0); assert.equal(button('Stop voice'), undefined);
assert.ok(button('Use this browser'));
await act(async () => button('Use this browser').click());
assert.equal(microphoneRequests, 1, 'Only an explicit switch attempts browser microphone access');
assert.match(div.querySelector('[role=alert]').textContent, /Allow microphone access/);
assert.ok(calls.every(([url]) => url.endsWith('/session')), 'Failed browser startup never claims or stops companion listening');
await act(async () => root.unmount());

// Permission prompts can remove focus while the document remains visible.
// Only an actual hidden page stops its browser microphone session.
const originalStart = VoiceClient.prototype.start, originalStop = VoiceClient.prototype.stop;
let stops = 0;
VoiceClient.prototype.start = async function() { this.state('listening'); };
VoiceClient.prototype.stop = async function() { stops++; this.state('stopped'); };
Object.defineProperty(document, 'hidden', { configurable: true, value: false });
const focusRoot = createRoot(div);
try {
  await act(async () => focusRoot.render(React.createElement(VoiceControls)));
  const initialStops = stops;
  await act(async () => div.querySelector('[aria-label="Voice controls"]').click());
  await act(async () => button('Listen for Kyrex').click());
  await act(async () => document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
  assert.equal(div.querySelector('[role=dialog]'), null);
  assert.equal(document.activeElement, div.querySelector('[aria-label="Voice controls"]'));
  assert.equal(stops, initialStops, 'Closing options does not stop an active voice session');
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  assert.equal(stops, initialStops, 'A visible page does not stop merely because focus is elsewhere');
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  assert.equal(stops, initialStops + 1); assert.match(div.textContent, /Voice off/);
  await act(async () => focusRoot.unmount());
} finally {
  VoiceClient.prototype.start = originalStart; VoiceClient.prototype.stop = originalStop;
  delete document.hidden;
}
console.log('Chat companion status, explicit browser switch and visible-page permission handling: passed');

// Native Talk is explicit input to the existing voice UI, never a second
// session or a silent takeover. A second shortcut cannot restart active work.
let desktopStarts = 0;
VoiceClient.prototype.start = async function(wake, takeover) {
  desktopStarts++; assert.equal(wake, false); assert.equal(takeover, undefined);
  this.state('starting');
};
VoiceClient.prototype.stop = async function() { this.state('stopped'); };
const desktopRoot = createRoot(div);
const desktopTalk = () => { window.__kyrexDesktopTalkPending = Date.now(); window.dispatchEvent(new Event('kyrex-desktop-talk')); };
Object.defineProperty(document, 'hidden', { configurable: true, value: false });
try {
  await act(async () => desktopRoot.render(React.createElement(VoiceControls, { disabled: true })));
  await act(async () => desktopTalk());
  assert.equal(desktopStarts, 0, 'Signed-out Chat cannot activate voice');
  await act(async () => desktopRoot.render(React.createElement(VoiceControls, { disabled: false })));
  await act(async () => desktopTalk());
  assert.equal(desktopStarts, 1); assert.ok(div.querySelector('[role=dialog]'));
  await act(async () => desktopTalk());
  assert.equal(desktopStarts, 1, 'Repeated Talk leaves an active session intact');
  await act(async () => desktopRoot.unmount());
  await act(async () => desktopTalk());
  assert.equal(desktopStarts, 1, 'Unmount removes the native input listener');
} finally {
  VoiceClient.prototype.start = originalStart; VoiceClient.prototype.stop = originalStop;
  delete window.__kyrexDesktopTalkPending;
  delete document.hidden;
}

const originalInterrupt = VoiceClient.prototype.interrupt;
let interrupts = 0;
VoiceClient.prototype.start = async function() { this.wake = true; this.state('speaking'); };
VoiceClient.prototype.interrupt = async function() { interrupts++; this.state('interrupting'); };
VoiceClient.prototype.stop = async function() { this.state('stopped'); };
const interruptRoot = createRoot(div);
try {
  await act(async () => interruptRoot.render(React.createElement(VoiceControls)));
  await act(async () => div.querySelector('[aria-label="Voice controls"]').click());
  await act(async () => button('Listen for Kyrex').click());
  assert.match(div.textContent, /Say “Kyrex” to interrupt/);
  assert.ok(button('Stop voice'));
  await act(async () => button('Interrupt reply').click());
  assert.equal(interrupts, 1); assert.match(div.textContent, /Interrupting the reply/);
  assert.equal(button('Interrupt reply'), undefined);
  assert.ok(button('Stop voice'), 'Stop remains available while interruption is pending');
  await act(async () => button('Stop voice').click());
  assert.match(div.textContent, /Voice off/);
  await act(async () => interruptRoot.unmount());
} finally {
  VoiceClient.prototype.start = originalStart; VoiceClient.prototype.stop = originalStop;
  VoiceClient.prototype.interrupt = originalInterrupt;
}

