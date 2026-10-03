import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import Message from '../src/components/Message.jsx';
const div = document.createElement('div'); document.body.append(div);
const root = createRoot(div);
let message = { role: 'assistant', content: 'I’m checking the source.', streaming: true,
  events: [
    { kind: 'progress', payload: { stage: 'Finding the relevant email…' } },
    { kind: 'progress', payload: { stage: 'Finding the relevant email…' } },
    { kind: 'progress', payload: { action: 'read', api_key: 'PRIVATE-KEY', internal_dump: 'DO-NOT-RENDER' } },
    { kind: 'approval_request', tier: 2, summary: 'Delete the selected event' },
  ] };
await act(async () => root.render(React.createElement(Message, { message })));
assert.equal(div.querySelectorAll('[role="status"]').length, 1);
assert.equal(div.querySelector('[role="status"]').textContent, 'Reading the source…');
assert.equal(div.querySelector('details').open, false);
assert.match(div.querySelector('summary').textContent, /Activity \(2\)/);
assert.match(div.querySelector('.event-approval').textContent, /Delete the selected event/);
assert.equal(div.textContent.includes('PRIVATE-KEY'), false);
assert.equal(div.textContent.includes('DO-NOT-RENDER'), false);
message = { ...message, streaming: false, content: 'Found the trip details.', task: { status: 'done' } };
await act(async () => root.render(React.createElement(Message, { message })));
assert.equal(div.querySelector('[role="status"]'), null, 'historical progress is not shown as live work');
assert.equal(div.querySelector('details').open, false);
assert.match(div.querySelector('.markdown').textContent, /Found the trip details/);
await act(async () => root.render(React.createElement(Message, { message: {
  role: 'assistant', content: 'Source: [Town event calendar](https://www.fuquay-varina.org/calendar.aspx?EID=123)',
} })));
const source = div.querySelector('.markdown a');
assert.equal(source.textContent, 'Town event calendar');
assert.equal(source.getAttribute('href'), 'https://www.fuquay-varina.org/calendar.aspx?EID=123');
assert.equal(source.getAttribute('target'), '_blank');
assert.equal(source.getAttribute('rel'), 'noopener noreferrer');
await act(async () => root.unmount());
console.log('One current update, collapsed activity, visible approvals and no raw payload dump: passed');
