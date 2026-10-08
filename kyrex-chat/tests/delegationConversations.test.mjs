import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import App from '../src/App.jsx';

globalThis.localStorage = window.localStorage;
localStorage.setItem('kyrex-chat.activeConversationId', 'chief-chat');
const sources = [];
globalThis.EventSource = class {
  listeners = new Map();
  constructor(url) { this.url = url; sources.push(this); }
  addEventListener(type, fn) { this.listeners.set(type, fn); }
  close() { this.closed = true; }
  emit(type, payload) { this.listeners.get(type)?.({ data: JSON.stringify(payload) }); }
};
const response = body => ({ ok: true, status: 200, async json() { return body; } });
let discovered = false, terminal = false, listReads = 0, targetReads = 0;
const posts = [];
const row = { delegation_id: 'delegation', task_id: 'existing-task', target_bot_id: 'dev',
  target_bot_name: 'Developer Bot', executor_prefix: 'developer', parent_conversation_id: 'chief-chat',
  target_conversation_id: 'dev-chat', text: 'Inspect the progress flow', status: 'running',
  progress: [{ stage: 'Inspecting the relevant files…' }] };
const result = 'Recommendation: relay the target progress.\n\n' + 'Full technical evidence. '.repeat(80);
globalThis.fetch = async (url, options = {}) => {
  if (options.method === 'POST') posts.push(url);
  if (url.startsWith('/api/delegations')) {
    if (url.includes('chief-chat')) { discovered = true; return response({ delegations: [row], relayed: [] }); }
    return response({ delegations: [], relayed: [] });
  }
  if (url === '/api/conversations') {
    listReads++;
    const activity = terminal ? null : { kind: 'task', task_id: row.task_id, status: 'running' };
    return response({ conversations: [
      { conversation_id: 'chief-chat', bot_id: 'chief', activity: discovered ? { ...activity, kind: 'delegation' } : null },
      ...(discovered ? [{ conversation_id: 'dev-chat', bot_id: 'dev', activity }] : []),
    ] });
  }
  if (url === '/api/conversations/chief-chat') return response({ conversation_id: 'chief-chat', bot_id: 'chief',
    messages: [{ id: 'handoff', role: 'assistant', content: 'The Developer Bot is inspecting the progress flow.' },
      ...(terminal ? [{ id: 'notice', role: 'assistant', content: 'Developer Bot finished. The full result is in its chat.',
        delegation_result: { conversation_id: 'dev-chat', bot_name: 'Developer Bot' } }] : [])] });
  if (url === '/api/conversations/dev-chat') {
    targetReads++;
    return response({ conversation_id: 'dev-chat', bot_id: 'dev', messages: [
      { id: 'delegation-request', role: 'user', content: row.text },
      { id: 'task-existing-task-result', role: 'assistant', content: terminal ? result : '',
        developer_result: true,
        ...(terminal ? {} : { delegated_active: true, task: { taskId: 'existing-task', status: 'running' } }),
        events: [{ kind: 'progress', payload: row.progress[0] }] },
    ] });
  }
  if (url === '/api/bots') return response({ bots: [
    { id: 'chief', name: 'The Overwatcher' }, { id: 'dev', name: 'Developer Bot' },
  ] });
  return response({});
};
const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div);
try {
  await act(async () => root.render(React.createElement(App)));
  assert.ok(listReads >= 2, 'newly discovered target chat refreshes the sidebar immediately');
  assert.match(div.querySelector('.sidebar').textContent, /Developer Bot/);
  assert.equal(div.querySelector('.conversation-item[aria-current="true"] .conversation-title').textContent, 'The Overwatcher');
  assert.match(div.querySelector('.overwatcher-progress').textContent, /Developer Bot: Inspecting the relevant files/);
  assert.equal(sources.filter(s => !s.closed).length, 1, 'both chats share the task stream');
  const source = sources.find(s => !s.closed);
  await act(async () => source.emit('progress', { stage: 'Running checks…', args: 'PRIVATE' }));
  assert.match(div.querySelector('.overwatcher-progress').textContent, /Developer Bot: Running checks/);
  assert.equal(div.textContent.includes('PRIVATE'), false);
  await act(async () => div.querySelector('.delegated-work .delegated-chat-link').click());
  assert.equal(div.querySelector('.conversation-item[aria-current="true"] .conversation-title').textContent, 'Developer Bot');
  assert.equal(div.querySelector('.overwatcher-progress'), null, 'parent updates cannot appear in the target chat');
  assert.match(div.querySelector('.message-events [role="status"]').textContent, /Running checks/);
  const priorReads = targetReads;
  terminal = true; row.status = 'done'; row.result_summary = result;
  await act(async () => source.emit('end', { status: 'done' }));
  assert.ok(targetReads > priorReads, 'stream completion retrieves the saved target result immediately');
  assert.match(div.querySelector('.work-result-details').textContent, /Full technical evidence/);
  assert.equal(div.querySelector('.message-events [role="status"]'), null);
  const parentItem = [...div.querySelectorAll('.conversation-item')].find(el => el.textContent.includes('The Overwatcher'));
  await act(async () => parentItem.click());
  assert.match(div.querySelector('.message-list').textContent, /Developer Bot finished/);
  assert.equal(div.textContent.includes('Full technical evidence'), false, 'full developer result stays in its own chat');
  assert.equal(div.querySelector('.message-list .delegated-chat-link').textContent, 'Open Developer Bot chat');
  assert.deepEqual(posts, [], 'opening and following chats never submits another execution');
} finally {
  await act(async () => root.unmount());
  delete globalThis.EventSource;
}
console.log('Overwatcher relays stages; target chat appears, opens without execution, and keeps full results: passed');
