import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
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
  if (url === '/api/chat/onboarding') return response({ required: false, step: 'ready' });
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
const root = createRoot(node);
const button = (name) => [...node.querySelectorAll('button')].find(b => b.textContent === name);
await act(async () => root.render(React.createElement(App)));
assert.equal(node.querySelector('.developer-repository'), null, 'repository controls are hidden from chat');
assert.equal(requests.some(r => r.url.endsWith('/repository')), false, 'chat does not fetch repository settings');
await act(async () => button('Bots').click());
assert.ok(button('Repository'), 'repository management remains available in Bot Settings');
await act(async () => button('Repository').click());
assert.ok(node.querySelector('[aria-label="Developer repository"]'));
await act(async () => button('Pause Bot').click());
assert.equal(bot.status, 'paused');
const select = () => node.querySelector('[aria-label="Developer repository"] select');
assert.equal(select().disabled, false);
await act(async () => {
  select().value = 'alice/private';
  select().dispatchEvent(new Event('change', { bubbles: true }));
});
checkoutFails = true;
await act(async () => button('Use repository').click());
assert.match(node.querySelector('[aria-label="Developer repository"] [role="alert"]').textContent, /queued tasks/);
assert.equal(info.repository, null);
assert.equal(bot.status, 'paused');
checkoutFails = false;
await act(async () => button('Use repository').click());
assert.equal(info.repository, 'alice/private');
await act(async () => button('Start Bot').click());
assert.equal(bot.status, 'running');
assert.equal(select().disabled, true);
await act(async () => button('Close').click());
assert.equal(node.querySelector('.developer-repository'), null, 'returning to chat hides the controls again');
assert.equal(node.querySelector('.chat-area').textContent.includes('alice/private'), false);
assert.equal(info.repository, 'alice/private', 'hiding the row preserves the selected workspace');
assert.equal(requests.some(r => r.url.endsWith('/publish')), false);
await act(async () => root.unmount());
console.log('Repository hidden in actual Chat; Bot Settings pause/select/start and server queue rejection still work: passed');
