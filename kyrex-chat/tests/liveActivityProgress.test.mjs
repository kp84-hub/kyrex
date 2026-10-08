import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { useLiveActivity } from '../src/hooks/useLiveActivity.js';
import { activityLine } from '../src/lib/activeWork.js';

const sources = [];
globalThis.EventSource = class {
  listeners = new Map();
  constructor(url) { this.url = url; sources.push(this); }
  addEventListener(type, fn) { this.listeners.set(type, fn); }
  close() { this.closed = true; }
  emit(type, payload) { this.listeners.get(type)?.({ data: JSON.stringify(payload) }); }
};
let overlay;
function Harness({ subs }) {
  overlay = useLiveActivity(subs);
  return React.createElement('span', null, activityLine(overlay.chat));
}
const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div);
const subscription = taskId => [{ conversationId: 'chat', taskId,
  activity: { kind: 'delegation', status: 'running', target_bot_id: 'dev', task_id: taskId } }];
const render = subs => act(async () => root.render(React.createElement(Harness, { subs })));
const emit = (source, type, payload) => act(async () => source.emit(type, payload));
try {
  await render(subscription('first'));
  const first = sources[0];
  assert.equal(first.url, '/api/task/first/events');
  await emit(first, 'progress', { stage: 'Inspecting the progress parser…', args: 'PRIVATE' });
  assert.equal(div.textContent, 'Inspecting the progress parser…');
  assert.equal(JSON.stringify(overlay).includes('PRIVATE'), false);
  await emit(first, 'status', { status: 'running' });
  assert.equal(div.textContent, 'Inspecting the progress parser…', 'status preserves same-task progress');
  await emit(first, 'progress', { stage: 123, args: 'PRIVATE' });
  await act(async () => first.listeners.get('progress')({ data: '{bad json' }));
  assert.equal(div.textContent, 'Inspecting the progress parser…');
  await emit(first, 'status', { status: 'awaiting_approval' });
  await emit(first, 'progress', { stage: 'Preparing changes…' });
  assert.equal(div.textContent, 'Waiting for your approval');
  await emit(first, 'end', { status: 'done' });
  assert.equal(div.textContent, '');
  assert.equal(first.closed, true);
  await emit(first, 'progress', { stage: 'Late stale progress' });
  assert.equal(div.textContent, '');

  await render(subscription('second'));
  assert.equal(overlay.chat, undefined, 'the first task overlay cannot leak into the next task');
  const second = sources[1];
  await emit(second, 'progress', { stage: 'Running checks…' });
  assert.equal(div.textContent, 'Running checks…');
  await render(subscription('third'));
  assert.equal(second.closed, true);
  await emit(second, 'status', { status: 'failed' });
  assert.equal(overlay.chat, undefined, 'retired callbacks cannot overwrite the new task');
  const third = sources[2];
  await emit(third, 'error', {});
  assert.equal(third.closed, true);
  await emit(third, 'progress', { stage: 'After transport error' });
  assert.equal(overlay.chat, undefined);
} finally {
  await act(async () => root.unmount());
  delete globalThis.EventSource;
}
console.log('Sidebar follows safe stages, preserves approval, and retires stale task streams: passed');
