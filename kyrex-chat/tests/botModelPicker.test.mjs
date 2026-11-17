import assert from 'node:assert/strict';
import React, { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import BotModelPicker from '../src/components/BotModelPicker.jsx';

const rootElement = document.createElement('div');
document.body.append(rootElement);
const root = createRoot(rootElement);
const go = { opencode_go: true, models: ['deepseek-v4.1-flash'] };
const groq = { models: ['qwen/qwen3.8-27b'] };
const calls = [];
let release;
globalThis.fetch = (url) => {
  calls.push(url);
  return new Promise(resolve => { release = resolve; });
};

function Field({ profile }) {
  const [value, setValue] = useState('');
  return React.createElement(BotModelPicker, {
    id: 'model', profile, value, onChange: setValue,
  });
}

const render = (profile) => act(async () => root.render(React.createElement(Field, { profile })));
const input = () => rootElement.querySelector('input');
const options = () => [...rootElement.querySelectorAll('[role=option]')].map(el => el.textContent);
const type = (value) => act(async () => {
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input(), value);
  input().dispatchEvent(new Event('input', { bubbles: true }));
});
const key = (value) => act(async () => input().dispatchEvent(
  new window.KeyboardEvent('keydown', { key: value, bubbles: true })));

await render(go);
assert.deepEqual(calls, [], 'Opening Bot settings does not fetch models');
await act(async () => input().focus());
assert.deepEqual(options(), ['deepseek-v4.1-flash']);
await act(async () => release({ ok: true, json: async () => ({
  models: ['deepseek-v4.1-flash', 'kimi-k3.1-pro', 'kimi-k3'],
}) }));
await type('Kimi 3.1 Pro');
assert.deepEqual(options(), ['kimi-k3.1-pro']);
await key('ArrowDown');
assert.equal(rootElement.querySelector('[aria-selected=true]').textContent, 'kimi-k3.1-pro');
await key('Enter');
assert.equal(input().value, 'kimi-k3.1-pro');
assert.equal(input().getAttribute('aria-expanded'), 'false');
await type('my-custom/model');
assert.deepEqual(options(), []);
assert.equal(input().value, 'my-custom/model');

await render(groq);
await type('qwen 27b');
assert.deepEqual(options(), ['qwen/qwen3.8-27b']);
await act(async () => rootElement.querySelector('[role=option]').click());
assert.equal(input().value, 'qwen/qwen3.8-27b');
assert.deepEqual(calls, ['/api/chat/provider-profiles/opencode-go/models']);
await act(async () => root.unmount());
