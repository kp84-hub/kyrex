import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import EmailAccountsConnection from '../src/components/EmailAccountsConnection.jsx';

let view = { selection: 'legacy', gmail_configured: true, accounts: [
  { id:'legacy',provider:'zoho',address:'primary@example.com',label:'primary@example.com',status:'connected' },
  { id:'a'.repeat(32),provider:'gmail',address:'first@example.com',label:'first@example.com',status:'connected',connection_revision:'old-grant' },
  { id:'b'.repeat(32),provider:'gmail',address:'second@example.com',label:'second@example.com',status:'connected' },
] };
const calls = [];
let fail = false, changes = 0, popupUrl;
const originalInterval=globalThis.setInterval, originalClear=globalThis.clearInterval;
let poll;
globalThis.setInterval=callback=>{poll=callback;return 1;};
globalThis.clearInterval=()=>{poll=null;};
window.open = url => {
  assert.equal(url, 'about:blank');
  return { opener:window, closed:false, close() {}, location:{replace(url) { popupUrl=url; }} };
};
globalThis.fetch = async (url, init = {}) => {
  const data=init.body ? JSON.parse(init.body) : undefined;
  calls.push({url,data});
  if (fail) return {ok:false,json:async()=>({detail:'This account is unavailable.'})};
  if (url.endsWith('/read-scope')) view={...view,selection:data.selection};
  if (url.endsWith('/accounts/gmail')) return {ok:true,json:async()=>({
    account_id:data.account_id || 'c'.repeat(32),expires_at:Date.now()/1000+600,
    connection_revision:data.account_id ? view.accounts.find(row=>row.id===data.account_id).connection_revision : 'new-pending-grant',
    authorization_url:'https://accounts.google.com/o/oauth2/v2/auth?state=MAILBOX-TEST',
  })};
  if (url.endsWith('/disconnect')) {
    const id=url.split('/').at(-2); view={...view,accounts:view.accounts.filter(row=>row.id!==id)};
  }
  if (url.endsWith('/accounts') && data) view={...view,accounts:[...view.accounts,
    {id:'d'.repeat(32),provider:'zoho',address:data.email,label:data.email,status:'connected'}]};
  return {ok:true,json:async()=>view};
};
const div=document.body.appendChild(document.createElement('div'));
const root=createRoot(div);
const button=text=>[...div.querySelectorAll('button')].find(item=>item.textContent===text);
const select=async(label,value)=>act(async()=>{
  const input=div.querySelector(`[aria-label="${label}"]`);
  Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set.call(input,value);
  input.dispatchEvent(new Event('change',{bubbles:true}));
});
const edit=async(input,value)=>act(async()=>{
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,value);
  input.dispatchEvent(new Event('input',{bubbles:true}));
});
await act(async()=>root.render(React.createElement(EmailAccountsConnection,{onChange:()=>changes++})));
assert.equal(calls.length,1,'Opening accounts only reads metadata');
assert.match(div.textContent,/first@example.com/); assert.match(div.textContent,/second@example.com/);
assert.equal(div.querySelector('[aria-label="Email search scope"]').value,'legacy');
await select('Email search scope','all');
assert.equal(view.selection,'all');
assert.ok(!calls.some(call=>call.url.includes('/google/disconnect')||call.url.endsWith('/email-inbox/source')));
await act(async()=>button('Add Gmail account').closest('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
assert.match(popupUrl,/^https:\/\/accounts.google.com\//);
assert.equal(view.accounts.length,3,'Opening authorization does not claim another connected mailbox');
assert.equal(view.selection,'all','Starting consent does not change the read scope');
await act(async()=>button('Reconnect first@example.com').click());
await act(async()=>poll());
assert.match(div.textContent,/Finish authorization with Google/,'An old connected grant cannot count as a completed reconnect');
view={...view,accounts:view.accounts.map(row=>row.id==='a'.repeat(32)?{...row,connection_revision:'new-grant'}:row)};
await act(async()=>poll());
assert.match(div.textContent,/Mailbox connected/);
assert.doesNotMatch(div.textContent,/Finish authorization with Google/);
await select('Add email account','zoho');
await edit(div.querySelector('input[type=email]'),'additional@example.com');
await edit(div.querySelector('input[type=password]'),'mailbox-test-app-password');
await act(async()=>button('Add read-only inbox').closest('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));
assert.equal(div.querySelector('input[type=password]').value,'','A saved secret is cleared, never read back');
assert.match(div.textContent,/additional@example.com/);
assert.equal(view.accounts.find(row=>row.id==='legacy').address,'primary@example.com');
await act(async()=>button('Disconnect first@example.com').click());
assert.ok(!view.accounts.some(row=>row.address==='first@example.com'));
assert.ok(view.accounts.some(row=>row.address==='second@example.com'));
fail=true;
await select('Email search scope','none');
assert.equal(view.selection,'all','Failed scope updates do not claim a successful change');
assert.match(div.querySelector('[role=alert]').textContent,/unavailable/);
assert.ok(changes>=3);
await act(async()=>root.unmount());
globalThis.setInterval=originalInterval; globalThis.clearInterval=originalClear;
console.log('Multiple email account consent, explicit scope, independent disconnect and secret clearing: passed');
