import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import BotModelCheck from '../src/components/BotModelCheck.jsx';

const calls = [];
let next = { ok: false, model: 'gemini-4-argon-contributor', workspace: { available: true, model_listed: true },
  message: 'OpenCode could not run the Gemini request (HTTP 400; inference_failed).' };
let pending;
globalThis.fetch = async (url, options) => {
  calls.push({ url, body: JSON.parse(options.body) });
  const value = pending ? await pending : next;
  return { ok: true, json: async () => value };
};
const div = document.createElement('div'); document.body.append(div);
const root = createRoot(div);
const profile = { id: 'go', base_url: 'https://opencode.ai/zen/go/v1' };
let model = 'gemini-4-argon-contributor';
const render = () => root.render(React.createElement(BotModelCheck, { botId: 'chief', profile, model }));
await act(async () => render());
assert.equal(calls.length, 0, 'Opening settings does not test or change a model');
await act(async () => div.querySelector('button').click());
assert.deepEqual(calls[0], { url: '/api/bots/chief/llm/check', body: { provider_profile_id: 'go', model } });
assert.match(div.querySelector('[role=alert]').textContent, /HTTP 400/);
assert.match(div.textContent, /lists this model/);
next = { ok: true, workspace: { available: false, http_status: 403 }, message: 'OpenCode accepted this model and returned a test reply.' };
await act(async () => div.querySelector('button').click());
assert.match(div.querySelector('[role=status]').textContent, /accepted/);
assert.match(div.textContent, /HTTP 403/);

let release;
pending = new Promise(resolve => { release = resolve; });
await act(async () => div.querySelector('button').click());
assert.equal(div.querySelector('button').disabled, true);
model = 'deepseek-v4.1-flash';
await act(async () => render());
assert.equal(div.querySelector('[role=status]'), null, 'A prior result is cleared on selection changes');
await act(async () => release(next));
assert.equal(div.querySelector('[role=status]'), null, 'An older slow check cannot verify a different selection');
assert.equal(calls.some(call => /configure|\/api\/chat$/.test(call.url)), false, 'Checks never save or run Chat');
await act(async () => root.render(React.createElement(BotModelCheck, {
  botId: 'chief', model, profile: { id: 'or', base_url: 'https://openrouter.ai/api/v1' },
})));
assert.equal(div.querySelector('button'), null, 'A non-OpenCode key is never used for an OpenCode check');
await act(async () => root.unmount());
console.log('Explicit Bot model checks, draft selection, and stale-result handling passed');
