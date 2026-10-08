import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Message from '../src/components/Message.jsx';
import DelegatedWork from '../src/components/DelegatedWork.jsx';
import { consumeStream } from '../src/lib/streaming.js';

const div = document.createElement('div'); document.body.append(div);
const root = createRoot(div);
const progress = [{ stage: 'Inspecting the relevant files…', category: 'developer' },
  { stage: 'Running checks…', category: 'developer', args: 'MUST-NOT-DISPLAY' }];
const row = { delegation_id: 'progress', target_bot_id: 'dev', task_id: 'task',
  status: 'running', executor_prefix: 'developer', text: 'Fix the parser', progress };
const render = props => act(async () => root.render(React.createElement(DelegatedWork,
  { conversationId: 'progress-chat', delegations: [row], ...props })));
await render({});
assert.equal(div.querySelector('[role="status"]').textContent, 'Running checks…');
assert.equal(div.querySelector('.message-activity').open, false);
assert.equal(div.textContent.includes('MUST-NOT-DISPLAY'), false);
await render({ delegations: [{ ...row, status: 'awaiting_approval' }] });
assert.equal(div.querySelector('[role="status"]').textContent, 'Waiting for your approval.');

const text = 'Fixed the parser.\n\n' + 'Detailed implementation evidence. '.repeat(100)
  + '\nChecks: 7 passed.\nNot deployed.\nhttps://github.com/kp84-hub/kyrex/pull/370';
await render({ delegations: [{ ...row, status: 'done', result_summary: text }] });
assert.equal(div.querySelector('[aria-live="polite"]'), null, 'terminal work has no live progress indicator');
assert.equal(div.querySelector('.work-result-details').open, false);
assert.ok(div.querySelector('.work-result-preview').textContent.length < 850);
assert.match(div.querySelector('.work-result-preview').textContent, /7 passed/);
assert.match(div.querySelector('.work-result-preview').textContent, /Not deployed/);
assert.equal(div.querySelector('.work-result-preview a').getAttribute('href'), 'https://github.com/kp84-hub/kyrex/pull/370');
assert.match(div.querySelector('.work-result-details').textContent, /Detailed implementation evidence/);

const events = progress.map(payload => ({ kind: 'progress', payload }));
async function* stream() { yield { type: 'done', content: text, developer_result: true, events }; }
const { terminal } = await consumeStream(stream());
assert.equal(terminal.developer_result, true);
assert.deepEqual(terminal.events, events);
// The exact same shape is used by live completion and persisted reloads.
await act(async () => root.render(React.createElement(Message, { message: {
  role: 'assistant', content: terminal.content, developer_result: terminal.developer_result, events: terminal.events,
} })));
assert.equal(div.querySelector('.work-result-details').open, false);
assert.match(div.querySelector('.markdown').textContent, /Fixed the parser/);
await act(async () => root.render(React.createElement(Message, { message: {
  role: 'assistant', content: text,
} })));
assert.equal(div.querySelector('.work-result-details'), null, 'ordinary answers are not automatically folded');
await act(async () => root.unmount());
console.log('Live developer stages, approvals, terminal metadata and expandable full results: passed');
