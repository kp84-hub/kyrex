import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { useChat } from '../src/hooks/useChat.js';
import Message from '../src/components/Message.jsx';

let latest, stored = [], requests = 0, taskStatus = 'running', offline = true;
let endWithErrorFrame = false, closeQuietly = false;
let conversationReads = 0, taskReads = 0;
let failTranscript = false, deferTask = false, releaseTask;
const response = body => ({ ok: true, status: 200, json: async () => body });
function Probe() { latest = useChat(); return null; }
globalThis.fetch = async (url, options) => {
  if (url === '/api/chat') {
    requests++;
    const requestId = JSON.parse(options.body).request_id;
    stored.push({ id: `turn-${requestId}-user`, role: 'user', content: '#L6Workout preview' });
    let read = 0;
    return { ok: true, body: new ReadableStream({ pull(controller) {
      if (read++ === 0) {
        for (const frame of [{ type: 'task', task_id: `t${requests}`, status: 'running' }, { type: 'delta', content: 'Reading the weekly post…' }]) {
          controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(frame) + '\n\n'));
        }
      } else if (endWithErrorFrame) {
        controller.enqueue(new TextEncoder().encode('data: {"type":"error","message":"task failed"}\n\n'));
        controller.close();
      } else if (closeQuietly) controller.close();
      else controller.error(new TypeError('network error'));
    } }) };
  }
  if (url.startsWith('/api/task/')) {
    taskReads++;
    assert.equal(options.cache, 'no-store');
    if (offline) throw new TypeError('Failed to fetch');
    if (deferTask) return new Promise(resolve => { releaseTask = () => resolve(response({ status: 'done' })); });
    return response({ task_id: url.split('/').at(-1), status: taskStatus });
  }
  if (url === '/api/conversations/c1') {
    conversationReads++;
    if (requests && (offline || failTranscript)) throw new TypeError('Failed to fetch');
    return response({ conversation_id: 'c1', messages: [...stored] });
  }
  if (url === '/api/conversations/c2') return response({ conversation_id: 'c2', messages: [{ id: 'other-chat', role: 'assistant', content: 'A different chat' }] });
  if (url === '/api/conversations') return response({ conversations: [{ conversation_id: 'c1' }] });
  if (url === '/api/bots') return response({ bots: [] });
  return response({});
};
Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
const root = createRoot(document.body.appendChild(document.createElement('div')));
await act(async () => root.render(React.createElement(Probe)));
await act(async () => latest.loadConversation('c1'));
await act(async () => latest.send('#L6Workout preview'));
const interrupted = latest.messages.at(-1);
assert.equal(interrupted.connection_interrupted, true, 'a dropped viewer must recover the task rather than report the action failed');
assert.equal(interrupted.task.status, 'unknown', 'the stale running frame is not a current status');
assert.equal(interrupted.error, null);
assert.equal(latest.error, null);
assert.equal(latest.isGenerating, false);
assert.equal(interrupted.content, 'Reading the weekly post…');
assert.ok(taskReads > 0 && conversationReads > 1, 'recovery checks the existing task and transcript immediately');
await act(async () => latest.retry());
assert.equal(requests, 1, 'checking an interrupted task must never submit a second action');
const view = document.body.appendChild(document.createElement('div'));
const messageRoot = createRoot(view);
await act(async () => messageRoot.render(React.createElement(Message, {
  message: interrupted, isLastAssistant: true, onRetry: latest.retry,
})));
assert.match(view.textContent, /status unavailable/);
assert.match(view.textContent, /Checking the existing task/);
assert.equal(view.querySelector('button').textContent.trim(), 'Check status');

offline = false;
stored.push({ id: 'other-result', role: 'assistant', content: interrupted.content });
await act(async () => latest.refreshMessages('c1'));
const pending = latest.messages.find(m => m.id === interrupted.id);
assert.equal(pending.task.status, 'running', 'task status is read from durable state');
assert.equal(pending.connection_interrupted, true, 'a user-only snapshot cannot erase the pending task');
assert.ok(latest.messages.some(m => m.id === 'other-result'), 'waiting on one task must not hide other background results');
assert.equal(pending.persisted_id, 'task-t1-result', 'an identical unrelated reply cannot acknowledge the task');
taskStatus = 'done';
stored.push({ id: 'task-t1-result', role: 'assistant', content: 'Preview only — nothing sent.\n#L6Workout\nMonday: Workout A' });
await act(async () => latest.refreshMessages('c1'));
const result = latest.messages.find(m => m.persisted_id === 'task-t1-result');
assert.equal(result.id, interrupted.id, 'the recovered reply keeps the visible bubble identity');
assert.match(result.content, /Workout A/);
assert.equal(result.connection_interrupted, undefined);
assert.equal(result.task, undefined);
await act(async () => window.dispatchEvent(new Event('focus')));
assert.equal(latest.messages.filter(m => m.persisted_id === 'task-t1-result').length, 1);
assert.equal(requests, 1);

endWithErrorFrame = true;
taskStatus = 'failed';
await act(async () => latest.send('#L6Workout preview'));
assert.equal(latest.messages.at(-1).task.status, 'failed');
await act(async () => latest.retry());
assert.equal(requests, 2, 'an executor error is also checked against its existing task before offering any new action');
stored.push({ id: 'task-t2-result', role: 'assistant', content: 'Task failed: Browser Host unavailable' });
await act(async () => latest.refreshMessages('c1'));
assert.match(latest.messages.at(-1).content, /Browser Host unavailable/);
endWithErrorFrame = false;
closeQuietly = true;
taskStatus = 'queued';
await act(async () => latest.send('#L6Workout preview'));
assert.equal(latest.messages.at(-1).task.status, 'queued', 'quiet EOF uses the same recovery path');
assert.equal(latest.messages.at(-1).connection_interrupted, true);
assert.equal(requests, 3);
failTranscript = true;
taskStatus = 'running';
await act(async () => latest.refreshMessages('c1'));
assert.equal(latest.messages.at(-1).task.status, 'running', 'a failed transcript read still applies the verified task status');
offline = true;
await act(async () => latest.refreshMessages('c1'));
assert.equal(latest.messages.at(-1).task.status, 'unknown', 'a new outage must not keep claiming the last known running state');
offline = false;
failTranscript = false;
deferTask = true;
let pendingRefresh;
await act(async () => { pendingRefresh = latest.refreshMessages('c1'); });
await act(async () => latest.loadConversation('c2'));
await act(async () => { releaseTask(); await pendingRefresh; });
assert.equal(latest.activeId, 'c2');
assert.equal(latest.messages.at(-1).content, 'A different chat', 'a delayed recovery must not replace a different conversation');
assert.equal(requests, 3);
await act(async () => { messageRoot.unmount(); root.unmount(); });
console.log('Dropped task streams recover by read-only checks, preserve pending work and never resubmit: passed');
