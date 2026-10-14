import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import EmailInboxConnection from '../src/components/EmailInboxConnection.jsx';

const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div);
const calls = [];
let view = { source: 'gmail', personal_gmail_enabled: true, connected: false, active: false };
let fail = false, changes = 0;
globalThis.fetch = async (url, init) => {
  const body = init.body ? JSON.parse(init.body) : null;
  calls.push({ url, method: init.method, body });
  if (fail) return { ok: false, json: async () => ({ detail: 'AgentMail unavailable. Personal Gmail was not used.' }) };
  if (url.endsWith('/source')) view = { ...view, source: body.source, personal_gmail_enabled: body.source === 'gmail', active: false };
  if (url.endsWith('/connect')) {
    assert.deepEqual(Object.keys(body).sort(), ['api_key', 'inbox_id', 'provider']);
    view = { provider: 'agentmail', source: 'agentmail', personal_gmail_enabled: false, connected: true, active: true,
      inbox_id: body.inbox_id, address: 'school@agentmail.to' };
  }
  if (url.endsWith('/disconnect')) view = { ...view, source: 'none', active: false, connected: false };
  return { ok: true, json: async () => view };
};
const button = text => [...div.querySelectorAll('button')].find(item => item.textContent === text);
const edit = async (input, value) => {
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};
await act(async () => root.render(React.createElement(EmailInboxConnection, { onChange: () => changes++ })));
assert.equal(calls.length, 1, 'Opening settings only reads status');
await act(async () => button('Stop personal Gmail reads').click());
assert.equal(view.source, 'none');
assert.ok(!calls.some(c => c.url.includes('/google/disconnect')), 'Calendar OAuth grant preserved');
await act(async () => {
  const select = div.querySelector('select');
  Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set.call(select, 'agentmail');
  select.dispatchEvent(new Event('change', { bubbles: true }));
});
await edit(div.querySelector('input'), 'inb_school');
await edit(div.querySelector('input[type=password]'), 'test-inbox-key-' + 'x'.repeat(30));
await act(async () => button('Connect dedicated inbox').click());
assert.equal(view.source, 'agentmail');
assert.equal(div.querySelector('input[type=password]').value, '', 'Saved secret never repopulated');
assert.match(div.textContent, /Keep forwarding for all incoming mail turned off/);
assert.match(div.textContent, /Personal Gmail reads are blocked/);
let copied;
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async text => { copied = text; } } });
await act(async () => button('Copy inbox address').click());
assert.equal(copied, 'school@agentmail.to');
fail = true;
await act(async () => button('Connect dedicated inbox').click());
assert.equal(view.source, 'agentmail');
assert.match(div.querySelector('[role=alert]').textContent, /Personal Gmail was not used/);
assert.ok(!calls.some(c => c.url.includes('upgrade-gmail')), 'No consent or Gmail fallback');
fail = false;
await act(async () => button('Disconnect dedicated inbox').click());
assert.equal(view.source, 'none');
assert.equal(button('Copy inbox address'), undefined);
await act(async () => button('Use personal Gmail').click());
assert.equal(view.source, 'gmail', 'Only explicit personal-Gmail choice reenables source');
assert.equal(changes, 4);
await act(async () => root.unmount());
console.log('Dedicated inbox setup, explicit source changes, failed reads and secret clearing: passed');

