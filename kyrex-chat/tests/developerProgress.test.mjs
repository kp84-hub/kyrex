import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Message from '../src/components/Message.jsx';
import DelegatedWork from '../src/components/DelegatedWork.jsx';
import DeveloperChanges from '../src/components/DeveloperChanges.jsx';
import { consumeStream } from '../src/lib/streaming.js';
import { resultPreview } from '../src/components/WorkResult.jsx';

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
const instructions = 'Read-only investigation: inspect the progress flow. '.repeat(8);
await render({ delegations: [{ ...row, text: instructions }] });
assert.equal(div.querySelector('.delegated-task-details').open, false);
assert.equal(div.querySelector('.delegated-task-details .delegated-work-task').textContent, instructions);

const trace = '[Delegated to dev] Traced the full progress-update chain: ' + 'executor → parser → durable event. '.repeat(40);
const recommendation = 'One practical improvement: show the latest progress stage in the sidebar while the bot works. Keep approval and terminal states authoritative.';
const investigation = trace + '\n\n' + recommendation + '\n\nAccess limitation: ' + 'No live deployment access available. '.repeat(8);
assert.ok(resultPreview(investigation).startsWith(recommendation));
assert.equal(resultPreview(investigation).includes('Traced the full'), false);
const clipped = resultPreview('Recommendation: ' + 'Render meaningful progress updates '.repeat(30));
assert.equal(clipped, 'Open the full result to read the findings.', 'an oversized sentence is not chopped into a misleading fragment');
await render({ delegations: [{ ...row, status: 'done', result_summary: investigation }] });
assert.match(div.querySelector('.work-result-preview').textContent, /One practical improvement/);
assert.match(div.querySelector('.work-result-details').textContent, /Traced the full progress-update chain/);
const prUrl = 'https://github.com/kp84-hub/kyrex/pull/370';
assert.match(resultPreview('Fixed it.\n\n' + 'Additional evidence. '.repeat(60) + '\n[Open the pull request after reviewing these lengthy verification notes](' + prUrl + ')'), /https:\/\/github.com\/kp84-hub\/kyrex\/pull\/370/);

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

let opened;
await render({ delegations: [{ ...row, target_bot_name: 'Developer Bot', target_conversation_id: 'dev-chat' }],
  onOpenConversation: id => { opened = id; } });
assert.equal(div.querySelector('.developer-delegation-line [role="status"]').textContent, 'Developer Bot · Running checks…');
assert.equal(div.querySelector('.message-activity'), null, 'full activity stays in Developer Bot');
await act(async () => div.querySelector('.delegated-chat-link').click());
assert.equal(opened, 'dev-chat');

const events = progress.map(payload => ({ kind: 'progress', payload }));
async function* stream() { yield { type: 'done', content: text, developer_result: true, developer_has_changes: true, events }; }
const { terminal } = await consumeStream(stream());
assert.equal(terminal.developer_result, true);
assert.deepEqual(terminal.events, events);
const liveMessage = { role: 'assistant', content: '', developer_result: true, events,
  task: { taskId: 'task', status: 'running' } };
const renderMessage = (message, onReviewDeveloperChanges) => act(async () =>
  root.render(React.createElement(Message, { message, onReviewDeveloperChanges })));
await renderMessage(liveMessage);
const card = div.querySelector('.developer-activity');
assert.equal(card.querySelector('[role="status"]').textContent, 'Running checks…');
const activity = card.querySelector('details');
assert.equal(activity.open, false);
activity.open = true;
assert.deepEqual([...activity.querySelectorAll('li')].map(li => li.textContent),
  ['Inspecting the relevant files…', 'Running checks…']);
let reviewedReport;
// The exact same shape is used by live completion and persisted reloads.
await renderMessage({ ...liveMessage, task: null, content: terminal.content, events: terminal.events,
  developer_has_changes: terminal.developer_has_changes }, report => { reviewedReport = report; });
