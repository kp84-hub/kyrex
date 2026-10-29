import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import App from '../src/App.jsx';
import Sidebar from '../src/components/Sidebar.jsx';
import MessageList from '../src/components/MessageList.jsx';

globalThis.localStorage = window.localStorage;
localStorage.clear(); localStorage.setItem('kyrex-chat.activeConversationId','chief-chat');
globalThis.EventSource = class {
  constructor() { this.listeners=new Map(); }
  addEventListener(type,callback) { this.listeners.set(type,callback); }
  close() {}
};
const records=new Map();
function save(id,bot_id,title,content) {
  const row={conversation_id:id,bot_id,title,updated_at:'2026-10-29T18:00:00Z',
    messages:[{id:id+'-reply',role:'assistant',content}],latest_update:content};
  records.set(id,row); return row;
}
save('chief-chat','chief','The Overwatcher','Saved coordinator history.');
for(let i=1;i<=4;i++) save('calendar-'+i,'calendar','Calendar history '+i,'Saved calendar conversation '+i);
records.get('calendar-1').activity={kind:'task',task_id:'pending',status:'awaiting_approval',text:'Calendar change needs approval'};
save('plain-1',null,'New chat','First ordinary chat.'); save('plain-2',null,'New chat','Second ordinary chat.');
const roster=[{id:'chief',name:'The Overwatcher',status:'running'},
  {id:'calendar',name:'Calendar Bot',status:'running'}, {id:'dev',name:'Developer Bot',status:'running'}];
const posts=[]; let sequence=0,holdCalendar=false,releaseCalendar;
const response=value=>({ok:true,status:200,json:async()=>value});
const latest=bot=>[...records.values()].reverse().find(row=>row.bot_id===bot);
globalThis.fetch=async(url,options={})=>{
  if(options.method==='POST') posts.push({url,body:JSON.parse(options.body || '{}')});
  if(url==='/api/chat/onboarding') return response({required:false,step:'ready'});
  if(url==='/api/bots') return response({bots:roster});
  if(url==='/api/conversations' && options.method==='POST') {
    const body=JSON.parse(options.body); return response(save('created-'+(++sequence),body.bot_id || null,'New chat',''));
  }
  if(url==='/api/conversations') return response({conversations:[...records.values()].reverse()});
  if(url.startsWith('/api/conversations/') && !options.method) {
    const row=records.get(url.split('/').at(-1));
    if(!row?.bot_id) return response(row);
    return response({...row,messages:[...records.values()].filter(source=>source.bot_id===row.bot_id)
      .flatMap(source=>source.messages.map(message=>({...message,source_conversation_id:source.conversation_id,
        source_message_id:message.id,id:source===row?message.id:source.conversation_id+':'+message.id})))});
  }
  const match=url.match(/^\/api\/bots\/([^/]+)\/conversation$/);
  if(match) {
    const bot=match[1];
    const row=latest(bot) || save(bot+'-chat',bot,'Bot chat','Saved '+bot+' history.');
    if(holdCalendar && bot==='calendar') return new Promise(resolve=>{releaseCalendar=()=>resolve(response(row));});
    return response(row);
  }
  if(url.startsWith('/api/delegations')) return response({delegations:[],relayed:[]});
  return response({providers:[],workspaces:[],available:true});
};
const node=document.body.appendChild(document.createElement('div')),root=createRoot(node);
const picker=()=>node.querySelector('select[aria-label="Select Bot"]');
const choose=async id=>act(async()=>{picker().value=id;picker().dispatchEvent(new Event('change',{bubbles:true}));});
const current=()=>node.querySelector('.conversation-item[aria-current="true"]');
try {
  await act(async()=>root.render(React.createElement(App)));
  assert.equal(node.querySelectorAll('.conversation-group').length,2);
  assert.equal(node.querySelectorAll('.conversation-item').length,4,'four Calendar chats occupy one row; ordinary chats remain separate');
  assert.equal(node.querySelector('.conversation-history-toggle'),null,'one continuous chat has no History submenu');
  assert.match(node.querySelector('.conversation-list').textContent,/Waiting for your approval/);
  await choose('calendar');
  assert.match(node.querySelector('.message-list').textContent,/Saved calendar conversation 4/);
  await choose('chief'); await choose('calendar');
  assert.equal(records.size,7,'switching repeatedly never creates another chat');
  assert.ok(posts.every(p=>p.url.endsWith('/conversation')));
  for(let i=1;i<=4;i++) assert.match(node.querySelector('.message-list').textContent,new RegExp('Saved calendar conversation '+i));
  assert.equal(current().querySelector('.conversation-title').textContent,'Calendar Bot');
  assert.equal(posts.filter(p=>p.url==='/api/conversations').length,0,'all navigation reuses the ongoing conversation');
  await choose('chief');
  holdCalendar=true; await choose('calendar');
  assert.ok(releaseCalendar);
  await choose('dev');
  await act(async()=>releaseCalendar());
  assert.equal(picker().value,'dev','a slow earlier selection cannot switch away from the later Bot');
  assert.match(node.querySelector('.message-list').textContent,/Saved dev history/);
  assert.ok(!posts.some(p=>/\/api\/chat$|\/api\/task|\/respond|\/approve/.test(p.url)),'navigation never submits or approves work');
} finally {
  await act(async()=>root.unmount()); node.remove(); delete globalThis.EventSource;
}
const side=document.body.appendChild(document.createElement('div')),sideRoot=createRoot(side);
try {
  await act(async()=>sideRoot.render(React.createElement(Sidebar,{
    conversations:[{conversation_id:'a',bot_id:'one',title:'Same name'},
      {conversation_id:'b',bot_id:'two',title:'Same name'},
      {conversation_id:'plain-a',title:'Same title'},{conversation_id:'plain-b',title:'Same title'}],
    bots:[{id:'one',name:'Calendar Bot'},{id:'two',name:'Calendar Bot'}],activeId:'b',activityLines:{}
  })));
  assert.equal(side.querySelectorAll('.conversation-group').length,2,'different Bot IDs remain separate even with identical names');
  assert.equal(side.querySelectorAll('.conversation-item').length,4,'ordinary chats are not grouped by title');
  assert.equal(side.querySelectorAll('[aria-current="true"]').length,1);
} finally { await act(async()=>sideRoot.unmount()); side.remove(); }
const messagesNode=document.body.appendChild(document.createElement('div')),messagesRoot=createRoot(messagesNode);
try {
  await act(async()=>messagesRoot.render(React.createElement(MessageList,{
    messages:[{id:'legacy:original-message',source_conversation_id:'legacy-calendar',source_message_id:'original-message',
      role:'assistant',content:'Prepared workout',message_draft:{recipient:'Test group'}}],
    conversationId:'ongoing-calendar',isGenerating:false
  })));
  await act(async()=>[...messagesNode.querySelectorAll('button')].find(button=>button.textContent==='Prepare for Test group').click());
  assert.ok(posts.some(post=>post.url==='/api/conversations/legacy-calendar/messages/original-message/prepare-send'),
    'an older message action keeps its original conversation and message binding');
} finally { await act(async()=>messagesRoot.unmount()); messagesNode.remove(); }
console.log('One continuous conversation per Bot: saved messages, origin-bound actions, active work and navigation ordering passed');