const zohoDiv = document.body.appendChild(document.createElement('div'));
const zohoRoot = createRoot(zohoDiv);
const zohoCalls = [];
let zohoView = { source:'none', connected:false, active:false, inbox_id:'', provider:'agentmail', personal_gmail_enabled:false };
globalThis.fetch = async (url, init) => {
  const data = init.body ? JSON.parse(init.body) : null;
  zohoCalls.push({url, data});
  if (url.endsWith('/connect')) {
    assert.deepEqual(Object.keys(data).sort(), ['email','host','password','provider']);
    assert.equal(data.provider, 'zoho');
    zohoView = {source:'zoho',provider:'zoho',inbox_id:data.email,address:data.email,host:data.host,connected:true,active:true,personal_gmail_enabled:false};
  }
  if (url.endsWith('/disconnect')) zohoView = {...zohoView,source:'none',connected:false,active:false};
  return {ok:true,json:async()=>zohoView};
};
const zohoButton = text => [...zohoDiv.querySelectorAll('button')].find(b=>b.textContent===text);
const select = async (element, value) => act(async()=>{
  Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set.call(element,value);
  element.dispatchEvent(new Event('change',{bubbles:true}));
});
await act(async()=>zohoRoot.render(React.createElement(EmailInboxConnection)));
assert.equal(zohoDiv.querySelector('select').value,'zoho','Zoho is the default for a new setup');
assert.equal(zohoCalls.length,1,'Opening setup only reads status');
assert.match(zohoDiv.textContent,/alias shares its account’s inbox/);
await edit(zohoDiv.querySelector('input[type=email]'),'keith@kyrex.example');
await edit(zohoDiv.querySelector('input[type=password]'),'zoho-test-app-password');
await act(async()=>zohoButton('Connect dedicated inbox').click());
assert.equal(zohoCalls.at(-1).data.email,'keith@kyrex.example');
assert.equal(zohoCalls.at(-1).data.host,'imappro.zoho.com');
assert.equal(zohoDiv.querySelector('input[type=password]').value,'');
assert.match(zohoDiv.textContent,/Personal Gmail reads are blocked/);
await act(async()=>zohoButton('Connect dedicated inbox').click());
assert.equal(zohoCalls.at(-1).data.password,'','Keeping a password never reads it back');
await select(zohoDiv.querySelectorAll('select')[1],'imap.zoho.com');
assert.equal(zohoButton('Connect dedicated inbox').disabled,true,'Changing servers requires entering its credential');
await edit(zohoDiv.querySelector('input[type=password]'),'pending-zoho-password');
await select(zohoDiv.querySelector('select'),'agentmail');
assert.equal(zohoDiv.querySelector('input[type=password]').value,'','Provider switch clears the previous provider secret');
assert.equal(zohoDiv.querySelector('input').value,'');
await act(async()=>zohoButton('Disconnect dedicated inbox').click());
assert.equal(zohoView.source,'none');
assert.ok(!zohoCalls.some(c=>c.url.includes('/google')));
await act(async()=>zohoRoot.unmount());
console.log('Zoho provider setup, saved credential reuse, server changes and provider isolation: passed');

const sendingDiv = document.body.appendChild(document.createElement('div'));
const sendingRoot = createRoot(sendingDiv);
const sendingCalls = [];
let sendingView = {source:'zoho',provider:'zoho',inbox_id:'assistant@example.com',address:'assistant@example.com',
  host:'imappro.zoho.com',connected:true,active:true,sending:{configured:true,connected:false,transport:'https'}};
globalThis.fetch = async (url, init) => {
  sendingCalls.push({url, init});
  if (url.endsWith('/zoho/connect')) return {ok:false,json:async()=>({detail:'Sign in with the dedicated mailbox account.'})};
  if (url.endsWith('/zoho/disconnect')) return {ok:true,json:async()=>({configured:true,connected:false,transport:'https'})};
  return {ok:true,json:async()=>sendingView};
};
const sendingButton = text => [...sendingDiv.querySelectorAll('button')].find(b=>b.textContent===text);
await act(async()=>sendingRoot.render(React.createElement(EmailInboxConnection)));
assert.equal(sendingCalls.length,1,'Opening Connections never starts OAuth or sends');
assert.match(sendingDiv.textContent,/Authorization needed/);
assert.match(sendingDiv.textContent,/separate Zoho authorization/);
assert.ok(sendingButton('Authorize Zoho sending'));
await act(async()=>sendingButton('Authorize Zoho sending').click());
assert.equal(sendingCalls.at(-1).url,'/api/connections/zoho/connect');
assert.equal(sendingCalls.at(-1).init.body,'{}','Client cannot select scopes, account or sender');
assert.match(sendingDiv.querySelector('[role=alert]').textContent,/dedicated mailbox/);
sendingView = {...sendingView,sending:{configured:true,connected:true,transport:'https'}};
await act(async()=>window.dispatchEvent(new Event('focus')));
assert.match(sendingDiv.textContent,/Connected over HTTPS/);
assert.ok(sendingButton('Reauthorize Zoho sending'));
await act(async()=>sendingButton('Disable Zoho sending').click());
assert.match(sendingDiv.textContent,/Inbox reading remains connected/);
assert.match(sendingDiv.textContent,/Authorization needed/);
assert.ok(sendingButton('Copy inbox address'),'Disabling sending preserves inbox');
sendingView = {...sendingView,sending:{configured:false,connected:false}};
await act(async()=>window.dispatchEvent(new Event('focus')));
assert.equal(sendingButton('Authorize Zoho sending'),undefined);
assert.match(sendingDiv.textContent,/Server setup needed/);
assert.ok(!sendingCalls.some(c=>c.url.includes('/email-drafts')),'Setup never submits a draft');
await act(async()=>sendingRoot.unmount());
console.log('HTTPS sending status, explicit consent, setup errors and independent disconnect: passed');