assert.equal(div.querySelector('.developer-activity'), card, 'the live card becomes the result');
assert.equal(activity.open, true);
await act(async () => card.querySelector('.delegated-chat-link').click());
assert.equal(reviewedReport, terminal.content);
assert.equal(div.querySelector('.work-result-details').open, false);
assert.match(div.querySelector('.markdown').textContent, /Fixed the parser/);
await act(async () => root.render(React.createElement(Message, { message: {
  role: 'assistant', content: text,
} })));
assert.equal(div.querySelector('.work-result-details'), null, 'ordinary answers are not automatically folded');
let approvedTask;
await act(async () => root.render(React.createElement(Message, { message: {
  role: 'assistant', content: '', developer_result: true, delegated_active: true,
  task: { taskId: 'background-task', status: 'awaiting_approval' },
  approval: { task_id: 'background-task', tier: 2, summary: 'Review edits' },
}, onRespondApproval: () => {}, onApproveDelegated: async id => { approvedTask = id; } })));
assert.equal(div.querySelector('.approval-input'), null, 'background T2 approvals use the task-owned host endpoint');
assert.ok(div.querySelector('.developer-activity .approval-card'), 'the task owns the inline approval controls');
await act(async () => div.querySelector('.approval-btn.approve').click());
assert.equal(approvedTask, 'background-task');

// jsdom provides the dialog element but does not implement its modal methods.
window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
window.HTMLDialogElement.prototype.close = function () { this.open = false; };
const workspaceReads = [];
globalThis.fetch = async (url, options) => {
  workspaceReads.push([url, options.method || 'GET']);
  if (workspaceReads.length === 1) return { ok: false, status: 409,
    json: async () => ({ detail: 'Wait for Developer Bot’s active tasks to finish first.' }) };
  if (url.includes('file=')) return { ok: true, json: async () => url.includes('large.txt')
    ? { error: 'This file’s diff is too large to show here.' } : { diff: '-old\n+new' } };
  return { ok: true, json: async () => ({ repository: 'alice/project', branch: 'kyrex/work-preview',
    base: 'main', review_available: true, revision: 'test-revision',
    changes: [{ path: 'parser.py' }, { path: 'large.txt' }] }) };
};
let reviewClosed = false;
await act(async () => root.render(React.createElement(DeveloperChanges, { botId: 'dev',
  report: 'Checks: 7 passed.', onClose: () => { reviewClosed = true; } })));
assert.equal(div.querySelector('dialog').open, true);
assert.match(div.querySelector('[role="alert"]').textContent, /active tasks/);
await act(async () => [...div.querySelectorAll('button')].find(button => button.textContent === 'Try again').click());
assert.equal(div.querySelector('.developer-changes-context code').textContent, 'kyrex/work-preview');
const [fileDiff, largeDiff, taskReport] = div.querySelectorAll('dialog details');
assert.equal(fileDiff.open, false);
assert.equal(fileDiff.querySelector('summary').textContent, 'parser.py');
assert.equal(fileDiff.querySelector('pre'), null);
assert.equal(workspaceReads.length, 2, 'the list does not fetch any file diffs');
await act(async () => {
  fileDiff.open = true;
  fileDiff.dispatchEvent(new Event('toggle'));
  largeDiff.open = true;
  largeDiff.dispatchEvent(new Event('toggle'));
});
assert.equal(fileDiff.querySelector('code').textContent, '-old\n+new');
assert.match(largeDiff.textContent, /too large/);
assert.match(taskReport.textContent, /7 passed/);
assert.deepEqual(workspaceReads, [
  ...Array(2).fill(['/api/bots/dev/repository/workspace?files_only=true', 'GET']),
  ['/api/bots/dev/repository/workspace?file=parser.py&revision=test-revision', 'GET'],
  ['/api/bots/dev/repository/workspace?file=large.txt&revision=test-revision', 'GET'],
]);
await act(async () => div.querySelector('dialog button').click());
assert.equal(reviewClosed, true);
await act(async () => root.unmount());
console.log('Live developer stages, task approvals, terminal results and read-only workspace review: passed');
