import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Message from '../src/components/Message.jsx';
import EventReviewSettings from '../src/components/EventReviewSettings.jsx';

const node = document.body.appendChild(document.createElement('div'));
let root = createRoot(node), requests = [];
let job = { id:'event-one',revision:'a'.repeat(32),state:'pending',kind:'create',
  fields:{title:'Science fair',date:'2027-02-04',start:'09:00',end:'10:00',all_day:false,location:'Hall 4'},
  source:{label:'School · parent@example.test',received:'2026-10-27T14:00:00Z',coverage:'Synced text only.'},
  evidence:'Science fair February 4, 2027 from 9am to 10am at Hall 4.',
  destination:{calendar:'family@example.test',email:'parent@example.test'},timezone:'America/New_York',
  needs:[],detail:'',can_edit:true,task:null,approval:null };
const response = data => ({ok:true,json:async()=>data});
globalThis.fetch = async (url, init = {}) => {
  const data = init.body ? JSON.parse(init.body) : undefined;
  if (init.method === 'POST') {
    requests.push({url,data});
    assert.equal(data.revision,job.revision);
    if (url.endsWith('/edit')) job = {...job,fields:data.fields,revision:'b'.repeat(32)};
    if (url.endsWith('/review')) job = {...job,state:'reviewing',can_edit:false,
      task:{task_id:'task-one',status:'awaiting_approval'},approval:{approval_id:'approval-one',detail:'Exact Science fair payload · family@example.test'}};
    if (url.endsWith('/decision')) {
      assert.equal(data.approval_id,'approval-one');
      job = {...job,state:'added',task:{task_id:'task-one',status:'done'},approval:null,detail:'Added after your approval.'};
    }
    if (url.endsWith('/dismiss')) job = {...job,state:'dismissed'};
  }
  return response(job);
};
const render = () => act(async () => root.render(React.createElement(Message,{
  conversationId:'c1',message:{id:'suggestion',role:'assistant',content:'Calendar suggestion',calendar_suggestion:{id:'event-one'}}})));
const button = name => [...node.querySelectorAll('button')].find(b=>b.textContent===name);
await render();
assert.equal(requests.length,0,'opening a suggestion never queues or approves a write');
assert.match(node.textContent,/parent@example.test/);
assert.match(node.querySelector('blockquote').textContent,/9am to 10am/);
await act(async()=>button('Edit').click());
const input = node.querySelector('input');
await act(async()=>{
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,'Science fair and awards');
  input.dispatchEvent(new Event('input',{bubbles:true}));
});
assert.equal(button('Review calendar event'),undefined,'unsaved edits cannot open an approval');
await act(async()=>button('Save event details').click());
assert.equal(requests.length,1); assert.ok(requests[0].url.endsWith('/edit'));
await act(async()=>button('Review calendar event').click());
assert.equal(requests.length,2,'preparing the review still does not approve it');
assert.equal(requests[1].data.revision,'b'.repeat(32));
assert.match(node.querySelector('pre').textContent,/family@example.test/);
await act(async()=>button('Approve').click());
assert.deepEqual(requests.at(-1).data,{revision:'b'.repeat(32),approval_id:'approval-one',decision:'approve'});
assert.match(node.textContent,/Added to Calendar/);
assert.equal(button('Approve'),undefined);
await act(async()=>root.unmount()); root = createRoot(node);
await render();
assert.equal(requests.length,3,'reopening an added event never retries the write');
await act(async()=>root.unmount()); root = createRoot(node);
job = {...job,state:'pending',task:null,can_edit:true,needs:['end time']};
await render();
assert.equal(button('Review calendar event').disabled,true);
await act(async()=>button('Dismiss').click());
assert.match(node.textContent,/Dismissed/);
assert.equal(button('Review calendar event'),undefined);
await act(async()=>root.unmount()); root = createRoot(node);
job = {...job,state:'unknown',can_edit:false,needs:[],detail:'Check Calendar before trying again.'};
await render();
assert.equal(button('Edit'),undefined); assert.equal(button('Approve'),undefined);
assert.match(node.textContent,/Check Calendar/);
await act(async()=>root.unmount()); root = createRoot(node);
let saves = [];
globalThis.fetch = async (url,init={})=>{
  if (init.method==='PUT') saves.push(JSON.parse(init.body));
  return response({settings:{enabled:false,emails:true,messages:false,bot_id:'assistant',interval_seconds:3600},
    configured:true,bots:[{id:'assistant',name:'Assistant'}],conversation_id:'c1',timezone:'America/New_York'});
};
await act(async()=>root.render(React.createElement(EventReviewSettings)));
assert.equal(saves.length,0,'settings never enable scans on mount');
const checkbox = node.querySelector('input[type=checkbox]');
await act(async()=>checkbox.click());
await act(async()=>node.querySelector('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
assert.equal(saves.length,1); assert.equal(saves[0].enabled,true); assert.equal(saves[0].interval_seconds,3600);
assert.match(node.textContent,/browser-only Messages connections are excluded/);
await act(async()=>root.unmount());
console.log('Event review: explicit setup, missing details, edits, exact approval, dismiss and unknown non-replay passed.');
