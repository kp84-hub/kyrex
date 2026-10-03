import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import DelegatedWork from '../src/components/DelegatedWork.jsx';
const div = document.createElement('div'); document.body.append(div);
const root = createRoot(div);
await act(async () => root.render(React.createElement(DelegatedWork, {
  conversationId: 'research-test', bots: [], delegations: [{
    delegation_id: 'research', target_bot_id: 'browser', executor_prefix: 'browser',
    status: 'done', text: '{"actions":[{"action":"read"}]}',
    result_summary: 'Event evidence and site footer',
  }, { delegation_id: 'calendar', target_bot_id: 'calendar', status: 'done',
    text: 'calendar: today', result_summary: 'Two calendar events' }],
})));
const details = div.querySelector('details');
assert.ok(details);
assert.equal(details.open, false);
assert.equal(details.querySelector('summary').textContent, 'Research details');
assert.match(details.textContent, /Event evidence/);
assert.ok(details.querySelector('.delegated-work-task'));
assert.match(div.querySelectorAll('.delegated-work-item')[1].textContent, /Two calendar events/);
await act(async () => root.unmount());
console.log('Browser research is collapsed; Calendar receipts remain visible: passed');
