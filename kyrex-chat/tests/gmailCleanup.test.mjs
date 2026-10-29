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
  count:2501,more:false,offset:0,page_size:50,has_next:true,scan_complete:true,approval_id:'approval-one',task_status:'awaiting_approval'};
globalThis.fetch=async (url,init={})=>{
  if(init.method==='POST') {
    const body=JSON.parse(init.body); writes.push({url,body});
    if(url.endsWith('/decision')) {
      assert.deepEqual(body,{revision:view.revision,approval_id:'approval-one',decision:'trash'});
      view={...view,state:'trashed',detail:'Moved the reviewed message to Trash.',approval_id:null,task_status:'done'};
    }
  }
  const offset=Number(new URL(url,'https://chat.test').searchParams.get('offset') || 0);
  return {ok:true,json:async()=>({...view,offset,has_next:offset===0,
    messages:offset ? [{id:'next',subject:'Email from the next page',date:'2026-10-29'}] : view.messages})};
};
const button=text=>[...node.querySelectorAll('button')].find(b=>b.textContent===text);
const render=()=>act(async()=>root.render(React.createElement(Message,{message:{id:'one',role:'assistant',content:'Preparing preview',gmail_cleanup:{id:'review'}}})));
await render();
assert.equal(writes.length,0,'opening a preview never approves a write');
assert.match(node.textContent,/alice@example.test/); assert.match(node.textContent,/offers@shop.example/);
assert.match(node.textContent,/2,501 matching emails/);
assert.match(node.textContent,/One approval covers this entire frozen set/);
assert.equal(node.querySelector('script'),null,'subjects remain inert text');
await act(async()=>button('Next emails').click());
assert.match(node.textContent,/Email from the next page/); assert.equal(writes.length,0);
await act(async()=>button('Move all 2,501 emails to Trash').click());
assert.equal(writes.length,1); assert.match(node.textContent,/Moved the reviewed message/);
assert.equal(button('Move all 2,501 emails to Trash'),undefined);
await act(async()=>root.unmount()); root=createRoot(node);
await render(); assert.equal(writes.length,1,'reopening cannot replay Trash');
await act(async()=>root.unmount()); root=createRoot(node);
view={...view,state:'unknown',detail:'Check Gmail before another cleanup.',task_status:'failed'};
await render(); assert.equal(button('Move all 2,501 emails to Trash'),undefined);
assert.match(node.textContent,/Check Gmail/);
await act(async()=>root.unmount()); root=createRoot(node);
view={...view,state:'executing',detail:'Moving the approved set.',attempted:true,moved_count:1000,in_flight_count:1000,task_status:'running'};
await render();
assert.match(node.textContent,/1,000 of 2,501 confirmed/);
assert.ok(button('Stop cleanup')); assert.equal(button('Move all 2,501 emails to Trash'),undefined);
await act(async()=>root.unmount()); root=createRoot(node);
view={...view,state:'preparing',count:500,scan_complete:false,attempted:false};
await render(); assert.match(node.textContent,/Still scanning/);
assert.equal(button('Move all 500 emails to Trash'),undefined);
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
