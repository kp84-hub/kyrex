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

