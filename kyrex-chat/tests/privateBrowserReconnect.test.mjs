import assert from 'node:assert/strict';
import {test} from 'node:test';
import React, {act} from 'react';
import {createRoot} from 'react-dom/client';
import PrivateBrowserReconnect from '../src/components/PrivateBrowserReconnect.jsx';

const response = body => ({ok:true,status:200,json:async()=>body});
const click = node => act(async () => node.dispatchEvent(new window.MouseEvent('click',{bubbles:true})));
async function mount(props={}) {
  const el=document.createElement('div');document.body.appendChild(el);const root=createRoot(el);
  await act(async()=>root.render(React.createElement(PrivateBrowserReconnect,
    {botId:'browser-a',url:'https://fixture.test/login',available:true,...props})));
  return {el,close:async()=>{await act(async()=>root.unmount());el.remove();}};
}
test('status is read-only; reconnect opens private URL and end binds the exact lease',async()=>{
  const calls=[];
  globalThis.fetch=async(url,options={})=>{
    calls.push({url,options});
    if(!options.method)return response({state:'idle'});
    const body=JSON.parse(options.body);
    return response({state:body.action==='end'?'stopping':'ready',lease:'a'.repeat(32),viewer_url:'https://fixture.ts.net/vnc.html'});
  };
  const ui=await mount();
  assert.equal(calls.filter(c=>c.options.method).length,0);
  await click([...ui.el.querySelectorAll('button')].find(b=>b.textContent==='Reconnect sign-in'));
  const link=ui.el.querySelector('a');assert.equal(link.href,'https://fixture.ts.net/vnc.html');
  assert.equal(link.target,'_blank');assert.match(link.rel,/noopener/);
  await click([...ui.el.querySelectorAll('button')].find(b=>b.textContent==='Finish sign-in and hand back'));
  const bodies=calls.filter(c=>c.options.method).map(c=>JSON.parse(c.options.body));
  assert.equal(bodies[0].bot_id,'browser-a');assert.equal(bodies[0].url,'https://fixture.test/login');
  assert.equal(bodies[1].action,'end');assert.equal(bodies[1].lease,'a'.repeat(32));assert.equal(bodies[1].url,'');
  assert.match(ui.el.textContent,/saving your sign-in/);await ui.close();
});
test('lost acknowledgement retries the same request, without automatic restart',async()=>{
  const ids=[];globalThis.fetch=async(url,options={})=>{
    if(!options.method)return response({state:'idle'});
    ids.push(JSON.parse(options.body).request_id);if(ids.length===1)throw Error('lost ack');
    return response({state:'pending'});
  };
  const ui=await mount();const button=ui.el.querySelector('button');await click(button);
  assert.match(ui.el.querySelector('[role=alert]').textContent,/same request/);await click(button);
  assert.equal(ids.length,2);assert.equal(ids[0],ids[1]);await ui.close();
});
test('unconfigured host has no start action or network request',async()=>{
  let calls=0;globalThis.fetch=async()=>{calls++;return response({});};
  const ui=await mount({available:false});assert.equal(calls,0);assert.equal(ui.el.querySelector('button'),null);await ui.close();
});
test('foreign viewer URL is not linked even in a forged host response',async()=>{
  globalThis.fetch=async()=>response({state:'ready',lease:'a'.repeat(32),viewer_url:'https://evil.test/vnc.html'});
  const ui=await mount();assert.equal(ui.el.querySelector('a'),null);await ui.close();
});
