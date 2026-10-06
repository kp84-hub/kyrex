import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import MessageSendCard from '../src/components/MessageSendCard.jsx';
import { consumeStream } from '../src/lib/streaming.js';

let state='ready';let decisions=[];let replyReads=0;let failGet=false;
const payload=()=>({id:'send-1',state,name:'Ethan The Neighbor',text:'Exact message 🌎',recipients:['Ethan · +15555550123','Other group member · +15555550124']});
globalThis.fetch=async(url,options)=>{
 if(failGet && !options) throw Error('Network unavailable');
 let data;
 if(url.endsWith('/decision')){const d=JSON.parse(options.body).decision;decisions.push(d);state=d==='send'?'accepted':'cancelled';data=payload();}
 else if(url.endsWith('/reply')){replyReads++;data={content:'Ethan — today\n\nYes I am home\n\nSnapshot synced today.'};}
 else data=payload();
 return {ok:true,json:async()=>data};
};
const node=document.createElement('div');document.body.append(node);const root=createRoot(node);
await act(async()=>root.render(React.createElement(MessageSendCard,{id:'send-1'})));
assert.equal(decisions.length,0,'mounting a verified preview must never send');
assert.match(node.textContent,/Exact message 🌎/);assert.match(node.textContent,/Other group member/);
const button=(text)=>[...node.querySelectorAll('button')].find(b=>b.textContent===text);
await act(async()=>button('Send').click());
assert.deepEqual(decisions,['send']);assert.equal(button('Send'),undefined);
assert.match(node.textContent,/accepted the send/);
await act(async()=>button('Read reply').click());
assert.equal(replyReads,1);assert.match(node.querySelector('[aria-label="Latest reply"]').textContent,/Yes I am home/);
assert.equal(node.textContent.includes('unrelated conversation'),false);
await act(async()=>root.unmount());
state='ready';const root2=createRoot(node);
await act(async()=>root2.render(React.createElement(MessageSendCard,{id:'send-2'})));
await act(async()=>button('Cancel').click());
assert.deepEqual(decisions,['send','cancel']);assert.equal(button('Send'),undefined);
await act(async()=>root2.unmount());
failGet=true;state='ready';const root3=createRoot(node);
await act(async()=>root3.render(React.createElement(MessageSendCard,{id:'send-3'})));
assert.ok(button('Check status'));assert.equal(button('Send'),undefined);
failGet=false;await act(async()=>button('Check status').click());
assert.ok(button('Send'));assert.deepEqual(decisions,['send','cancel'],'status recovery must never resend');
await act(async()=>root3.unmount());
let card;
async function* stream(){yield {type:'message_send',send_id:'scoped-id'};yield {type:'done',content:'Preview ready'};}
await consumeStream(stream(),{onMessageSend:event=>card=event.send_id});
assert.equal(card,'scoped-id');
console.log('Chat explicit Send/Cancel, all recipients, focused reply and SSE card: passed');
