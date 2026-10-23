import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Composer from '../src/components/Composer.jsx';
import KbotHeader from '../src/components/KbotHeader.jsx';
import VoiceControls from '../src/components/VoiceControls.jsx';
import { VoiceClient } from '../src/lib/voiceClient.js';
import { isKbotSurface, kbotPhase, readKbotAnchor } from '../src/lib/kbotSurface.js';

assert.equal(isKbotSurface({ search: '?surface=kbot' }), true);
assert.equal(isKbotSurface({ search: '?surface=other' }), false);
assert.equal(kbotPhase('speaking', false), 'speaking');
assert.equal(kbotPhase('recording', false), 'listening');
assert.equal(kbotPhase('stopped', true, { role: 'assistant', streaming: true, content: 'Hello' }), 'replying');
assert.equal(kbotPhase('stopped', true), 'thinking');
assert.equal(readKbotAnchor({ side: 'evil', y: 50 }), null);
assert.equal(readKbotAnchor({ side: 'left', y: -100 }).y, 28);

const host = document.body.appendChild(document.createElement('div'));
host.className = 'kbot-app';
const root = createRoot(host), sent = [];
const compact = { compact: true, onSend: text => sent.push(text), onStop: () => {}, isConversationReady: true };
await act(async () => root.render(React.createElement(Composer, compact)));
assert.equal(host.querySelector('textarea').getAttribute('aria-label'), 'Message Kbot');
assert.equal(host.querySelector('[aria-label="Attach workspace"]'), null);
assert.equal(host.querySelector('[aria-label="Change conversation model"]'), null);
const textarea = host.querySelector('textarea');
Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(textarea, 'Hello Kbot');
await act(async () => textarea.dispatchEvent(new Event('input', { bubbles: true })));
await act(async () => textarea.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
assert.deepEqual(sent, ['Hello Kbot']);
await act(async () => root.render(React.createElement(KbotHeader, { phase: 'speaking', botName: 'The Overwatcher', conversationId: 'fixture thread', onHistory: () => {} })));
assert.match(host.textContent, /Kbot.*Talking.*The Overwatcher/s);
assert.equal(host.querySelector('a').getAttribute('href'), '/?conversation=fixture%20thread');
window.__kyrexKbotAnchor = { side: 'left', y: 90 };
await act(async () => window.dispatchEvent(new Event('kyrex-kbot-anchor')));
assert.equal(host.dataset.tail, 'left');
assert.equal(host.style.getPropertyValue('--kbot-tail-y'), '90px');

const start = VoiceClient.prototype.start, stop = VoiceClient.prototype.stop;
let starts = 0;
const phases = [];
VoiceClient.prototype.start = async function(wake, takeover) { starts++; assert.equal(wake, false); assert.equal(takeover, false); this.state('recording'); };
VoiceClient.prototype.stop = async function() { this.state('stopped'); };
try {
  await act(async () => root.render(React.createElement(VoiceControls, { compact: true, onPhaseChange: phase => phases.push(phase) })));
  assert.equal(starts, 0, 'Opening Kbot never starts a microphone');
  await act(async () => host.querySelector('[aria-label="Talk to Kbot"]').click());
  assert.equal(starts, 1, 'The microphone button is explicit Talk input');
  assert.ok(phases.includes('recording'));
} finally {
  await act(async () => root.unmount());
  VoiceClient.prototype.start = start; VoiceClient.prototype.stop = stop;
  delete window.__kyrexKbotAnchor;
  host.remove();
}
console.log('Kbot presentation, existing composer send, conversation links, and explicit voice activation passed');
