import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Level6PreviewActions from '../src/components/Level6PreviewActions.jsx';

const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div);
let requests = 0, refreshes = [], finish, fail = false;
globalThis.fetch = (url, options = {}) => {
  requests++;
  assert.equal(url, '/api/conversations/l6-thread/level6-preview');
  assert.equal(options.method, 'POST');
  assert.equal(options.body, undefined, 'the server selects the owner, week and preview command');
  return new Promise(resolve => { finish = () => resolve({
    ok: !fail, status: fail ? 409 : 200,
    json: async () => fail ? { detail: 'Browser Bot is unavailable' }
      : { task_id: 'retry-1', status: 'queued' },
  }); });
};
const render = messages => act(async () => root.render(React.createElement(Level6PreviewActions, {
  conversationId: 'l6-thread', messages, onQueued: async id => refreshes.push(id),
})));
const button = () => div.querySelector('button');
await render([]);
assert.equal(requests, 0, 'opening the scheduled thread never queues a task');
assert.equal(button().textContent, 'Run preview now');
await act(async () => { button().click(); button().click(); });
assert.equal(requests, 1);
assert.equal(button().disabled, true);
await act(async () => finish());
assert.deepEqual(refreshes, ['l6-thread']);
assert.equal(button().disabled, true, 'keep disabled before refreshed active state arrives');
await render([{ id: 'task-retry-1-result', task: { taskId: 'retry-1', status: 'running' } }]);
assert.equal(button().disabled, true);
await render([{ id: 'task-retry-1-result', role: 'assistant', content: 'Preview only — nothing sent.' }]);
assert.equal(button().disabled, false, 'a saved terminal result allows a deliberate new preview');
fail = true;
await act(async () => button().click());
await act(async () => finish());
assert.match(div.querySelector('[role="alert"]').textContent, /Browser Bot is unavailable/);
assert.equal(button().disabled, false);
await render([{ id: 'task-another-result', task: { taskId: 'another', status: 'queued' } }]);
assert.equal(button().disabled, true, 'an active task restored after reload prevents a second attempt');
assert.equal(requests, 2, 'no recipient preparation or send was requested');
await act(async () => root.unmount());
console.log('L6 Run now queues one preview, follows saved state, and never prepares or sends: passed');
