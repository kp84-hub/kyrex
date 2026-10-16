import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { useChat } from '../src/hooks/useChat.js';

let latest, nextId = 0, rejectTurn = false, failAttachment = false;
let delayedAttachment, delayedConversation, holdAttachment = false, holdConversation = false;
let roster = [{id:'chief',name:'The Overwatcher',status:'running'}];
const conversations = new Map([
  ['legacy-bot', {conversation_id:'legacy-bot',bot_id:'chief',workspace_id:'kyrex',messages:[]}],
  ['plain', {conversation_id:'plain',messages:[]}],
  ['saved-workspace', {conversation_id:'saved-workspace',workspace_id:'saved-repo',messages:[]}],
]);
const posts = [], attachments = [];
const json = (body, status = 200) => new Response(JSON.stringify(body), {status,headers:{'Content-Type':'application/json'}});
globalThis.fetch = async (url, options = {}) => {
  if (url === '/api/bots') return json({bots:roster});
  if (url === '/api/conversations' && options.method === 'POST') {
    const body = JSON.parse(options.body), conversation = {conversation_id:`created-${++nextId}`,messages:[]};
    if (body.bot_id) conversation.bot_id = body.bot_id;
    conversations.set(conversation.conversation_id, conversation);
    return json(conversation);
  }
  if (url === '/api/conversations') return json({conversations:[...conversations.values()]});
  if (url.startsWith('/api/conversations/')) {
    const id = url.split('/').at(-1);
    if (options.method === 'DELETE') { conversations.delete(id); return json({ok:true}); }
    if (holdConversation && id === 'legacy-bot') return new Promise(resolve => {
      delayedConversation = () => resolve(json(conversations.get(id)));
    });
    return json(conversations.get(id) || {});
  }
  if (url === '/api/chat/workspace') {
    const body = JSON.parse(options.body);
    attachments.push(body);
    assert.equal(conversations.get(body.conversation_id)?.bot_id, undefined, 'Never attach a workspace to a Bot');
    if (failAttachment) return json({detail:'Workspace unavailable'},400);
    if (holdAttachment) return new Promise(resolve => { delayedAttachment = () => resolve(json({workspace_id:body.workspace_id})); });
    return json({workspace_id:body.workspace_id});
  }
  if (url === '/api/chat') {
    const body = JSON.parse(options.body);
    posts.push(body);
    if (conversations.get(body.conversation_id)?.bot_id && body.workspace_id) {
      return json({detail:'a bot-bound conversation cannot attach a workspace'},400);
    }
    if (rejectTurn) return json({detail:'Temporary test error'},400);
    return new Response('data: '+JSON.stringify({type:'done',conversation_id:body.conversation_id,content:'Calendar checked.'})+'\n\n',
      {headers:{'Content-Type':'text/event-stream'}});
  }
  return json({providers:[],workspaces:[]});
};
function Probe(){ latest=useChat(); return null; }
const node=document.body.appendChild(document.createElement('div')), root=createRoot(node);
globalThis.localStorage=window.localStorage;
localStorage.clear();
await act(async()=>root.render(React.createElement(Probe)));
const call = async (name,...args) => act(async()=>{ await latest[name](...args); });
const last = () => posts.at(-1);
const noWorkspace = () => assert.equal(Object.hasOwn(last(),'workspace_id'),false);

// A workspace preselected on the blank screen cannot follow the Bot picker.
await call('attachWorkspace','kyrex');
assert.equal(latest.activeWorkspaceId,'kyrex');
await call('newChat','chief');
assert.equal(latest.activeBotId,'chief');
assert.equal(latest.activeWorkspaceId,null);
await call('send',"What's on my calendar?");
noWorkspace();
assert.equal(latest.error,null);
assert.equal(latest.messages.at(-1).content,'Calendar checked.');
await call('newChat');
await call('send','Ordinary chat after the Bot');
noWorkspace();

// Restore even a legacy record carrying both fields; Retry must stay scoped.
await call('loadConversation','legacy-bot');
assert.equal(latest.activeWorkspaceId,null);
await call('attachWorkspace','kyrex');
assert.equal(attachments.length,0);
rejectTurn=true;
await call('send',"What's on my calendar?");
const failedCount=posts.length;
noWorkspace();
rejectTurn=false;
await act(async()=>{ latest.retry(); await new Promise(resolve=>setTimeout(resolve,20)); });
assert.equal(posts.length,failedCount+1);
noWorkspace();
assert.equal(latest.error,null);

// A roster refresh can hide a deleted Bot, but cannot change its saved binding.
roster=[];
await call('refreshBots');
assert.equal(latest.activeBotId,null);
await call('attachWorkspace','other-repo');
assert.equal(attachments.length,0);
await call('send','Still the stored Bot conversation');
noWorkspace();
roster=[{id:'chief',name:'The Overwatcher',status:'running'}];

// Ordinary Chat still supports pending, persisted and explicitly changed workspaces.
await call('newChat');
await call('removeConversation',latest.activeId);
await call('attachWorkspace','selected-repo');
await call('newChat');
await call('send','Inspect the selected repository');
assert.equal(last().workspace_id,'selected-repo');
await call('loadConversation','saved-workspace');
await call('send','Inspect the saved repository');
assert.equal(last().workspace_id,'saved-repo');
await call('attachWorkspace',null);
await call('send','Ordinary chat without a workspace');
noWorkspace();

// A delayed attachment response for a different thread cannot poison a Bot.
await call('loadConversation','plain');
holdAttachment=true;
let attaching;
await act(async()=>{ attaching=latest.attachWorkspace('slow-repo'); });
await call('loadConversation','legacy-bot');
await act(async()=>{ delayedAttachment(); await attaching; });
holdAttachment=false;
assert.equal(latest.activeWorkspaceId,null);
await call('send','Calendar after a delayed attachment');
noWorkspace();

// An unsuccessful active-chat attachment never becomes a pending global choice.
await call('loadConversation','plain');
failAttachment=true;
await call('attachWorkspace','unavailable-repo');
failAttachment=false;
await call('newChat');
await call('send','Fresh ordinary conversation');
noWorkspace();

// Do not submit with the previous conversation's context while another loads.
holdConversation=true;
let loading;
await act(async()=>{ loading=latest.loadConversation('legacy-bot'); });
assert.equal(latest.isConversationReady,false);
const beforeLoad=posts.length;
await call('send','Do not use the previous workspace');
assert.equal(posts.length,beforeLoad);
await act(async()=>{ delayedConversation(); await loading; });
holdConversation=false;
assert.equal(latest.isConversationReady,true);
await call('send','Calendar after the Bot finishes opening');
noWorkspace();
await act(async()=>root.unmount());
console.log('Workspace selections, Bot loads/retries, roster changes and delayed attachment responses stay conversation-scoped: passed');
