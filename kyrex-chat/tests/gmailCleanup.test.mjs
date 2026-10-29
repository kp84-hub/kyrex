import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Message from '../src/components/Message.jsx';
import { GmailCleanupSettings } from '../src/components/GmailCleanup.jsx';
import { consumeStream } from '../src/lib/streaming.js';

const node=document.body.appendChild(document.createElement('div'));
let root=createRoot(node), writes=[];
let view={id:'review',revision:'a'.repeat(32),state:'ready',detail:'Review the exact messages.',
  mailbox:'alice@example.test',sender:'offers@shop.example',messages:[{id:'one',subject:'<script>unsafe email subject</script>',date:'2026-10-29'}],
  count:1,more:true,approval_id:'approval-one',task_status:'awaiting_approval'};
globalThis.fetch=async (url,init={})=>{
  if(init.method==='POST') {
    const body=JSON.parse(init.body); writes.push({url,body});
    if(url.endsWith('/decision')) {
      assert.deepEqual(body,{revision:view.revision,approval_id:'approval-one',decision:'trash'});
      view={...view,state:'trashed',detail:'Moved the reviewed message to Trash.',approval_id:null,task_status:'done'};
    }
  }
  return {ok:true,json:async()=>view};
};
const button=text=>[...node.querySelectorAll('button')].find(b=>b.textContent===text);
const render=()=>act(async()=>root.render(React.createElement(Message,{message:{id:'one',role:'assistant',content:'Preparing preview',gmail_cleanup:{id:'review'}}})));
await render();
assert.equal(writes.length,0,'opening a preview never approves a write');
assert.match(node.textContent,/alice@example.test/); assert.match(node.textContent,/offers@shop.example/);
assert.match(node.textContent,/More matches may remain/);
assert.equal(node.querySelector('script'),null,'subjects remain inert text');
await act(async()=>button('Move listed emails to Trash').click());
assert.equal(writes.length,1); assert.match(node.textContent,/Moved the reviewed message/);
assert.equal(button('Move listed emails to Trash'),undefined);
await act(async()=>root.unmount()); root=createRoot(node);
await render(); assert.equal(writes.length,1,'reopening cannot replay Trash');
await act(async()=>root.unmount()); root=createRoot(node);
view={...view,state:'unknown',detail:'Check Gmail before another cleanup.',task_status:'failed'};
await render(); assert.equal(button('Move listed emails to Trash'),undefined);
assert.match(node.textContent,/Check Gmail/);
await act(async()=>root.unmount()); root=createRoot(node);
globalThis.fetch=async()=>({ok:true,json:async()=>({connected:false,address:'',revision:''})});
await act(async()=>root.render(React.createElement(GmailCleanupSettings)));
assert.equal(writes.length,1,'opening settings never requests extra Google permissions');
assert.match(node.textContent,/broad Gmail modify permission/);
assert.match(node.textContent,/read-only connections/);
assert.ok(button('Connect Gmail cleanup'));
await act(async()=>root.unmount()); node.remove();
async function* frames(){yield {type:'done',content:'Preview ready',gmail_cleanup:{id:'review'}};}
const result=await consumeStream(frames());
assert.equal(result.terminal.gmail_cleanup.id,'review','cleanup references survive the terminal SSE frame');
console.log('Gmail cleanup: preview, exact approval, inert subjects, unknown outcome, reconnect safety and SSE passed.');
