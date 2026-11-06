import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import VoiceModeButton from '../src/components/VoiceModeButton.jsx';

const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div), choices = [];
const render = disabled => act(async () => root.render(React.createElement(VoiceModeButton, { disabled, onSelect: wake => choices.push(wake) })));
const launcher = () => div.querySelector('.voice-mode-launcher');
const pointer = async (type, x = 100, y = 150, extra = {}) => act(async () => {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, { pointerId: 1, clientX: x, clientY: y, button: 0, isPrimary: true, ...extra });
  launcher().dispatchEvent(event);
});
const hold = () => act(async () => new Promise(resolve => setTimeout(resolve, 260)));
const key = key => act(async () => launcher().dispatchEvent(new window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })));

await render(false);
await pointer('pointerdown'); await hold();
assert.equal(choices.length, 0, 'Holding reveals choices without opening the microphone');
await pointer('pointermove', 40, 70);
assert.equal(div.querySelector('[data-selected]').dataset.mode, 'talk');
await pointer('pointerup', 40, 70);
await act(async () => launcher().click());
assert.deepEqual(choices, [false], 'Up-left releases once, including the browser click');
assert.equal(div.querySelector('[role=group]'), null);
await pointer('pointerdown'); await hold(); await pointer('pointerup', 160, 70);
assert.deepEqual(choices, [false, true], 'Up-right selects wake listening');

for (const end of [[100, 60], [170, 150], [100, 150]]) {
  await pointer('pointerdown'); await hold(); await pointer('pointerup', ...end);
}
await pointer('pointerdown'); await pointer('pointerup', 40, 70);
assert.equal(choices.length, 2, 'Ambiguous directions, returning to center and swipes without a hold do not start voice');
await pointer('pointerdown'); await hold(); await pointer('pointercancel'); await pointer('pointerup', 40, 70);
await pointer('pointerdown'); await hold(); await pointer('lostpointercapture'); await pointer('pointerup', 40, 70);
assert.equal(choices.length, 2, 'Cancelled gestures never select a mode');
await pointer('pointerdown'); await hold(); await pointer('pointerdown', 10, 10, { pointerId: 2, isPrimary: false });
await pointer('pointerup', 30, 60, { pointerId: 2 });
assert.equal(choices.length, 2, 'Another finger cannot complete the active gesture');
await key('Escape'); await pointer('pointerup', 40, 70);
assert.equal(choices.length, 2, 'Escape cancels the gesture');
await pointer('pointerdown'); await hold(); await render(true); await pointer('pointerup', 40, 70);
assert.equal(choices.length, 2, 'Disabling voice invalidates an in-flight hold');
await render(false);
await key('Enter');
await act(async () => launcher().click());
assert.ok(div.querySelector('[role=group]'), 'A simple tap exposes accessible choices');
assert.equal(choices.length, 2);
await key('ArrowRight');
assert.equal(document.activeElement.dataset.mode, 'wake');
await act(async () => document.activeElement.click());
assert.deepEqual(choices, [false, true, true]);
await pointer('pointerdown'); await hold();
await act(async () => window.dispatchEvent(new Event('blur')));
await pointer('pointerup', 40, 70);
assert.equal(choices.length, 3, 'Losing focus cancels the pending selection');
await pointer('pointerdown'); await act(async () => root.unmount()); await hold();
assert.equal(choices.length, 3, 'Unmount clears the pending hold timer');
console.log('Voice mode hold/swipe, cancellation and accessible choices: passed');
