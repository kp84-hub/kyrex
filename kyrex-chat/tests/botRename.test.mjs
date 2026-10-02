import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import BotSettings from '../src/components/BotSettings.jsx';
let bot = { id: 'chief', name: 'Chief of Staff', manageable: true, status: 'running',
  role: {id:'chief-of-staff',label:'The Overwatcher'}, coordinator: true };
const calls = [];
globalThis.fetch = async (url, opts = {}) => {
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url, body, method: opts.method });
  let data = {};
  if (url === '/api/bots/chief' && opts.method === 'PATCH') { bot = {...bot, name:body.name}; data=bot; }
  return {ok:true, async json(){return data;}};
};
const container = document.createElement('div'); document.body.append(container);
const root = createRoot(container);
const render = () => root.render(React.createElement(BotSettings,{bots:[bot],onChanged:render}));
await act(async () => render());
await act(async () => container.querySelector('.bot-more-btn').click());
const button = text => [...container.querySelectorAll('button')].find(b => b.textContent.trim() === text);
await act(async () => button('Rename').click());
const input = container.querySelector('[id="rename-chief"]');
assert.equal(input.value,'Chief of Staff');
await act(async () => {
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,'The Overwatcher');
  input.dispatchEvent(new window.Event('input',{bubbles:true}));
});
await act(async () => button('Save name').click());
assert.ok(container.textContent.includes('Bot renamed to The Overwatcher.'));
assert.deepEqual(calls.find(c => c.method==='PATCH'), {url:'/api/bots/chief',method:'PATCH',body:{name:'The Overwatcher'}});
assert.equal(container.querySelector('form[aria-label="Rename Bot"]'),null);
await act(async () => root.unmount());
console.log('Name-only Bot rename and roster refresh passed');
