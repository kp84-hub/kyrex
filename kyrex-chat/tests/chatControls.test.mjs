import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Composer from '../src/components/Composer.jsx';
import ChatHeader from '../src/components/ChatHeader.jsx';
import MessageList from '../src/components/MessageList.jsx';

const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div), sent = [], changed = [], selected = [];
let saveModel, stopped = 0;
const button = label => div.querySelector(`button[aria-label="${label}"]`);
const input = async (selector, value) => act(async () => {
  const field = div.querySelector(selector);
  Object.getOwnPropertyDescriptor(field.constructor.prototype, 'value').set.call(field, value);
  field.dispatchEvent(new Event('input', { bubbles: true }));
});
const props = { onSend: text => sent.push(text), onStop: () => stopped++, providers: [{id:'p1',models:['first-model']}],
  activeProvider: 'p1', activeModel: 'first-model',
  onChangeProvider: (provider, model) => { changed.push([provider, model]); return new Promise(resolve => { saveModel = resolve; }); } };

await act(async () => root.render(React.createElement(Composer, props)));
assert.equal(div.querySelector('input[aria-label="Model ID"]'), null);
await input('textarea', 'Keep this draft');
await act(async () => button('Change conversation model').click());
assert.equal(div.querySelector('textarea').value, 'Keep this draft');
await input('input[aria-label="Model ID"]', 'custom/model-id');
await act(async () => button('Use model ID').click());
assert.deepEqual(changed, [['p1', 'custom/model-id']]);
assert.equal(button('Send message').disabled, true, 'Do not send while changing the conversation model');
await act(async () => { saveModel(); });
await act(async () => document.dispatchEvent(new window.KeyboardEvent('keydown', {key:'Escape',bubbles:true})));
assert.equal(div.querySelector('[role=dialog]'), null);
assert.equal(document.activeElement, button('Change conversation model'));
await act(async () => button('Send message').click());
assert.deepEqual(sent, ['Keep this draft']);
assert.equal(div.querySelector('textarea').value, '');
await act(async () => root.render(React.createElement(Composer, {...props, activeBotId:'bot', activeWorkspaceId:'kyrex'})));
assert.equal(button('Change conversation model'), null, 'Bot model stays in Bot settings');
assert.equal(button('Attach workspace'), null, 'Bot conversations keep their existing workspace boundary');
assert.equal(div.querySelector('.workspace-active-chip'), null, 'A stale workspace cannot appear attached to a Bot');
await act(async () => root.render(React.createElement(Composer, {...props, isGenerating:true})));
await act(async () => button('Stop generating').click());
assert.equal(stopped, 1);

await act(async () => root.render(React.createElement(ChatHeader, {
  bots:[{id:'one',name:'First Bot',status:'running'},{id:'paused',name:'Paused Bot',status:'paused'}],
  activeBotId:null, onSelectBot:id=>selected.push(id),
})));
const picker = div.querySelector('[aria-label="Select Bot"]');
assert.equal(picker.options[0].textContent, 'Kyrex');
assert.equal(picker.querySelector('[value=paused]').disabled, true);
await act(async () => { picker.value='one'; picker.dispatchEvent(new Event('change',{bubbles:true})); });
assert.deepEqual(selected, ['one']);

await act(async () => root.render(React.createElement(MessageList, {messages:[],delegations:[],isGenerating:false})));
assert.match(div.textContent, /How can I help\?/);
await act(async () => root.render(React.createElement(MessageList, {messages:[],delegations:[],isGenerating:true})));
assert.equal(div.querySelector('.empty-state'), null, 'Do not show the welcome prompt during a reply');
await act(async () => root.unmount());
console.log('Compact chat controls preserve drafts, model saving, Bot boundaries, Stop and empty-state behavior: passed');
