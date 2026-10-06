import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { useChat } from '../src/hooks/useChat.js';
import { consumeStream } from '../src/lib/streaming.js';

let latest, frames = [], requests = 0;
function Probe() { latest = useChat(); return null; }
const response = body => ({ok:true,status:200,json:async()=>body});
globalThis.fetch = async url => {
 if (url === '/api/chat') {
  requests++;
  return {ok:true,body:new ReadableStream({start(controller) {
   for (const frame of frames) controller.enqueue(new TextEncoder().encode('data: '+JSON.stringify(frame)+'\n\n'));
   controller.close();
  }})};
 }
 if (url === '/api/conversations/c1') return response({conversation_id:'c1',messages:[]});
 if (url === '/api/conversations') return response({conversations:[{conversation_id:'c1'}]});
 if (url === '/api/bots') return response({bots:[]});
 return response({});
};
const root=createRoot(document.body.appendChild(document.createElement('div')));
await act(async()=>root.render(React.createElement(Probe)));
await act(async()=>latest.loadConversation('c1'));
await act(async()=>latest.send('Show my messages'));
assert.match(latest.error,/ended before the reply completed/);
assert.equal(latest.messages.at(-1).streaming,false);
assert.equal(latest.messages.at(-1).cancelled,undefined);
assert.equal(latest.isGenerating,false);
frames=[{type:'delta',content:'Partial reply'}];
await act(async()=>latest.send('Another request'));
assert.equal(latest.messages.at(-1).content,'Partial reply');
assert.match(latest.messages.at(-1).error,/ended before/);
frames=[{type:'done',content:''}];
await act(async()=>latest.send('Empty response'));
assert.match(latest.error,/finished without a reply/);
frames=[{type:'delta',content:'draft'},{type:'done',content:'Complete reply'}];
await act(async()=>latest.send('Working response'));
assert.equal(latest.messages.at(-1).content,'Complete reply');
assert.equal(latest.error,null);
assert.equal(requests,4,'an interrupted action must never be automatically retried');
await act(async()=>root.unmount());
async function* cancelled(){yield {type:'delta',content:'partial'};throw Object.assign(new Error('Stopped'),{name:'AbortError'});}
assert.equal((await consumeStream(cancelled())).terminal.kind,'aborted');
async function* card(){yield {type:'message_send',send_id:'s1'};yield {type:'done',content:''};}
assert.equal((await consumeStream(card())).terminal.kind,'done','a send card is a valid result even without reply text');
console.log('Interrupted/empty replies are visible, partial text preserved, no retries, explicit Stop retained: passed');
