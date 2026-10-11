import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import ChatRepository from '../src/components/ChatRepository.jsx';
import App from '../src/App.jsx';

globalThis.localStorage = window.localStorage;
window.localStorage.setItem('kyrex-chat.activeConversationId', 'c1');
let bot = { id: 'dev', name: 'Developer Bot', status: 'running', manageable: true,
  available: true, repository_available: true, role: { id: 'custom' } };
let info = { repositories: ['alice/private'], repository: null, base: null,
  publisher_connected: false, can_publish: false };
let checkoutFails = false;
const requests = [];
const response = (payload, status = 200) => ({ ok: status === 200, status, json: async () => payload });
globalThis.fetch = async (url, options = {}) => {
  requests.push({ url, options });
  if (url === '/api/bots') return response({ bots: [bot] });
  if (url === '/api/bots/dev' && options.method === 'PATCH') {
    bot = { ...bot, status: JSON.parse(options.body).status }; return response(bot);
  }
  if (url === '/api/bots/dev/repository') {
    if (options.method === 'POST') {
      assert.equal(bot.status, 'paused');
      if (checkoutFails) return response({ detail: 'Wait for queued tasks to finish first.' }, 409);
      info = { ...info, repository: JSON.parse(options.body).repository, base: 'main' };
    }
    return response(info);
  }
  if (url === '/api/conversations') return response({ conversations: [
    { conversation_id: 'c1', bot_id: 'dev', title: 'Developer Bot' } ] });
  if (url === '/api/conversations/c1') return response({ conversation_id: 'c1', bot_id: 'dev', messages: [] });
  if (url.endsWith('/delegations')) return response({ delegations: [] });
  if (url === '/api/chat/workspaces') return response({ workspaces: [] });
  if (url === '/api/chat/providers') return response({ providers: [] });
  return response({});
};
const node = document.body.appendChild(document.createElement('div'));
let root = createRoot(node);
const button = (name) => [...node.querySelectorAll('button')].find(b => b.textContent === name);
const draw = (taskRunning = false, activeBotId = 'dev') => root.render(
  React.createElement(ChatRepository, { bots: [bot], activeBotId, taskRunning,
    onChanged: () => draw() }));
const render = async (...args) => act(async () => draw(...args));

// Exercise the actual App/useChat integration so an inaccessible child panel
// cannot pass just because the standalone component renders successfully.
await act(async () => root.render(React.createElement(App)));
assert.ok(button('Repository: original workspace'), 'control appears in the Developer Bot chat without opening Bot Settings');
assert.equal(node.querySelector('.chat-area .developer-repository-chat') !== null, true);
await act(async () => root.unmount());
root = createRoot(node);

await render();
assert.ok(button('Repository: original workspace'));
await act(async () => button('Repository: original workspace').click());
assert.equal(node.querySelector('select').disabled, true);
await act(async () => button('Pause Bot').click());
assert.equal(bot.status, 'paused');
assert.equal(node.querySelector('select').disabled, false);
await act(async () => {
  node.querySelector('select').value = 'alice/private';
  node.querySelector('select').dispatchEvent(new Event('change', { bubbles: true }));
});
checkoutFails = true;
await act(async () => button('Use repository').click());
assert.match(node.querySelector('[role="alert"]').textContent, /queued tasks/);
assert.equal(info.repository, null, 'server rejects a switch with queued work');
assert.equal(bot.status, 'paused', 'failed checkout does not implicitly start or retry the Bot');
checkoutFails = false;
await act(async () => button('Use repository').click());
assert.equal(info.repository, 'alice/private');
await act(async () => button('Start Bot').click());
assert.equal(bot.status, 'running');
assert.equal(node.querySelector('select').disabled, true);
await act(async () => button('Close repository').click());
assert.ok(button('Repository: alice/private'), 'selected project stays visible while the panel is closed');
assert.deepEqual(requests.filter(r => r.options.method === 'PATCH').map(r => JSON.parse(r.options.body)),
  [{ status: 'paused' }, { status: 'running' }]);
assert.equal(requests.some(r => r.url.endsWith('/publish')), false);

await render(true);
await act(async () => button('Repository: alice/private').click());
assert.equal(button('Pause Bot').disabled, true);
assert.equal(button('Use repository').disabled, true);
assert.match(node.textContent, /Finish or stop the current task/);

bot = { ...bot, repository_available: false, role: { id: 'developer' } };
await render();
assert.equal(node.textContent, '', 'server denial overrides role and display name');
bot = { ...bot, repository_available: true };
const before = requests.length;
await render(false, 'missing');
await render(false, null);
assert.equal(node.textContent, '');
assert.equal(requests.length, before, 'unbound and unavailable Bots never query a repository');
await act(async () => root.unmount());
console.log('Actual Chat repository control, pause/select/start, current project, queue failure and active-task guards: passed');
